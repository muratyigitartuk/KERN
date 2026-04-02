from __future__ import annotations

import os
from pathlib import Path


_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
}


def _is_unc_path(path: Path) -> bool:
    return str(path).startswith("\\\\")


def _resolve_for_write(path: Path) -> Path:
    parent = path.parent.expanduser().resolve()
    return (parent / path.name).resolve(strict=False)


def ensure_local_path(path: str | Path, *, allow_network: bool = False, reject_symlink: bool = False) -> Path:
    candidate = Path(path).expanduser()
    if _is_unc_path(candidate) and not allow_network:
        raise ValueError("Network paths are not allowed.")
    if candidate.exists():
        resolved = candidate.resolve()
        if reject_symlink and candidate.is_symlink():
            raise ValueError("Symlinked paths are not allowed.")
        return resolved
    return _resolve_for_write(candidate)


def ensure_path_within_roots(
    path: str | Path,
    *,
    roots: list[str | Path],
    allow_network: bool = False,
    reject_symlink: bool = False,
    allow_create: bool = False,
) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.exists() and not allow_create:
        raise ValueError("Path does not exist.")
    resolved = ensure_local_path(candidate, allow_network=allow_network, reject_symlink=reject_symlink)
    allowed_roots = [Path(root).expanduser().resolve() for root in roots]
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ValueError(f"Path is outside approved roots: {resolved}")


def sanitize_filename(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        raise ValueError("Filename is required.")
    if raw in {".", ".."} or "/" in raw or "\\" in raw or "\x00" in raw:
        raise ValueError("Filename contains invalid path characters.")
    basename = os.path.basename(raw).strip().rstrip(". ")
    if not basename:
        raise ValueError("Filename is empty after normalization.")
    stem = basename.split(".", 1)[0].lower()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError("Reserved device filename is not allowed.")
    return basename
