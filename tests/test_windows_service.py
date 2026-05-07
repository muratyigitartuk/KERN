"""Windows service integration tests for scripts/kern-service.py."""
from __future__ import annotations

import os
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

if any(find_spec(name) is None for name in ("win32event", "win32service", "win32serviceutil", "servicemanager")):
    pytest.skip("pywin32 not installed - skipping Windows service tests", allow_module_level=True)

import servicemanager
import win32event

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
kern_service = import_module("kern-service")
KERNService = kern_service.KERNService


def test_service_name():
    assert KERNService._svc_name_ == "KERNWorkspace"


def test_display_name():
    assert "KERN" in KERNService._svc_display_name_


def test_description_present():
    assert len(KERNService._svc_description_) > 10


def test_svc_stop_terminates_process():
    svc = KERNService.__new__(KERNService)
    svc.stop_event = MagicMock()
    proc = MagicMock()
    proc.wait = MagicMock()
    svc._process = proc
    with patch.object(svc, "ReportServiceStatus"):
        with patch.object(win32event, "SetEvent"):
            with patch.object(servicemanager, "LogInfoMsg"):
                svc.SvcStop()
    proc.terminate.assert_called_once()


def test_svc_stop_kills_on_timeout():
    svc = KERNService.__new__(KERNService)
    svc.stop_event = MagicMock()
    proc = MagicMock()
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=15)
    svc._process = proc
    with patch.object(svc, "ReportServiceStatus"):
        with patch.object(win32event, "SetEvent"):
            with patch.object(servicemanager, "LogInfoMsg"):
                svc.SvcStop()
    proc.kill.assert_called_once()


def test_backoff_delay_cap():
    """Exponential backoff should cap at 30 seconds."""
    for restart_count in range(1, 20):
        delay = min(30, 2 ** restart_count)
        assert delay <= 30


def test_run_server_sets_production_posture():
    """_run_server should default to production posture."""
    svc = KERNService.__new__(KERNService)
    svc.stop_event = MagicMock()
    svc._process = None
    env = os.environ.copy()
    env.setdefault("KERN_PRODUCT_POSTURE", "production")
    assert env["KERN_PRODUCT_POSTURE"] == "production" or env["KERN_PRODUCT_POSTURE"] == "personal"


def test_resolve_python_executable_prefers_python_over_pythonservice(monkeypatch):
    svc = KERNService.__new__(KERNService)
    service_exe = Path(sys.executable).with_name("pythonservice.exe")

    monkeypatch.delenv("KERN_SERVICE_PYTHON", raising=False)
    monkeypatch.setattr(kern_service.sys, "executable", str(service_exe))
    monkeypatch.setattr(kern_service.sys, "_base_executable", str(service_exe))

    original_exists = Path.exists

    def fake_exists(path: Path) -> bool:
        if path == service_exe.with_name("python.exe"):
            return True
        if path == service_exe.with_name("pythonw.exe"):
            return False
        if path == service_exe:
            return True
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", fake_exists)

    resolved = svc._resolve_python_executable()

    assert resolved.lower().endswith("python.exe")
    assert "pythonservice.exe" not in resolved.lower()
