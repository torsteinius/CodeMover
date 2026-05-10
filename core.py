"""Core logic for Code Mover.

Handles:
- Repository root detection with custom markers
- Safe path validation
- Patch validation (YAML structure, actions, exact matches)
- Diff preview generation
- Patch application with backup
- Repository tree fingerprinting for cross-side validation
- Patch generation (export)
- Patch history with tamper detection
- ZIP export/import
"""

from pathlib import Path
import yaml
import difflib
import shutil
import hashlib
import json
import zipfile
import io
from datetime import datetime
from typing import Optional

APP_ROOT_MARKERS = [
    "app.py",
    "core.py",
]

ALLOWED_ACTIONS = {
    "patch_hunk",
    "create_file",
    "append_to_file",
}

PATCH_HISTORY_DIR = "_code_mover_patches"


# ─── Hunk Parsing / Building ────────────────────────────────────────────


def parse_hunk(hunk: str) -> tuple[str, str]:
    """Parse a unified diff-style hunk into (find_text, replace_text).

    Lines starting with '-' are removed content (what to find and remove).
    Lines starting with '+' are added content (what to replace with).
    Lines starting with ' ' (space) are context lines included in both sides,
    which tightens the match and prevents accidental replacements.
    Lines starting with '@@' are hunk headers — informational only, skipped.

    Example input:
        @@ -15,1 +15,1 @@
        -imo = st.text_input("IMO")
        +imo = normalize_imo(st.text_input("IMO"))

    Returns:
        Tuple of (find_text, replace_text) for content-based matching.
    """
    find_lines: list[str] = []
    replace_lines: list[str] = []

    for line in hunk.splitlines():
        if line.startswith("@@"):
            continue  # Hunk header — skip
        elif line.startswith("-"):
            find_lines.append(line[1:])
        elif line.startswith("+"):
            replace_lines.append(line[1:])
        else:
            # Context line — strip leading space if present, include in both
            context = line[1:] if line.startswith(" ") else line
            find_lines.append(context)
            replace_lines.append(context)

    find = "\n".join(find_lines)
    replace = "\n".join(replace_lines)

    # Preserve trailing newline (YAML block scalars end with \n)
    if hunk.endswith("\n"):
        find += "\n"
        replace += "\n"

    return find, replace


def build_hunk_string(old_text: str, new_text: str) -> str:
    """Build a unified diff hunk string comparing old and new file content.

    Produces standard unified diff output:
        @@ -line,count +line,count @@
        -removed line
        +added line
         context line

    The --- / +++ file header lines are stripped — only the @@ hunks remain,
    ready to be used as the 'hunk' field in a patch_hunk change.

    Args:
        old_text: Baseline content (what the other side currently has).
        new_text: Updated content (what we want the other side to have).

    Returns:
        Hunk string with all changed sections separated by @@ headers.
    """
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    diff_lines = list(difflib.unified_diff(old_lines, new_lines, n=3))

    # Strip the --- and +++ file header lines (always the first two)
    if len(diff_lines) >= 2 and diff_lines[0].startswith("---"):
        diff_lines = diff_lines[2:]

    return "".join(diff_lines)


# ─── Repository Detection ───────────────────────────────────────────────


def find_repo_root(start: Path, markers: Optional[list[str]] = None) -> Path:
    """Walk upward from start until a directory containing all markers is found."""
    if markers is None:
        markers = APP_ROOT_MARKERS

    current = start.resolve()

    for path in [current, *current.parents]:
        if all((path / marker).exists() for marker in markers):
            return path

    raise RuntimeError(
        f"Fant ikke gyldig repo-root med markører {markers}. Starter fra: {start}"
    )


def validate_repo_markers(repo_path: Path, markers: list[str]) -> list[str]:
    """Check which markers exist / are missing in a repo path.

    Returns list of missing markers (empty = all good).
    """
    missing = []
    for marker in markers:
        if not (repo_path / marker).exists():
            missing.append(marker)
    return missing


# ─── Tree Fingerprinting ────────────────────────────────────────────────


