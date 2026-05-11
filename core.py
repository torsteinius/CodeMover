"""Core logic for Code Mover.

Handles:
- Repository detection
- Git-based patch generation (git format-patch)
- Git-based patch application (git am)
- Sync-state tracking (last synced commit hash)
- Patch history
- ZIP export/import
"""

from pathlib import Path
import re
import json
import zipfile
import io
import subprocess
import tempfile
import os
import time
from datetime import datetime
from typing import Optional

APP_ROOT_MARKERS = [
    "app.py",
    "core.py",
]

PATCH_HISTORY_DIR = "_code_mover_patches"
SYNC_STATE_FILE   = "sync.json"

# Extensions considered transferable text files (used for display / ls-files fallback)
TEXT_EXTENSIONS = {
    ".py", ".pyi", ".pyx",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".json",
    ".html", ".css", ".js", ".ts", ".jsx", ".tsx",
    ".md", ".txt", ".rst",
    ".sh", ".bash", ".zsh",
    ".sql", ".xml", ".csv", ".tsv",
}

TEXT_FILENAMES = {
    "Dockerfile", "Makefile", "Procfile",
    "requirements.txt", ".gitignore", ".gitattributes", ".env",
}

SKIP_DIRS = {
    "__pycache__", "_code_mover_backups", "_code_mover_patches",
    "node_modules", ".venv", "venv", "env",
}

# Extensions that are NEVER transferred — regardless of git tracking
NEVER_TRANSFER_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd",
    ".so", ".dll", ".dylib", ".a", ".lib",
    ".exe", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp", ".svg",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv",
    ".db", ".sqlite", ".sqlite3",
    ".ttf", ".otf", ".woff", ".woff2",
}


# ─── Git subprocess helper ───────────────────────────────────────────────


def _run_git(*args, cwd: str) -> subprocess.CompletedProcess:
    """Run a git command and guarantee stdout/stderr are always str, never None.

    With capture_output=True + text=True Python should always give strings, but
    on some platforms or when git exits via signal the pipe can come back None.
    Normalising here means every caller can safely call .strip()/.splitlines().
    """
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.stdout is None:
        result.stdout = ""
    if result.stderr is None:
        result.stderr = ""
    return result


# ─── Helpers ────────────────────────────────────────────────────────────


def is_text_file(path: Path) -> bool:
    if path.name in TEXT_FILENAMES:
        return True
    return path.suffix.lower() in TEXT_EXTENSIONS


def _is_excluded_path(rel_str: str, parts: tuple) -> bool:
    """True if this path should always be excluded from transfers."""
    if "_code_mover_backups" in rel_str or "_code_mover_patches" in rel_str:
        return True
    # Skip hidden dirs/files and known junk dirs
    if any(p.startswith(".") for p in parts):
        return True
    if any(p in SKIP_DIRS for p in parts):
        return True
    return False


def get_tracked_files(repo_root: Path) -> list[str]:
    """Return sorted list of transferable file paths relative to repo_root.

    Two-pass strategy so no transferable file is ever missed:

    Pass 1 — git ls-files
        Authoritative for all committed/staged files.  Respects .gitignore so
        build artefacts that are already excluded stay excluded.

    Pass 2 — recursive OS walk
        Always runs after Pass 1.  Ensures .py and .sql files are included
        even when they are brand-new and not yet tracked by git.  Any file
        whose extension is in TEXT_EXTENSIONS is also picked up here.

    Both passes hard-filter NEVER_TRANSFER_EXTENSIONS (including .pyc/.pyo)
    and SKIP_DIRS (including __pycache__).
    """
    seen: set[str] = set()

    # ── Pass 1: git ls-files ─────────────────────────────────────────────
    try:
        result = _run_git("ls-files", cwd=str(repo_root))
        if result.returncode == 0:
            for f in result.stdout.splitlines():
                f = f.strip()
                if not f:
                    continue
                p = Path(f)
                if p.suffix.lower() in NEVER_TRANSFER_EXTENSIONS:
                    continue
                if _is_excluded_path(f, p.parts):
                    continue
                if is_text_file(repo_root / f):
                    seen.add(f)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # ── Pass 2: OS walk — always runs, with timeout ──────────────────────
    # Picks up untracked .py/.sql (and all other TEXT_EXTENSIONS) files.
    # Uses os.walk(topdown=True) so we can prune skip-dirs BEFORE descending
    # into them — prevents hangs on node_modules, .venv, large build trees, etc.
    # Hard timeout: on slow disks we stop walking and return what git gave us.
    _WALK_TIMEOUT = 8.0   # seconds
    _walk_start   = time.monotonic()

    for dirpath, dirnames, filenames in os.walk(repo_root, topdown=True, followlinks=False):
        if time.monotonic() - _walk_start > _WALK_TIMEOUT:
            break  # return what we have; Pass 1 (git ls-files) still covered tracked files

        # Prune in-place: os.walk will not descend into removed entries
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".")
            and d not in SKIP_DIRS
            and d not in {"_code_mover_backups", "_code_mover_patches"}
        ]

        cur_dir = Path(dirpath)
        for filename in filenames:
            full_path = cur_dir / filename
            try:
                rel = full_path.relative_to(repo_root)
            except ValueError:
                continue
            rel_str = rel.as_posix()
            suffix  = full_path.suffix.lower()

            if suffix in NEVER_TRANSFER_EXTENSIONS:
                continue
            if filename.startswith("."):
                continue
            if is_text_file(full_path):
                seen.add(rel_str)

    return sorted(seen)


