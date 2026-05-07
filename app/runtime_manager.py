from __future__ import annotations

import asyncio
from collections.abc import Iterable

from app.config import settings
from app.identity import IdentityService
from app.path_safety import validate_workspace_slug
from app.platform import PlatformStore, connect_platform_db
from app.runtime import KernRuntime
from app.types import ProfileSummary


class RuntimeManager:
    def __init__(self) -> None:
        self.platform = PlatformStore(connect_platform_db(settings.system_db_path), audit_enabled=settings.audit_enabled)
        self.identity_service = IdentityService(self.platform)
        default_profile = self.platform.ensure_default_profile(
            profile_root=settings.profile_root,
            backup_root=settings.backup_root,
            legacy_db_path=settings.db_path,
            title="Primary profile",
            slug="default",
        )
        self.default_workspace_slug = default_profile.slug
        self._lock = asyncio.Lock()
        self._runtimes: dict[str, KernRuntime] = {}

    async def start(self) -> None:
        await self.get_runtime(self.default_workspace_slug)

    async def stop(self) -> None:
        async with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            await runtime.stop()
        self.platform.connection.close()

    async def get_runtime(self, workspace_slug: str | None = None) -> KernRuntime:
        slug = validate_workspace_slug((workspace_slug or self.default_workspace_slug).strip() or self.default_workspace_slug)
        async with self._lock:
            runtime = self._runtimes.get(slug)
            if runtime is None:
                runtime = KernRuntime(profile_slug=slug)
                await runtime.start()
                self._runtimes[slug] = runtime
            elif not getattr(runtime, "_started", False):
                await runtime.start()
            return runtime

    async def stop_runtime(self, workspace_slug: str) -> None:
        async with self._lock:
            runtime = self._runtimes.pop(workspace_slug, None)
        if runtime is not None:
            await runtime.stop()

    def list_workspaces(self) -> list[ProfileSummary]:
        return self.platform.list_profiles()

    def resolve_workspace_slug(self, preferred_slug: str | None = None) -> str:
        slug = (preferred_slug or "").strip()
        if slug:
            slug = validate_workspace_slug(slug)
            profile = self.platform.get_profile(slug)
            if profile is not None:
                return profile.slug
        return self.default_workspace_slug

    def workspace_slugs(self) -> Iterable[str]:
        for profile in self.list_workspaces():
            yield profile.slug