def compute_tree_fingerprint(repo_root: Path) -> str:
    """Compute a hash of the relative file tree structure.

    Creates a deterministic fingerprint of all files and directories
    in the repo. Used to verify that a patch targets the correct repository
    on the other side.
    """
    hasher = hashlib.sha256()
    paths = sorted(repo_root.rglob("*"))

    for path in paths:
        rel = path.relative_to(repo_root)
        parts = rel.parts
        if any(p.startswith(".") for p in parts):
            continue
        if rel.name == "__pycache__":
            continue
        if "_code_mover_backups" in parts or "_code_mover_patches" in parts:
            continue

        if path.is_dir():
            hasher.update(f"dir:{rel}\n".encode("utf-8"))
        else:
            try:
                stat = path.stat()
                hasher.update(f"file:{rel}:{stat.st_size}\n".encode("utf-8"))
            except OSError:
                hasher.update(f"file:{rel}:?\n".encode("utf-8"))

    return hasher.hexdigest()[:16]


def compute_file_structure_snapshot(repo_root: Path) -> str:
    """Generate a human-readable tree structure of the repo."""
    lines = []
    paths = sorted(repo_root.rglob("*"))

    for path in paths:
        rel = path.relative_to(repo_root)
        parts = rel.parts

        if any(p.startswith(".") for p in parts):
            continue
        if rel.name == "__pycache__":
            continue
        if "_code_mover_backups" in parts or "_code_mover_patches" in parts:
            continue

        depth = len(parts) - 1
        indent = "  " * depth
        prefix = "📁 " if path.is_dir() else "📄 "
        lines.append(f"{indent}{prefix}{rel.name}")

    return "\n".join(lines)


# ─── Per-File Hashing (for tamper detection) ────────────────────────────


def compute_file_hashes(repo_root: Path) -> dict[str, str]:
    """Compute SHA256 hashes for all tracked files in the repo.

    Returns dict of {relative_path: hash}.
    Used to detect if files have changed between patches.
    """
    hashes = {}
    paths = sorted(repo_root.rglob("*"))

    for path in paths:
        if not path.is_file():
            continue

        rel = path.relative_to(repo_root)
        parts = rel.parts

        # Skip hidden, cache, and code_mover internals
        if any(p.startswith(".") for p in parts):
            continue
        if rel.name == "__pycache__":
            continue
        if "_code_mover_backups" in parts or "_code_mover_patches" in parts:
            continue

        try:
            content = path.read_bytes()
            file_hash = hashlib.sha256(content).hexdigest()
            hashes[str(rel)] = file_hash
        except (OSError, PermissionError):
            pass

    return hashes


def detect_changes_since_last_patch(
    repo_root: Path,
    last_patch_id: Optional[str] = None,
) -> list[dict]:
    """Compare current file hashes against the last applied patch's snapshot.

    Returns list of changed files with old/new hash.
    Empty list = no changes detected.
    """
    history = load_patch_history(repo_root)

    if not history:
        return []  # No history to compare against

    # Find the snapshot to compare against
    if last_patch_id:
        # Compare against a specific patch
        target = next((p for p in history if p["patch_id"] == last_patch_id), None)
        if not target:
            return []
        previous_hashes = target.get("file_hashes_after", {})
    else:
        # Compare against the most recent applied patch
        applied = [p for p in history if p["status"] == "applied"]
        if not applied:
            return []
        previous_hashes = applied[-1].get("file_hashes_after", {})

    current_hashes = compute_file_hashes(repo_root)
    changes = []

    # Check for modified files
    for file_path, current_hash in current_hashes.items():
        previous_hash = previous_hashes.get(file_path)
        if previous_hash is None:
            changes.append({
                "file": file_path,
                "type": "new",
                "old_hash": None,
                "new_hash": current_hash,
            })
        elif previous_hash != current_hash:
            changes.append({
                "file": file_path,
                "type": "modified",
                "old_hash": previous_hash,
                "new_hash": current_hash,
            })

    # Check for deleted files
    for file_path in previous_hashes:
        if file_path not in current_hashes:
            changes.append({
                "file": file_path,
                "type": "deleted",
                "old_hash": previous_hashes[file_path],
                "new_hash": None,
            })

    return changes


