from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

from app.types import NetworkStatusSnapshot

if TYPE_CHECKING:
    from app.platform import PlatformStore


_KERN_SAFE_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _check_outbound(allowed_hosts: set[str] | None = None) -> tuple[str, int, list[str]]:
    allowed = {host.strip().lower() for host in (allowed_hosts or set()) if host.strip()}
    allowed.update(host.lower() for host in _KERN_SAFE_HOSTS)
    try:
        import psutil

        pid = None
        try:
            import os

            pid = os.getpid()
        except Exception as exc:
            logger.debug("PID lookup failed: %s", exc)
            return "unmonitored", 0, []
        if pid is not None:
            try:
                proc = psutil.Process(pid)
                conns = proc.net_connections(kind="inet")
            except Exception as exc:
                logger.debug("process-scoped net_connections failed; refusing system-wide fallback: %s", exc)
                return "unmonitored", 0, []
        else:
            return "unmonitored", 0, []

        external: list[str] = []
        for conn in conns:
            if conn.status not in {"ESTABLISHED", "SYN_SENT", "TIME_WAIT", "FIN_WAIT1", "FIN_WAIT2", "CLOSE_WAIT"}:
                continue
            raddr = conn.raddr
            if not raddr:
                continue
            host = str(raddr.ip)
            normalized_host = host.lower()
            if normalized_host in allowed:
                continue
            if host.startswith("127.") or host.startswith("::1"):
                continue
            external.append(f"{host}:{raddr.port}")
        return ("isolated" if not external else "network_detected"), len(external), external
    except ImportError:
        return "unmonitored", 0, []
    except Exception as exc:
        logger.debug("outbound connection check failed: %s", exc)
        return "unmonitored", 0, []


class NetworkMonitor:
    """Periodically checks KERN process for unexpected outbound network connections."""

    def __init__(
        self,
        platform: "PlatformStore",
        profile_slug: str,
        interval_seconds: int = 30,
        enabled: bool = True,
        allowed_hosts: set[str] | None = None,
    ) -> None:
        self.platform = platform
        self.profile_slug = profile_slug
        self.interval_seconds = interval_seconds
        self.enabled = enabled
        self.allowed_hosts = {host.strip().lower() for host in (allowed_hosts or set()) if host.strip()}
        self.allowed_hosts.update(host.lower() for host in _KERN_SAFE_HOSTS)
        self._status: NetworkStatusSnapshot = NetworkStatusSnapshot(
            outbound_connections=0,
            last_check=datetime.now(timezone.utc).isoformat(),
            status="checking",
            allowed_hosts=sorted(self.allowed_hosts),
        )
        self._lock = threading.Lock()
        self._last_check_time: float = 0.0

    @property
    def status(self) -> NetworkStatusSnapshot:
        with self._lock:
            return self._status

    def check(self) -> NetworkStatusSnapshot:
        """Run a network check if the interval has elapsed. Returns current status."""
        if not self.enabled:
            with self._lock:
                self._status = NetworkStatusSnapshot(
                    outbound_connections=0,
                    last_check=datetime.now(timezone.utc).isoformat(),
                    status="unmonitored",
                    allowed_hosts=sorted(self.allowed_hosts),
                )
            return self._status

        now = time.monotonic()
        if now - self._last_check_time < self.interval_seconds:
            return self._status

        self._last_check_time = now
        status_label, count, endpoints = _check_outbound(self.allowed_hosts)
        check_time = datetime.now(timezone.utc).isoformat()

        with self._lock:
            prev_status = self._status.status
            self._status = NetworkStatusSnapshot(
                outbound_connections=count,
                last_check=check_time,
                status=status_label,
                endpoints=endpoints,
                allowed_hosts=sorted(self.allowed_hosts),
            )

        if status_label == "network_detected" and prev_status != "network_detected":
            self.platform.record_audit(
                "network",
                "outbound_detected",
                "warning",
                f"Unexpected outbound connections detected: {', '.join(endpoints[:5])}",
                profile_slug=self.profile_slug,
                details={"count": count, "endpoints": endpoints[:10]},
            )

        return self._status
