from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote


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

_WORKSPACE_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _is_unc_path(path: Path) -> bool:
    return str(path).startswith("\\\\")


def _is_reparse_point(path: Path) -> bool:
    try:
        attrs = path.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return bool(attrs & 0x400)


def _reject_linked_path(path: Path) -> None:
    for item in [path, *path.parents]:
        try:
            if item.exists() and (item.is_symlink() or _is_reparse_point(item)):
                raise ValueError("Symlinked or reparse-point paths are not allowed.")
        except OSError as exc:
            raise ValueError("Path could not be inspected safely.") from exc


def _resolve_for_write(path: Path) -> Path:
    parent = path.parent.expanduser().resolve()
    return (parent / path.name).resolve(strict=False)


def ensure_local_path(path: str | Path, *, allow_network: bool = False, reject_symlink: bool = False) -> Path:
    candidate = Path(path).expanduser()
    if _is_unc_path(candidate) and not allow_network:
        raise ValueError("Network paths are not allowed.")
    if reject_symlink:
        _reject_linked_path(candidate)
    if candidate.exists():
        resolved = candidate.resolve()
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


def validate_workspace_slug(slug: str) -> str:
    candidate = str(slug or "").strip()
    if not candidate:
        raise ValueError("Workspace slug is required.")
    if not _WORKSPACE_SLUG_RE.fullmatch(candidate):
        raise ValueError(
            "Workspace slug must be 1-63 lowercase ASCII letters, numbers, or hyphens, "
            "and must start and end with a letter or number."
        )
    if "." in candidate or "/" in candidate or "\\" in candidate or "\x00" in candidate:
        raise ValueError("Workspace slug contains invalid path characters.")
    if candidate.split(".", 1)[0].lower() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("Reserved device workspace slug is not allowed.")
    return candidate


def _profile_import_roots(profile: object, extra_roots: list[str | Path] | None = None) -> list[Path]:
    roots: list[Path] = []
    for attr in ("documents_root", "archives_root", "archive_root", "attachments_root", "uploads_root", "upload_staging_root"):
        value = getattr(profile, attr, None)
        if value:
            roots.append(Path(value))
    if extra_roots:
        roots.extend(Path(root) for root in extra_roots)
    return roots


def validate_user_import_path(
    path: str | Path,
    profile: object,
    *,
    roots: list[str | Path] | None = None,
    allow_create: bool = False,
) -> Path:
    candidate = Path(path)
    if _is_unc_path(candidate):
        raise ValueError("Network paths are not allowed.")
    return ensure_path_within_roots(
        candidate,
        roots=_profile_import_roots(profile, roots),
        allow_network=False,
        reject_symlink=True,
        allow_create=allow_create,
    )


def safe_content_disposition(filename: str) -> dict[str, str]:
    cleaned = sanitize_filename(str(filename or "download"))
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in {'"', "\\", ";"} else "_" for ch in cleaned)
    ascii_name = ascii_name.strip(" .") or "download"
    encoded = quote(cleaned, safe="")
    return {"Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'}


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