# ─── Path Safety ────────────────────────────────────────────────────────


def safe_relative_path(repo_root: Path, file_path: str) -> Path:
    """Validate and resolve a file path relative to repo_root.

    Rejects:
    - Absolute paths (starting with /)
    - Path traversal (..)
    - Paths that resolve outside repo_root
    """
    if file_path.startswith("/") or ".." in Path(file_path).parts:
        raise ValueError(f"Ulovlig filsti: {file_path}")

    full_path = (repo_root / file_path).resolve()

    if not str(full_path).startswith(str(repo_root.resolve())):
        raise ValueError(f"Fil peker utenfor repo: {file_path}")

    return full_path


# ─── Patch History ─────────────────────────────────────────────────────


def get_patch_history_dir(repo_root: Path) -> Path:
    """Get the path to the patch history directory for a repo."""
    return repo_root / PATCH_HISTORY_DIR


def load_patch_history(repo_root: Path) -> list[dict]:
    """Load patch history from disk. Returns list of patch records."""
    history_dir = get_patch_history_dir(repo_root)
    history_file = history_dir / "history.json"

    if not history_file.exists():
        return []

    try:
        with open(history_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_patch_history(repo_root: Path, history: list[dict]) -> None:
    """Save patch history to disk."""
    history_dir = get_patch_history_dir(repo_root)
    history_dir.mkdir(parents=True, exist_ok=True)

    history_file = history_dir / "history.json"
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def add_patch_to_history(
    repo_root: Path,
    patch: dict,
    status: str,
    file_hashes_before: Optional[dict] = None,
    file_hashes_after: Optional[dict] = None,
) -> str:
    """Add a patch record to history.

    Args:
        repo_root: Repository root.
        patch: The patch dict that was applied/generated.
        status: 'generated', 'applied', or 'failed'.
        file_hashes_before: File hashes before applying (for tamper detection).
        file_hashes_after: File hashes after applying.

    Returns:
        The patch_id (timestamp-based).
    """
    history = load_patch_history(repo_root)

    patch_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    record = {
        "patch_id": patch_id,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "source_side": patch.get("source_side", "?"),
        "description": patch.get("description", ""),
        "source_tree_fingerprint": patch.get("source_tree_fingerprint", ""),
        "change_count": len(patch.get("changes", [])),
        "changes_summary": [
            {
                "file": c.get("file", "?"),
                "action": c.get("action", "?"),
            }
            for c in patch.get("changes", [])
        ],
        "file_hashes_before": file_hashes_before or {},
        "file_hashes_after": file_hashes_after or {},
    }

    history.append(record)
    save_patch_history(repo_root, history)

    return patch_id


def get_patch_history_summary(repo_root: Path) -> list[dict]:
    """Get a human-readable summary of patch history."""
    history = load_patch_history(repo_root)

    return [
        {
            "patch_id": r["patch_id"],
            "timestamp": r["timestamp"],
            "status": r["status"],
            "source_side": r["source_side"],
            "description": r["description"][:60] if r["description"] else "—",
            "change_count": r["change_count"],
            "changes_summary": r["changes_summary"],
        }
        for r in reversed(history)  # Most recent first
    ]


# ─── ZIP Export / Import ───────────────────────────────────────────────


def export_patch_to_zip(patch_yaml: str, repo_root: Path) -> bytes:
    """Package a patch as a ZIP file in memory.

    The ZIP contains:
    - patch.yaml: The YAML patch itself
    - file_hashes.json: Current file hashes for tamper detection
    - metadata.json: Patch metadata (side, fingerprint, etc.)
    - files/: A complete snapshot of all repo files (for later diffing)

    Returns:
        ZIP file contents as bytes.
    """
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # The YAML patch
        zf.writestr("patch.yaml", patch_yaml)

        # Current file hashes (for tamper detection on the other side)
        file_hashes = compute_file_hashes(repo_root)
        zf.writestr("file_hashes.json", json.dumps(file_hashes, indent=2))

        # Metadata
        patch = yaml.safe_load(patch_yaml)
        metadata = {
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_side": patch.get("source_side", "?"),
            "source_tree_fingerprint": patch.get("source_tree_fingerprint", ""),
            "description": patch.get("description", ""),
            "file_count": len(file_hashes),
        }
        zf.writestr("metadata.json", json.dumps(metadata, indent=2))

        # Full file snapshot for later diffing
        for rel_path, _ in sorted(file_hashes.items()):
            full_path = repo_root / rel_path
            if full_path.exists() and full_path.is_file():
                zf.writestr(f"files/{rel_path}", full_path.read_bytes())

    return buf.getvalue()


def import_patch_from_zip(zip_bytes: bytes) -> tuple[str, dict, dict]:
    """Extract patch and metadata from a ZIP file.

    Args:
        zip_bytes: Raw ZIP file contents.

    Returns:
        Tuple of (patch_yaml_str, file_hashes_dict, metadata_dict).
    """
    buf = io.BytesIO(zip_bytes)

    with zipfile.ZipFile(buf, "r") as zf:
        # Validate expected files exist
        for name in ["patch.yaml", "file_hashes.json", "metadata.json"]:
            if name not in zf.namelist():
                raise ValueError(f"Mangler {name} i ZIP-filen")

        patch_yaml = zf.read("patch.yaml").decode("utf-8")
        file_hashes = json.loads(zf.read("file_hashes.json").decode("utf-8"))
        metadata = json.loads(zf.read("metadata.json").decode("utf-8"))

    return patch_yaml, file_hashes, metadata


# ─── Patch Validation ──────────────────────────────────────────────────


def validate_patch(
    patch_text: str,
    repo_root: Path,
    expected_side: Optional[str] = None,
    source_file_hashes: Optional[dict[str, str]] = None,
) -> dict:
    """Validate a YAML patch against the current repository.

    Args:
        patch_text: Raw YAML string.
        repo_root: Resolved repository root path.
        expected_side: If set, verify patch.source_side matches (cross-side check).
        source_file_hashes: File hashes from the source side (for tamper detection).

    Returns:
        Parsed and validated patch dict.

    Raises:
        ValueError on any validation failure.
    """
    patch = yaml.safe_load(patch_text)

    if not isinstance(patch, dict):
        raise ValueError("Patch må være et YAML-objekt.")

    if patch.get("version") != 1:
        raise ValueError("Kun version: 1 støttes.")

    # Cross-side validation
    if expected_side:
        source_side = patch.get("source_side", "?")
        if source_side == expected_side:
            raise ValueError(
                f"Patchen er generert på samme side ({expected_side}). "
                f"Du må bruke en patch generert på den andre siden."
            )

    # Validate tree fingerprint if present
    source_fingerprint = patch.get("source_tree_fingerprint")
    if source_fingerprint:
        local_fingerprint = compute_tree_fingerprint(repo_root)
        if source_fingerprint != local_fingerprint:
            raise ValueError(
                f"Tre-struktur-fingeravtrykk matcher ikke!\n"
                f"  Patch (kilde): {source_fingerprint}\n"
                f"  Lokalt:         {local_fingerprint}\n\n"
                f"Dette kan bety at patchen er ment for et annet repo, "
                f"eller at repoet har blitt endret siden patchen ble generert."
            )

    # Tamper detection: compare source file hashes with local
    if source_file_hashes:
        local_hashes = compute_file_hashes(repo_root)
        tampered_files = []

        for file_path, source_hash in source_file_hashes.items():
            local_hash = local_hashes.get(file_path)
            if local_hash is None:
                tampered_files.append(f"{file_path} (mangler lokalt)")
            elif local_hash != source_hash:
                tampered_files.append(f"{file_path} (endret)")

        if tampered_files:
            raise ValueError(
                f"⚠️ TAMPER DETECTED! Følgende filer er endret siden patchen ble generert:\n"
                + "\n".join(f"  - {f}" for f in tampered_files)
                + "\n\nDette kan bety at filer har blitt modifisert manuelt, "
                "eller at patchen ikke er ment for dette repoet."
            )

    changes = patch.get("changes")
    if not isinstance(changes, list):
        raise ValueError("Patch må ha 'changes' som en liste.")

    for change in changes:
        action = change.get("action")
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"Ugyldig action: {action}")

        file_path = change.get("file")
        if not file_path:
            raise ValueError("Alle changes må ha 'file'.")

        full_path = safe_relative_path(repo_root, file_path)

        if action == "patch_hunk":
            if not full_path.exists():
                raise ValueError(f"Filen finnes ikke: {file_path}")

            hunk = change.get("hunk")
            if not hunk:
                raise ValueError(f"patch_hunk mangler hunk: {file_path}")

            find, _ = parse_hunk(hunk)
            content = full_path.read_text(encoding="utf-8")
            count = content.count(find)

            if count != 1:
                raise ValueError(
                    f"hunk må matche nøyaktig én gang i {file_path}, "
                    f"men matcher {count}."
                )

        elif action == "create_file":
            if full_path.exists():
                raise ValueError(
                    f"create_file nekter å overskrive eksisterende fil: {file_path}"
                )

            if "content" not in change:
                raise ValueError(f"create_file mangler content: {file_path}")

        elif action == "append_to_file":
            if not full_path.exists():
                raise ValueError(f"append_to_file krever eksisterende fil: {file_path}")

            if "content" not in change:
                raise ValueError(f"append_to_file mangler content: {file_path}")

    return patch


