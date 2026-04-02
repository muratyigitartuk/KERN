from __future__ import annotations

import sys
from types import SimpleNamespace

from app.network_monitor import NetworkMonitor, _check_outbound


class _PlatformStub:
    def __init__(self) -> None:
        self.audits: list[tuple[str, str, str, str, str, dict[str, object]]] = []

    def record_audit(
        self,
        capability: str,
        action: str,
        outcome: str,
        message: str,
        *,
        profile_slug: str,
        details: dict[str, object],
    ) -> None:
        self.audits.append((capability, action, outcome, message, profile_slug, details))


def _conn(host: str, port: int, status: str = "ESTABLISHED"):
    return SimpleNamespace(status=status, raddr=SimpleNamespace(ip=host, port=port))


def test_check_outbound_uses_current_process_connections(monkeypatch) -> None:
    fake_psutil = SimpleNamespace(
        Process=lambda pid: SimpleNamespace(
            net_connections=lambda kind="inet": [
                _conn("127.0.0.1", 11434),
                _conn("54.192.97.113", 443),
            ]
        )
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    status, count, endpoints = _check_outbound({"127.0.0.1", "localhost"})

    assert status == "network_detected"
    assert count == 1
    assert endpoints == ["54.192.97.113:443"]


def test_check_outbound_refuses_systemwide_fallback(monkeypatch) -> None:
    def _broken_process(pid):
        raise RuntimeError("access denied")

    def _systemwide_should_not_run(kind="inet"):
        raise AssertionError("system-wide fallback should not be used")

    fake_psutil = SimpleNamespace(Process=_broken_process, net_connections=_systemwide_should_not_run)
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    status, count, endpoints = _check_outbound({"127.0.0.1", "localhost"})

    assert status == "unmonitored"
    assert count == 0
    assert endpoints == []


def test_network_monitor_only_audits_real_process_outbound(monkeypatch) -> None:
    fake_psutil = SimpleNamespace(
        Process=lambda pid: SimpleNamespace(net_connections=lambda kind="inet": [_conn("127.0.0.1", 11434)])
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    platform = _PlatformStub()
    monitor = NetworkMonitor(platform=platform, profile_slug="default", interval_seconds=0, enabled=True)

    snapshot = monitor.check()

    assert snapshot.status == "isolated"
    assert snapshot.outbound_connections == 0
    assert snapshot.endpoints == []
    assert platform.audits == []
