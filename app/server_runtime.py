from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.identity import IdentityService
from app.server_platform import PostgresPlatformStore, ServerHealthConnection
from app.types import ModelInfoSnapshot, ProfileSession, RuntimeSnapshot


@dataclass(slots=True)
class UnsupportedServerComponent:
    name: str

    def __getattr__(self, item: str):
        raise RuntimeError(
            f"Server mode component '{self.name}' does not implement '{item}'. "
            "This restricted server path supports thread/auth workflows only."
        )


@dataclass(slots=True)
class ServerRuntimeSurface:
    snapshot: RuntimeSnapshot


class ServerRuntime:
    def __init__(self, platform: PostgresPlatformStore, workspace_slug: str | None = None) -> None:
        self.platform = platform
        self.identity_service = IdentityService(platform)
        self.active_profile = platform.get_profile(workspace_slug) if workspace_slug else None
        if self.active_profile is None:
            profiles = platform.list_profiles()
            self.active_profile = profiles[0] if profiles else platform.ensure_default_profile(
                settings.profile_root,
                settings.backup_root,
                settings.db_path,
            )
        self.profile_session = ProfileSession(profile_slug=self.active_profile.slug, unlocked=True)
        self.memory = UnsupportedServerComponent("memory")
        self.health_connection = ServerHealthConnection(platform)
        self.scheduler_service = None
        self.audit_chain_ok = True
        self.audit_chain_reason = None
        self._using_locked_scaffold = False
        self.snapshot = RuntimeSnapshot(
            product_posture=settings.product_posture,
            policy_mode=settings.policy_mode,
            active_profile=self.active_profile,
            profile_session=self.profile_session,
            model_info=ModelInfoSnapshot(app_version="server-mode"),
            llm_available=False,
            runtime_degraded_reasons=[
                "Server mode is restricted to persisted thread/auth workflows until document evidence parity is implemented."
            ],
        )
        self.orchestrator = ServerRuntimeSurface(self.snapshot)

    async def start(self) -> None:
        self.platform.ping()
        if settings.redis_url:
            try:
                import redis
            except Exception as exc:  # pragma: no cover - dependency/env guard
                raise RuntimeError("Server mode requires redis.") from exc
            redis.Redis.from_url(settings.redis_url).ping()

    async def stop(self) -> None:
        return None

    async def _refresh_platform_snapshot(self) -> None:
        self.snapshot.active_profile = self.active_profile
        self.snapshot.profile_session = self.profile_session

    def verify_audit_chain(self, source: str = "server_runtime"):
        return self.platform.verify_audit_chain(source)


class ServerRuntimeManager:
    def __init__(self) -> None:
        self.platform = PostgresPlatformStore(settings.postgres_dsn, audit_enabled=settings.audit_enabled)
        self.identity_service = IdentityService(self.platform)
        default_profile = self.platform.ensure_default_profile(
            settings.profile_root,
            settings.backup_root,
            settings.db_path,
        )
        self.default_workspace_slug = default_profile.slug
        self._runtimes: dict[str, ServerRuntime] = {}

    async def start(self) -> None:
        await self.get_runtime(self.default_workspace_slug)

    async def stop(self) -> None:
        self._runtimes.clear()

    async def get_runtime(self, workspace_slug: str | None = None) -> ServerRuntime:
        slug = workspace_slug or self.default_workspace_slug
        runtime = self._runtimes.get(slug)
        if runtime is None:
            runtime = ServerRuntime(self.platform, slug)
            await runtime.start()
            self._runtimes[slug] = runtime
        return runtime

    def list_workspaces(self):
        return self.platform.list_profiles()
