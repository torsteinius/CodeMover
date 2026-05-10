# Code Mover

Safe patch-based code transfer for isolated/offline environments.

## Purpose

Code Mover is a Streamlit application designed for environments where code cannot be pushed directly through Git, CI/CD, or external services — such as air-gapped government networks, isolated infrastructure, or offline systems.

Instead of transferring entire files, Code Mover transfers precise, validated patches. This reduces transfer size, LLM token usage, risk of accidental overwrites, and manual copy/paste work.

---

## How It Works

Code Mover uses a **two-sided model**:

- **Side A (Sender)** — has access to an LLM or external tools. Generates a structured YAML patch describing what changed.
- **Side B (Receiver)** — the isolated environment. Receives the patch, validates it, previews changes, and applies it.

```
LLM (external)
    ↓
Generate structured YAML patch
    ↓
Transfer manually (copy/paste or ZIP file)
    ↓
Code Mover on Side B
    ↓
Validate → Preview → Backup → Apply
```

---

## Core Principles

**1. Transfer intent, not files**
Send only the exact changes, not entire file contents.

**2. Validate everything before writing**
Every patch must pass YAML validation, path validation, repository validation, and exact block-match validation before any file is touched.

**3. Fail closed**
If anything is uncertain — missing file, multiple block matches, invalid path, malformed YAML — patching stops immediately. No partial patching is allowed.

---

## Supported Actions

### `patch_hunk`
Replace a block of code using unified diff notation. Lines starting with `-` are removed, lines starting with `+` are added, and lines starting with a space are context (used to tighten the match). The `-` block must match exactly once in the target file.

```yaml
- file: "panels/fleet_panel.py"
  action: "patch_hunk"
  hunk: |
    @@ -15,1 +15,1 @@
    -imo = st.text_input("IMO")
    +imo = normalize_imo(st.text_input("IMO"))
```

With context lines for more precise matching:

```yaml
- file: "panels/fleet_panel.py"
  action: "patch_hunk"
  hunk: |
    @@ -14,3 +14,3 @@
     def render_fleet():
    -    imo = st.text_input("IMO")
    +    imo = normalize_imo(st.text_input("IMO"))
         st.write(imo)
```

### `create_file`
Create a new file. Refuses to overwrite existing files. Creates missing parent directories automatically.

```yaml
- file: "core/utils/example.py"
  action: "create_file"
  content: |
    def hello():
        print("hello")
```

### `append_to_file`
Append content to the end of an existing file.

```yaml
- file: "requirements.txt"
  action: "append_to_file"
  content: |
    pyyaml
```

---

## Patch Format

A patch is a YAML document with a version, description, and a list of changes:

```yaml
version: 1
description: "Add IMO normalization"

changes:
  - file: "core/fleet/imo_lookup.py"
    action: "create_file"
    content: |
      def normalize_imo(value: str) -> str:
          return str(value).replace("IMO", "").strip()

  - file: "panels/fleet_panel.py"
    action: "patch_hunk"
    hunk: |
      @@ -15,1 +15,1 @@
      -imo = st.text_input("IMO")
      +imo = normalize_imo(st.text_input("IMO"))
```

---

## Streamlit Workflow

### Side A — Generate

1. Select active repository
2. Optionally upload a baseline ZIP from a previous transfer to generate a diff-based patch
3. If no baseline ZIP, optionally send all files
4. Add a description and click **Generate patch**
5. Download the patch as `.yaml` or `.zip` (recommended — includes file hashes for tamper detection)

### Side B — Apply

1. Select active repository
2. Paste YAML or upload ZIP
3. Click **Validate patch** — shows a unified diff preview
4. Click **Apply patch** — backups are created, changes are written

---

## Safety Features

**Path validation** — rejects `../`, absolute paths, and paths outside the repository root.

**Exact match validation** — for `patch_hunk`, the `-` lines must match exactly one time in the target file. This prevents patch drift and accidental replacements.

**Atomic apply** — the entire patch is validated before any file is written. If change #8 fails, changes #1–7 are not applied.

**Automatic backups** — before applying, all affected files are backed up to `_code_mover_backups/YYYYMMDD_HHMMSS/`.

**Tree fingerprinting** — a hash of the repository structure is embedded in generated patches, so Side B can verify it received a patch intended for the correct repo state.

**Tamper detection** — file hashes are embedded in ZIP exports. Side B detects any files that changed unexpectedly since the last patch.

---

## Repository Orientation

Code Mover identifies the repository root by walking upward from the current directory until it finds a folder containing all configured markers (default: `app.py`, `core.py`). Custom markers can be set per repo in the sidebar.

If the repository root cannot be found, patching stops immediately.

---

## Multi-Repo Support

Multiple repositories can be registered in the sidebar. Each repo has a name, absolute path, and optional set of markers. The active repo can be switched at any time.

Use the **Discover repos** button to automatically scan common locations.

---

## Patch History

All generated and applied patches are recorded in `_code_mover_patches/`. The history tab shows:

- Timestamp and patch ID
- Source side and status (generated / applied / failed)
- Number of changes and description
- Per-file action summary

---

## Why YAML Instead of Git Diff

Traditional git patches are compact but fragile for LLM generation. Structured YAML is easier for LLMs to produce correctly, easier to validate programmatically, and more deterministic — especially in isolated environments where tooling may be limited.

---

## Installation

```bash
pip install -r requirements.txt
streamlit run app.py
```

**Requirements:** Python 3.10+, `streamlit>=1.28.0`, `pyyaml>=6.0`

---

## Suggested Future Features

- **Optional test execution** — run `pytest` automatically after applying a patch
- **Patch signing** — digitally sign patches to verify origin
- **Patch history database** — store timestamp, user, patch hash, changed files, and status
- **Rollback UI** — restore a previous backup snapshot directly from the Streamlit interface