def compute_file_structure_snapshot(repo_root: Path) -> str:
    """Human-readable tree of tracked text files (for display only)."""
    lines = []
    for rel_str in get_tracked_files(repo_root):
        rel = Path(rel_str)
        depth = len(rel.parts) - 1
        indent = "  " * depth
        lines.append(f"{indent}📄 {rel.name}")
    return "\n".join(lines)


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
    """Return list of missing markers (empty = all OK)."""
    return [m for m in markers if not (repo_path / m).exists()]


def check_is_git_repo(repo_root: Path) -> bool:
    """Return True if repo_root is inside a git repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(repo_root),
        capture_output=True,
    )
    return result.returncode == 0


# ─── Git State ──────────────────────────────────────────────────────────


def get_current_commit(repo_root: Path) -> str:
    """Return the current HEAD commit hash (full SHA)."""
    result = _run_git("rev-parse", "HEAD", cwd=str(repo_root))
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD feilet: {result.stderr.strip()}")
    return result.stdout.strip()


def get_commits_since(repo_root: Path, since_hash: str) -> list[dict]:
    """Return commits reachable from HEAD but not from since_hash, oldest first."""
    result = _run_git(
        "log", f"{since_hash}..HEAD", "--oneline", "--no-decorate", "--reverse",
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        raise RuntimeError(f"git log feilet: {result.stderr.strip()}")
    commits = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            short_hash, _, message = line.partition(" ")
            commits.append({"hash": short_hash, "message": message})
    return commits


def get_uncommitted_files(repo_root: Path) -> list[dict]:
    """Return files with uncommitted changes (staged + unstaged)."""
    result = _run_git("status", "--porcelain", cwd=str(repo_root))
    if result.returncode != 0:
        return []
    files = []
    for line in result.stdout.splitlines():
        if line.strip():
            status = line[:2].strip()
            path = line[3:].strip()
            files.append({"status": status, "path": path})
    return files


def get_changed_files_since(repo_root: Path, since_hash: str) -> list[str]:
    """Return list of transferable files changed in commits since since_hash.

    Hard-filters:
    - NEVER_TRANSFER_EXTENSIONS (.pyc / .pyo / binaries / media / archives)
    - __pycache__ and other SKIP_DIRS directories
    - hidden path components
    """
    base = since_hash if since_hash else _EMPTY_TREE
    result = _run_git("diff", "--name-only", f"{base}..HEAD", cwd=str(repo_root))
    if result.returncode != 0:
        return []

    out = []
    for f in result.stdout.splitlines():
        f = f.strip()
        if not f:
            continue
        p = Path(f)
        if p.suffix.lower() in NEVER_TRANSFER_EXTENSIONS:
            continue
        if _is_excluded_path(f, p.parts):
            continue
        out.append(f)
    return sorted(out)



# ─── Sync State ─────────────────────────────────────────────────────────


def _sync_file(repo_root: Path) -> Path:
    return repo_root / PATCH_HISTORY_DIR / SYNC_STATE_FILE


def load_sync_state(repo_root: Path) -> dict:
    """Load sync state. Returns {} if no sync has been recorded yet."""
    f = _sync_file(repo_root)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_sync_state(repo_root: Path, commit_hash: str) -> None:
    """Record commit_hash as the last successfully synced commit."""
    f = _sync_file(repo_root)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({
        "last_synced_commit": commit_hash,
        "synced_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=2), encoding="utf-8")


# ─── Patch Generation ───────────────────────────────────────────────────

# The git empty-tree hash — diffing against this gives all files as new.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def get_file_content(repo_root: Path, file_path: str) -> str:
    """Read a file from the git working tree (HEAD version if committed, else working copy)."""
    full_path = repo_root / file_path
    if full_path.exists():
        return full_path.read_text(encoding="utf-8")
    # Fallback: try to read from git HEAD
    result = _run_git("show", f"HEAD:{file_path}", cwd=str(repo_root))
    if result.returncode != 0:
        raise RuntimeError(f"Kunne ikke lese {file_path}: {result.stderr.strip()}")
    return result.stdout


def export_selected_files(repo_root: Path, selected_files: list[str]) -> bytes:
    """Create a ZIP with the raw content of selected files as they are in HEAD.

    Returns ZIP bytes containing:
      files.json — manifest with file paths and metadata
      <file>     — each selected file as a separate entry
    """
    buf = io.BytesIO()
    manifest = {
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "files": [],
    }
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in selected_files:
            try:
                content = get_file_content(repo_root, file_path)
                zf.writestr(file_path, content)
                manifest["files"].append({
                    "path": file_path,
                    "size": len(content),
                })
            except Exception as e:
                manifest["files"].append({
                    "path": file_path,
                    "error": str(e),
                })
        zf.writestr("_manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    return buf.getvalue()


_FILE_SEPARATOR = "\n\n----============== {filnavn}\n\n"


def export_files_as_text(repo_root: Path, selected_files: list[str]) -> str:
    """Combine selected files into a single text block with file separators.

    Each file is prefixed with:
        ----============== <filnavn>
        PATH: <relativ sti fra repo-root>

    This makes it easy to copy-paste between environments without git.
    """
    parts = []
    for file_path in selected_files:
        try:
            content = get_file_content(repo_root, file_path)
            parts.append(
                f"----============== {file_path}\n"
                f"PATH: {file_path}\n"
                f"{content}"
            )
        except Exception as e:
            parts.append(
                f"----============== {file_path}\n"
                f"PATH: {file_path}\n"
                f"# ERROR: {e}"
            )
    return "\n\n".join(parts)



def parse_and_apply_files_text(text: str, target_dir: Path) -> list[dict]:
    """Parse a text block with ----============== separators and write files to disk.

    Args:
        text:        The text block from export_files_as_text.
        target_dir:  Root directory where files will be written.

    Returns:
        List of dicts with {path, status, error?} for each file.
    """
    results = []
    # Split on the separator pattern
    blocks = re.split(r"^----==============\s+(.+)$", text, flags=re.MULTILINE)
    # blocks[0] is preamble (empty or ignored)
    # blocks[1:] are alternating [filnavn, innhold, filnavn, innhold, ...]
    i = 1
    while i < len(blocks) - 1:
        file_path = blocks[i].strip()
        content = blocks[i + 1]
        i += 2

        if not file_path:
            continue

        # Strip leading newlines, then remove the PATH: header line added by
        # export_files_as_text (first non-empty line starting with "PATH: ").
        content = content.lstrip("\n")
        if content.startswith("PATH: "):
            content = content.split("\n", 1)[1] if "\n" in content else ""
        content = content.strip("\n")

        full_path = target_dir / file_path
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            results.append({"path": file_path, "status": "written"})
        except Exception as e:
            results.append({"path": file_path, "status": "error", "error": str(e)})

    return results




def generate_format_patch(repo_root: Path, since_hash: Optional[str] = None) -> str:
    """Generate a git format-patch covering commits since since_hash (or all commits).

    Args:
        repo_root:   Repository root.
        since_hash:  Last synced commit hash. If None, includes all commits.

    Returns:
        The full text output of 'git format-patch ... --stdout'.

    Raises:
        RuntimeError on git errors or if there is nothing to patch.
    """
    base = since_hash if since_hash else _EMPTY_TREE
    result = _run_git("format-patch", f"{base}..HEAD", "--stdout", cwd=str(repo_root))
    if result.returncode != 0:
        raise RuntimeError(f"git format-patch feilet:\n{result.stderr.strip()}")
    if not result.stdout.strip():
        raise RuntimeError("Ingen nye commits å patche siden sist sync.")
    return result.stdout


def generate_format_patch_for_files(
    repo_root: Path, since_hash: Optional[str], selected_files: list[str]
) -> str:
    """Generate a git format-patch covering only the selected files.

    Uses 'git format-patch <base>..HEAD --stdout -- <file1> <file2> ...'
    to produce a patch that only includes diffs for the specified files.

    Args:
        repo_root:       Repository root.
        since_hash:      Last synced commit hash. If None, includes all commits.
        selected_files:  List of file paths (relative to repo root) to include.

    Returns:
        The full text output of 'git format-patch ... --stdout -- <files>'.

    Raises:
        RuntimeError on git errors or if there is nothing to patch.
    """
    base = since_hash if since_hash else _EMPTY_TREE
    result = _run_git(
        "format-patch", f"{base}..HEAD", "--stdout", "--", *selected_files,
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        raise RuntimeError(f"git format-patch feilet:\n{result.stderr.strip()}")
    if not result.stdout.strip():
        raise RuntimeError("Ingen endringer for valgte filer siden sist sync.")
    return result.stdout



# ─── Patch Preview ──────────────────────────────────────────────────────


def preview_format_patch(patch_text: str) -> dict:
    """Parse a git format-patch blob and return structured metadata for display.

    Returns a dict with:
        commits:       list of {hash, subject}
        files_changed: sorted list of unique file paths touched
    """
    commits: list[dict] = []
    files_changed: set[str] = set()
    current: dict = {}

    for line in patch_text.splitlines():
        # New commit block
        if line.startswith("From ") and len(line.split()) >= 3:
            if current:
                commits.append(current)
            current = {"hash": line.split()[1][:12], "subject": ""}
        # Subject line (may be prefixed with [PATCH n/m])
        elif line.startswith("Subject: ") and current is not None:
            subject = line[9:].strip()
            subject = re.sub(r"^\[PATCH[^\]]*\]\s*", "", subject)
            current["subject"] = subject
        # Diff file header
        elif line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+) b/(.+)", line)
            if m:
                files_changed.add(m.group(2))

    if current:
        commits.append(current)

    return {
        "commits": commits,
        "files_changed": sorted(files_changed),
    }


# ─── Patch Application ──────────────────────────────────────────────────


def apply_format_patch(repo_root: Path, patch_text: str) -> str:
    """Apply a git format-patch blob using 'git am'.

    Args:
        repo_root:   Repository root (must be a git repo).
        patch_text:  The full text from generate_format_patch / import.

    Returns:
        stdout from git am on success.

    Raises:
        RuntimeError with git am output on failure (also aborts the am session).
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(patch_text)
        tmp_path = tf.name

    try:
        result = _run_git("am", "--keep-cr", "--3way", tmp_path, cwd=str(repo_root))
        if result.returncode != 0:
            subprocess.run(
                ["git", "am", "--abort"],
                cwd=str(repo_root), capture_output=True,
            )
            raise RuntimeError(
                f"git am feilet:\n{result.stdout.strip()}\n{result.stderr.strip()}"
            )
        return result.stdout.strip()
    finally:
        os.unlink(tmp_path)


