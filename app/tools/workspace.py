from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from app.tools.base import Tool
from app.types import ToolRequest, ToolResult

_WINDOWS = sys.platform == "win32"


def _is_within_boundary(path: Path, root: Path) -> bool:
    """Check if resolved path is within the workspace root, handling Windows case-insensitivity and UNC paths."""
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        return False
    # Reject UNC paths on Windows
    if _WINDOWS and str(resolved).startswith("\\\\"):
        return False
    resolved_root = root.resolve()
    # Use case-insensitive comparison on Windows
    if _WINDOWS:
        return str(resolved).lower().startswith(str(resolved_root).lower() + "\\") or str(resolved).lower() == str(resolved_root).lower()
    try:
        return resolved.is_relative_to(resolved_root)
    except AttributeError:
        # Python < 3.9 fallback
        return str(resolved).startswith(str(resolved_root) + "/") or resolved == resolved_root


class SearchFilesTool(Tool):
    name = "search_files"

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path.cwd()).resolve()

    def availability(self) -> tuple[bool, str | None]:
        if not self.root.exists() or not self.root.is_dir():
            return False, f"Workspace root is unavailable: {self.root}."
        return True, None

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "File name search phrase"},
            },
            "required": ["query"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        query = str(request.arguments.get("query", "")).strip().lower()
        if not query:
            return ToolResult(
                success=False,
                status="failed",
                display_text="I need a search phrase.",
            )
        matches: list[str] = []
        for path in self.root.rglob("*"):
            if path.is_file() and query in path.name.lower():
                matches.append(str(path))
            if len(matches) >= 5:
                break
        if not matches:
            return ToolResult(
                success=True,
                status="observed",
                display_text=f"No files matched {query}.",
                evidence=[f"Searched under {self.root}."],
                data={"matches": []},
            )
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Found {len(matches)} matching file(s).",
            evidence=[f"Searched under {self.root}."],
            data={"matches": matches},
            suggested_follow_up="Ask me to inspect one of those files.",
        )


class ReadFileExcerptTool(Tool):
    name = "read_file_excerpt"

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path.cwd()).resolve()

    def availability(self) -> tuple[bool, str | None]:
        if not self.root.exists() or not self.root.is_dir():
            return False, f"Workspace root is unavailable: {self.root}."
        return True, None

    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read"},
            },
            "required": ["path"],
        }

    async def run(self, request: ToolRequest) -> ToolResult:
        raw_path = str(request.arguments.get("path", "")).strip()
        if not raw_path:
            return ToolResult(
                success=False,
                status="failed",
                display_text="I need a file path.",
            )
        path = Path(raw_path)
        if not path.is_absolute():
            path = (self.root / path).resolve()
        else:
            path = path.resolve()
        if not _is_within_boundary(path, self.root):
            return ToolResult(
                success=False,
                status="failed",
                display_text="That file is outside the workspace safety boundary.",
            )
        if not path.exists() or not path.is_file():
            return ToolResult(
                success=False,
                status="failed",
                display_text=f"I could not find {path}.",
            )
        content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        excerpt = "\n".join(content[:20]).strip()
        return ToolResult(
            success=True,
            status="observed",
            display_text=f"Read {path.name}.",
            evidence=[str(path)],
            data={"path": str(path), "excerpt": excerpt},
        )
