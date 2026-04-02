"""KERN AI Workspace -- Windows Service wrapper.

Install:   python kern-service.py install
Start:     python kern-service.py start
Stop:      python kern-service.py stop
Remove:    python kern-service.py remove

Requires pywin32: pip install pywin32
"""
from __future__ import annotations

import os
import sys
import subprocess
import logging
from pathlib import Path

logger = logging.getLogger("kern-service")

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError:
    print("pywin32 is required. Install with: pip install pywin32")
    sys.exit(1)


class KERNService(win32serviceutil.ServiceFramework):
    _svc_name_ = "KERNWorkspace"
    _svc_display_name_ = "KERN AI Workspace"
    _svc_description_ = "Privacy-first local AI workspace for enterprise use."

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._process = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._process.kill()
        servicemanager.LogInfoMsg("KERN service stopped.")

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self._run_server()

    def _resolve_python_executable(self) -> str:
        explicit = os.environ.get("KERN_SERVICE_PYTHON", "").strip()
        candidates: list[Path] = []
        if explicit:
            candidates.append(Path(explicit))

        base_executable = getattr(sys, "_base_executable", "")
        if base_executable:
            base_path = Path(base_executable)
            if base_path.name.lower().startswith("pythonservice"):
                candidates.append(base_path.with_name("python.exe"))
                candidates.append(base_path.with_name("pythonw.exe"))
            candidates.append(base_path)

        current = Path(sys.executable)
        if current.name.lower().startswith("pythonservice"):
            candidates.append(current.with_name("python.exe"))
            candidates.append(current.with_name("pythonw.exe"))
        candidates.append(current)

        for candidate in candidates:
            if candidate and candidate.exists():
                return str(candidate)
        return str(current)

    def _run_server(self):
        app_dir = Path(__file__).resolve().parent.parent
        python = self._resolve_python_executable()
        env = os.environ.copy()
        env.setdefault("KERN_PRODUCT_POSTURE", "production")
        env.setdefault("KERN_POLICY_MODE", "corporate")
        log_dir = app_dir / ".kern"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "kern-service.log"
        restart_count = 0
        last_start = 0.0
        cmd = [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"]
        servicemanager.LogInfoMsg(f"KERN service resolved interpreter: {python}")
        servicemanager.LogInfoMsg(f"KERN service application directory: {app_dir}")
        try:
            while True:
                import time as _time
                now = _time.time()
                # Reset backoff after 5 minutes of stable operation
                if last_start and (now - last_start) > 300:
                    restart_count = 0
                last_start = now
                log_file = open(log_path, "a", encoding="utf-8")
                servicemanager.LogInfoMsg(f"KERN launch command: {cmd}")
                self._process = subprocess.Popen(
                    cmd,
                    cwd=str(app_dir),
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
                servicemanager.LogInfoMsg(f"KERN server started (PID {self._process.pid}).")
                # Wait for stop signal or process exit
                while True:
                    result = win32event.WaitForSingleObject(self.stop_event, 5000)
                    if result == win32event.WAIT_OBJECT_0:
                        log_file.close()
                        return
                    if self._process.poll() is not None:
                        log_file.close()
                        break
                restart_count += 1
                delay = min(30, 2 ** restart_count)
                servicemanager.LogWarningMsg(
                    f"KERN server exited (code {self._process.returncode}). "
                    f"Restarting in {delay}s (attempt {restart_count})..."
                )
                # Check for stop during backoff
                wait_result = win32event.WaitForSingleObject(self.stop_event, delay * 1000)
                if wait_result == win32event.WAIT_OBJECT_0:
                    return
        except Exception as exc:
            servicemanager.LogErrorMsg(f"KERN service error: {exc}")
        finally:
            if self._process and self._process.poll() is None:
                self._process.terminate()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(KERNService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(KERNService)