# ─── ZIP Export / Import ────────────────────────────────────────────────


def export_patch_to_zip(patch_text: str, metadata: dict) -> bytes:
    """Wrap a git format-patch and its metadata in a ZIP for easy transfer.

    ZIP contents:
        patch.patch   — the git format-patch output
        metadata.json — side, timestamp, commit range info
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("patch.patch", patch_text)
        zf.writestr("metadata.json", json.dumps(metadata, indent=2, ensure_ascii=False))
    return buf.getvalue()


def import_patch_from_zip(zip_bytes: bytes) -> tuple[str, dict]:
    """Extract patch text and metadata from a ZIP.

    Returns:
        Tuple of (patch_text, metadata_dict).
    """
    buf = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(buf, "r") as zf:
        names = zf.namelist()
        if "patch.patch" not in names:
            raise ValueError("Ugyldig ZIP: mangler patch.patch")
        patch_text = zf.read("patch.patch").decode("utf-8")
        metadata = {}
        if "metadata.json" in names:
            metadata = json.loads(zf.read("metadata.json").decode("utf-8"))
    return patch_text, metadata


# ─── Patch History ──────────────────────────────────────────────────────


def _history_file(repo_root: Path) -> Path:
    return repo_root / PATCH_HISTORY_DIR / "history.json"


def load_patch_history(repo_root: Path) -> list[dict]:
    f = _history_file(repo_root)
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_patch_history(repo_root: Path, history: list[dict]) -> None:
    f = _history_file(repo_root)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def add_to_history(
    repo_root: Path,
    status: str,          # "generated" | "applied"
    side: str,            # "a" | "b"
    since_hash: str,
    head_hash: str,
    commits: list[dict],
    description: str = "",
) -> str:
    """Append a record to patch history. Returns the patch_id."""
    history = load_patch_history(repo_root)
    patch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    history.append({
        "patch_id": patch_id,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "side": side,
        "since_commit": since_hash,
        "head_commit": head_hash,
        "commit_count": len(commits),
        "commits": commits,
        "description": description,
    })
    save_patch_history(repo_root, history)
    return patch_id


def get_patch_history_summary(repo_root: Path) -> list[dict]:
    """Return history entries newest-first."""
    return list(reversed(load_patch_history(repo_root)))
