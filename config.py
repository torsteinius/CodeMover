"""Configuration management for Code Mover.

Handles:
- Which side (a/b) this instance is on
- Registered repositories with custom markers
- Active repo selection
"""

from pathlib import Path
import yaml
from typing import Optional

DEFAULT_CONFIG_PATH = Path.home() / ".code_mover_config.yaml"

DEFAULT_CONFIG = {
    "side": "a",
    "repos": [],
    "active_repo": None,
}


def get_default_config_path() -> Path:
    return DEFAULT_CONFIG_PATH


def load_config(config_path: Optional[Path] = None) -> dict:
    """Load configuration from YAML file. Returns defaults if file doesn't exist."""
    path = config_path or DEFAULT_CONFIG_PATH

    if not path.exists():
        return dict(DEFAULT_CONFIG)

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # Ensure all default keys exist
    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, value)

    return config


def save_config(config: dict, config_path: Optional[Path] = None) -> None:
    """Save configuration to YAML file."""
    path = config_path or DEFAULT_CONFIG_PATH

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)


def set_side(side: str, config_path: Optional[Path] = None) -> dict:
    """Set which side this instance is on ('a' or 'b')."""
    if side not in ("a", "b"):
        raise ValueError("Side must be 'a' or 'b'")

    config = load_config(config_path)
    config["side"] = side
    save_config(config, config_path)
    return config


def get_side(config_path: Optional[Path] = None) -> str:
    """Get the configured side for this instance."""
    config = load_config(config_path)
    return config.get("side", "a")


def add_repo(
    name: str,
    path: str,
    markers: Optional[list[str]] = None,
    config_path: Optional[Path] = None,
) -> dict:
    """Register a repository in the configuration."""
    config = load_config(config_path)

    # Default markers if not specified
    if markers is None:
        markers = ["app.py", "core.py"]

    repo_entry = {
        "name": name,
        "path": str(Path(path).resolve()),
        "markers": markers,
    }

    # Replace if name already exists, otherwise append
    existing = [r for r in config["repos"] if r["name"] == name]
    if existing:
        config["repos"] = [
            repo_entry if r["name"] == name else r for r in config["repos"]
        ]
    else:
        config["repos"].append(repo_entry)

    save_config(config, config_path)
    return config


def remove_repo(name: str, config_path: Optional[Path] = None) -> dict:
    """Remove a repository from configuration."""
    config = load_config(config_path)
    config["repos"] = [r for r in config["repos"] if r["name"] != name]

    if config.get("active_repo") == name:
        config["active_repo"] = None

    save_config(config, config_path)
    return config


def set_active_repo(name: Optional[str], config_path: Optional[Path] = None) -> dict:
    """Set the active repository. Pass None to deselect."""
    config = load_config(config_path)

    if name is not None:
        repo_names = [r["name"] for r in config["repos"]]
        if name not in repo_names:
            raise ValueError(f"Repo '{name}' is not registered. Available: {repo_names}")

    config["active_repo"] = name
    save_config(config, config_path)
    return config


def get_active_repo(config_path: Optional[Path] = None) -> Optional[dict]:
    """Get the active repository entry, or None if none selected."""
    config = load_config(config_path)
    active_name = config.get("active_repo")

    if not active_name:
        return None

    for repo in config["repos"]:
        if repo["name"] == active_name:
            return repo

    return None


def discover_repos(
    search_paths: Optional[list[Path]] = None,
    require_git: bool = True,
) -> list[dict]:
    """Scan directories for potential repositories.

    Looks at direct children of each search path and identifies
    directories that look like git repos (have .git folder) or
    are simply non-hidden directories.

    Args:
        search_paths: List of directories whose children to scan.
            Defaults to common dev directories.
        require_git: If True, only include directories with .git.
            If False, include all non-hidden subdirectories.

    Returns:
        List of dicts with 'name', 'path', 'has_git' keys.
    """
    if search_paths is None:
        home = Path.home()
        search_paths = [
            home / "Documents/GitHub",
            home / "Documents/projects",
            Path.cwd(),
        ]

    # Directories to always skip
    SKIP_DIRS = {
        "__pycache__", "_code_mover_backups", "_code_mover_patches",
        "node_modules", ".venv", "venv", "env", ".git",
        "Old Firefox Data",
    }

    found = []
    seen_paths = set()

    for search_path in search_paths:
        if not search_path.exists() or not search_path.is_dir():
            continue

        # Check the search_path itself
        if search_path.is_dir() and not search_path.name.startswith("."):
            resolved = str(search_path.resolve())
            if resolved not in seen_paths and search_path.name not in SKIP_DIRS:
                has_git = (search_path / ".git").exists()
                if not require_git or has_git:
                    seen_paths.add(resolved)
                    found.append({
                        "name": search_path.name,
                        "path": resolved,
                        "has_git": has_git,
                    })

        # Check children of the search path
        for child in sorted(search_path.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in SKIP_DIRS:
                continue

            resolved = str(child.resolve())
            if resolved in seen_paths:
                continue

            has_git = (child / ".git").exists()
            if not require_git or has_git:
                seen_paths.add(resolved)
                found.append({
                    "name": child.name,
                    "path": resolved,
                    "has_git": has_git,
                })

    return found