# ─── Auto-Generate Changes from ZIP Diff ──────────────────────────────


def generate_changes_from_zip_diff(
    zip_bytes: bytes,
    repo_root: Path,
) -> list[dict]:
    """Compare files in a baseline ZIP against the current repo and generate changes.

    For each file that exists in both the ZIP and the repo:
    - If content differs → generate replace_block with old/new content

    For each file that exists in the repo but not in the ZIP:
    - Generate create_file

    For each file that exists in the ZIP but not in the repo:
    - Generate create_file (treat as new since it was in baseline)

    Args:
        zip_bytes: Raw ZIP file contents (must contain files/ directory).
        repo_root: Repository root to compare against.

    Returns:
        List of change dicts suitable for generate_patch().
    """
    buf = io.BytesIO(zip_bytes)
    changes = []

    with zipfile.ZipFile(buf, "r") as zf:
        # Check if this ZIP has a files/ directory (baseline snapshot)
        all_names = zf.namelist()
        has_files = any(n.startswith("files/") for n in all_names)

        if not has_files:
            raise ValueError(
                "ZIP-filen inneholder ikke en fil-snapshot (files/-mappen). "
                "Dette er en eldre ZIP uten filinnhold. "
                "Generer en ny ZIP for å få med filene."
            )

        # Get all files from the ZIP
        zip_files = {}
        for name in all_names:
            if name.startswith("files/") and not name.endswith("/"):
                rel_path = name[len("files/"):]
                zip_files[rel_path] = zf.read(name)

        # Get all current repo files
        current_hashes = compute_file_hashes(repo_root)

        # Check each repo file against ZIP baseline
        for rel_path in sorted(current_hashes.keys()):
            full_path = repo_root / rel_path
            if not full_path.exists() or not full_path.is_file():
                continue

            current_content = full_path.read_bytes()

            if rel_path in zip_files:
                # File exists in both — check if changed
                if current_content != zip_files[rel_path]:
                    # Content differs → replace_block
                    old_text = zip_files[rel_path].decode("utf-8", errors="replace")
                    new_text = current_content.decode("utf-8", errors="replace")

                    hunk = build_hunk_string(old_text, new_text)
                    changes.append({
                        "file": rel_path,
                        "action": "patch_hunk",
                        "hunk": hunk,
                    })
            else:
                # File exists in repo but not in ZIP — new file
                new_text = current_content.decode("utf-8", errors="replace")
                changes.append({
                    "file": rel_path,
                    "action": "create_file",
                    "content": new_text,
                })

        # Check for files in ZIP that no longer exist in repo
        for rel_path in sorted(zip_files.keys()):
            if rel_path not in current_hashes:
                # File was in baseline but is gone now — create it back
                content = zip_files[rel_path].decode("utf-8", errors="replace")
                changes.append({
                    "file": rel_path,
                    "action": "create_file",
                    "content": content,
                })

    return changes


