# LLM Patch Format — Code Mover

## Purpose

When you (an LLM) are asked to produce code changes for a repository that uses **Code Mover**, use this format. It is designed to be:

- **Easy for LLMs to generate** — no git required, no format-patch, no email-style headers
- **Easy for humans to read** — clear file boundaries, minimal boilerplate
- **Easy for Code Mover to apply** — the receiver side parses this format directly

---

## Format Specification

Each file is separated by a delimiter line. The delimiter contains the **relative file path** from the repository root.

```
----====== FILE: path/to/file.py
<file content here>
----====== FILE: path/to/another/file.ts
<file content here>
```

### Rules

1. **Delimiter**: `----====== FILE: <path>` on its own line (exactly 6 dashes, 6 equals signs, space, "FILE:", space, then the path).
2. **Path**: Relative to repository root. Use forward slashes (`/`) even on Windows.
3. **Content**: The full file content follows the delimiter line. Do not truncate or use `...` placeholders.
4. **No metadata**: No commit messages, no author info, no timestamps. Just files.
5. **Order**: Files should be listed in a logical order (e.g., dependencies first).
6. **Binary files**: Do not include binary files. Text files only.
7. **New files**: Include the full content. The receiver will create the file.
8. **Deleted files**: To indicate a file should be deleted, use:
   ```
   ----====== DELETE: path/to/file.py
   ```
9. **Multiple patches**: If you need to send multiple independent changes, separate them with a blank line and a comment:
   ```
   ----====== FILE: ...
   ...

   # --- Next patch ---

   ----====== FILE: ...
   ```

---

## Example

```patch
----====== FILE: src/utils/helpers.py
import json
from pathlib import Path


def load_config(path: Path) -> dict:
    """Load a JSON config file."""
    if not path.exists():
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save_config(path: Path, config: dict) -> None:
    """Save a JSON config file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)

----====== FILE: src/main.py
from pathlib import Path
from src.utils.helpers import load_config, save_config


def main() -> None:
    config = load_config(Path("config.json"))
    print(f"Loaded config with {len(config)} keys")
    config["version"] = "2.0"
    save_config(Path("config.json"), config)


if __name__ == "__main__":
    main()
```

---

## Instructions for LLMs

When a user asks you to generate a patch for Code Mover:

1. **Output the complete files** — do not skip content or use `# ... rest of file unchanged`. The receiver needs the full file to write it to disk.
2. **Use the exact delimiter format** — `----====== FILE: <path>`. No variations.
3. **Include ALL files** that need to change, even if only one line changed.
4. **New files** are created automatically — just include them in the patch.
5. **File deletion** — use `----====== DELETE: <path>` to indicate a file should be removed.
6. **Order matters** — list files so that dependencies come first (e.g., a helper module before the file that imports it).

---

## Why This Format?

| Feature | Git format-patch | LLM Patch Format |
|---------|-----------------|------------------|
| Easy for LLMs to generate | ❌ (complex headers, diff format) | ✅ (just file path + content) |
| Preserves git history | ✅ | ❌ |
| Human-readable | ⚠️ (diff noise) | ✅ (full files) |
| Requires git on receiver | ✅ | ❌ |
| Handles binary files | ✅ | ❌ (text only) |
| Easy to review | ⚠️ | ✅ |
