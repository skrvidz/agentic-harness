from __future__ import annotations

from pathlib import Path


def ensure_harness_dir(root: Path) -> Path:
    """Keep runtime data inside the chosen project and private to its owner."""
    path = root / ".harness"
    if path.is_symlink():
        raise ValueError(f"Refusing symlinked harness directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f"Invalid harness directory: {path}")
    path.chmod(0o700)
    if any(entry.is_symlink() for entry in path.rglob("*")):
        raise ValueError(f"Refusing symlink inside harness directory: {path}")
    return path