# ─── Patch Generation (Export) ─────────────────────────────────────────


def generate_patch(
    changes: list[dict],
    repo_root: Path,
    side: str = "a",
    description: str = "",
) -> str:
    """Generate a complete YAML patch string from a list of changes.

    Args:
        changes: List of change dicts with file, action, find/replace/content.
        repo_root: Repository root for fingerprinting.
        side: Which side generated this patch ('a' or 'b').
        description: Optional human-readable description.

    Returns:
        Complete YAML patch as a string, ready to transfer.
    """
    fingerprint = compute_tree_fingerprint(repo_root)
    tree_snapshot = compute_file_structure_snapshot(repo_root)

    patch = {
        "version": 1,
        "source_side": side,
        "source_tree_fingerprint": fingerprint,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "description": description,
        "source_tree_structure": tree_snapshot,
        "changes": changes,
    }

    return yaml.dump(patch, default_flow_style=False, allow_unicode=True)


# ─── Diff Preview ──────────────────────────────────────────────────────


def build_preview_diff(patch: dict, repo_root: Path) -> str:
    """Generate a unified diff preview for all changes in a patch."""
    all_diffs = []

    for change in patch["changes"]:
        file_path = change["file"]
        full_path = safe_relative_path(repo_root, file_path)
        action = change["action"]

        old = full_path.read_text(encoding="utf-8") if full_path.exists() else ""

        if action == "patch_hunk":
            find, replace = parse_hunk(change["hunk"])
            new = old.replace(find, replace, 1)
        elif action == "create_file":
            new = change["content"]
        elif action == "append_to_file":
            new = old + "\n" + change["content"]
        else:
            continue

        diff = difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="",
        )

        all_diffs.extend(diff)
        all_diffs.append("")

    return "\n".join(all_diffs)


# ─── Patch Application ─────────────────────────────────────────────────


def apply_patch(patch: dict, repo_root: Path) -> None:
    """Apply all changes in a patch with automatic backup.

    Also records the patch in history with before/after file hashes
    for tamper detection on future patches.
    """
    # Capture file hashes BEFORE applying
    file_hashes_before = compute_file_hashes(repo_root)

    backup_dir = (
        repo_root
        / "_code_mover_backups"
        / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    backup_dir.mkdir(parents=True, exist_ok=True)

    for change in patch["changes"]:
        file_path = change["file"]
        full_path = safe_relative_path(repo_root, file_path)
        action = change["action"]

        # Backup existing file
        if full_path.exists():
            backup_path = backup_dir / file_path
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(full_path, backup_path)

        old = full_path.read_text(encoding="utf-8") if full_path.exists() else ""

        if action == "patch_hunk":
            find, replace = parse_hunk(change["hunk"])
            new = old.replace(find, replace, 1)
        elif action == "create_file":
            full_path.parent.mkdir(parents=True, exist_ok=True)
            new = change["content"]
        elif action == "append_to_file":
            new = old + "\n" + change["content"]
        else:
            raise ValueError(f"Ugyldig action: {action}")

        full_path.write_text(new, encoding="utf-8")

    # Capture file hashes AFTER applying
    file_hashes_after = compute_file_hashes(repo_root)

    # Record in history
    add_patch_to_history(
        repo_root,
        patch,
        status="applied",
        file_hashes_before=file_hashes_before,
        file_hashes_after=file_hashes_after,
    )
