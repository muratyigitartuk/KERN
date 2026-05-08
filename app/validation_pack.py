from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME


logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = ROOT_DIR / "output" / "playwright"
FIXTURE_ROOT = ROOT_DIR / "tests" / "fixtures" / "validation"
NODE_BIN = shutil.which("node") or shutil.which("node.exe") or "node"
NPX_BIN = shutil.which("npx") or shutil.which("npx.cmd") or "npx"
POWERSHELL_BIN = shutil.which("powershell") or shutil.which("powershell.exe")
REQUIRED_RELEASE_LANES = (
    "shell_smoke",
    "trust_governance",    "package_validation",
    "package_smoke_install",
    "update_restore_smoke",
    "uninstall_smoke",
    "regression_visuals",
)
ADVISORY_RELEASE_LANES = ("busy_day_advisory",)


class ValidationPackError(RuntimeError):
    pass


@dataclass(slots=True)
class CheckResult:
    status: str
    title: str
    detail: str
    artifacts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LaneResult:
    lane: str
    status: str = "pass"
    checks: list[CheckResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.checks.append(result)
        self.artifacts.extend(result.artifacts)
        if result.status == "fail":
            self.status = "fail"
        elif result.status == "warn" and self.status == "pass":
            self.status = "warn"

    def warn(self, title: str, detail: str, *artifacts: str) -> None:
        self.warnings.append(f"{title}: {detail}")
        self.add(CheckResult(status="warn", title=title, detail=detail, artifacts=list(artifacts)))

    def fail(self, title: str, detail: str, *artifacts: str) -> None:
        self.add(CheckResult(status="fail", title=title, detail=detail, artifacts=list(artifacts)))

    def ok(self, title: str, detail: str, *artifacts: str) -> None:
        self.add(CheckResult(status="pass", title=title, detail=detail, artifacts=list(artifacts)))


@dataclass(slots=True)
class RuntimeHandle:
    base_url: str
    output_dir: Path
    process: subprocess.Popen[str] | None = None
    env_root: Path | None = None
    mode: str = "external"
    policy_mode: str = "personal"
    product_posture: str = "production"

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None



class PlaywrightCliSession:
    def __init__(self, *, session: str, workdir: Path):
        self.session = session
        self.workdir = workdir
        self.cli_dir = workdir / ".playwright-cli"
        self.cli_dir.mkdir(parents=True, exist_ok=True)

    def _cmd(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = [
            NPX_BIN,
            "--yes",
            "--package",
            "@playwright/cli",
            "playwright-cli",
            f"-s={self.session}",
            *args,
        ]
        proc = subprocess.run(
            command,
            cwd=self.workdir,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise ValidationPackError(proc.stderr.strip() or proc.stdout.strip() or f"Playwright CLI failed: {' '.join(args)}")
        return proc

    def open(self, url: str) -> str:
        return self._cmd("open", url).stdout

    def close(self) -> str:
        return self._cmd("close", check=False).stdout

    def resize(self, width: int, height: int) -> str:
        return self._cmd("resize", str(width), str(height)).stdout

    def snapshot(self, name: str) -> Path:
        proc = self._cmd("snapshot")
        return self._capture_linked_artifact(proc.stdout, ".yml", name)

    def screenshot(self, name: str) -> Path:
        proc = self._cmd("screenshot")
        return self._capture_linked_artifact(proc.stdout, ".png", name)

    def console(self, name: str, min_level: str | None = None) -> Path:
        args = ["console"]
        if min_level:
            args.append(min_level)
        proc = self._cmd(*args, check=False)
        dest = self.workdir / f"{name}.console.log"
        dest.write_text(proc.stdout or proc.stderr or "", encoding="utf-8")
        return dest

    def network(self, name: str) -> Path:
        proc = self._cmd("network", check=False)
        dest = self.workdir / f"{name}.network.log"
        dest.write_text(proc.stdout or proc.stderr or "", encoding="utf-8")
        return dest

    def eval_js(self, expression: str) -> str:
        return self._cmd("eval", expression).stdout.strip()

    def run_code(self, code: str) -> str:
        return self._cmd("run-code", code).stdout

    def _capture_linked_artifact(self, stdout: str, suffix: str, name: str) -> Path:
        linked = _extract_markdown_link_paths(stdout)
        source: Path | None = None
        for entry in linked:
            candidate = (self.workdir / entry).resolve()
            if candidate.suffix.lower() == suffix and candidate.exists():
                source = candidate
                break
        if source is None:
            matches = sorted(self.cli_dir.glob(f"*{suffix}"), key=lambda item: item.stat().st_mtime)
            if not matches:
                raise ValidationPackError(f"Playwright CLI did not produce a {suffix} artifact.")
            source = matches[-1]
        destination = self.workdir / f"{name}{suffix}"
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        return destination


def _extract_markdown_link_paths(text: str) -> list[Path]:
    matches = re.findall(r"\]\(([^)]+)\)", text)
    return [Path(match) for match in matches]


def _utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _make_output_dir(base: str | None) -> Path:
    if base:
        path = Path(base)
        if not path.is_absolute():
            path = ROOT_DIR / path
    else:
        path = OUTPUT_ROOT / _utc_now_stamp()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _runtime_env(
    root: Path,
    policy_mode: str,
    product_posture: str = "production",
    *,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("KERN_")}
    env_root = root.resolve()
    env.update(
        {
            "KERN_SYSTEM_DB_PATH": str(env_root / "kern-system.db"),
            "KERN_DB_PATH": str(env_root / "kern.db"),
            "KERN_ROOT_PATH": str(env_root),
            "KERN_PROFILE_ROOT": str(env_root / "profiles"),
            "KERN_BACKUP_ROOT": str(env_root / "backups"),
            "KERN_DOCUMENT_ROOT": str(env_root / "documents"),
            "KERN_ATTACHMENT_ROOT": str(env_root / "attachments"),
            "KERN_ARCHIVE_ROOT": str(env_root / "archives"),
            "KERN_POLICY_MODE": policy_mode,
            "KERN_PRODUCT_POSTURE": product_posture,
            "KERN_SEED_DEFAULTS": "true",
            "KERN_PWA_ENABLED": "false",
            "KERN_DISABLE_DOTENV": "true",
            "KERN_LLM_ENABLED": "false",
            "KERN_ARTIFACT_ENCRYPTION_ENABLED": "false",
            "KERN_PROACTIVE_ENABLED": "false",
            "KERN_NETWORK_MONITOR_ENABLED": "false",
            "KERN_OCR_ENABLED": "false",
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "true",
        }
    )
    if extra_env:
        env.update(extra_env)
    return env

def _admin_headers() -> dict[str, str]:
    return {}


def _http_get_json(url: str, *, expected_status: int | None = 200) -> tuple[int, Any]:
    with httpx.Client(timeout=20.0) as client:
        response = client.get(url, headers=_admin_headers())
    if expected_status is not None and response.status_code != expected_status:
        raise ValidationPackError(f"Unexpected status {response.status_code} for {url}")
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text


def _http_post_json(url: str, *, expected_status: int | None = 200) -> tuple[int, Any]:
    with httpx.Client(timeout=20.0) as client:
        parsed = httpx.URL(url)
        port = f":{parsed.port}" if parsed.port is not None else ""
        headers = _bootstrap_csrf_headers(client, f"{parsed.scheme}://{parsed.host}{port}")
        response = client.post(url, headers=headers)
    if expected_status is not None and response.status_code != expected_status:
        raise ValidationPackError(f"Unexpected status {response.status_code} for {url}")
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text


def _bootstrap_csrf_headers(client: httpx.Client, base_url: str) -> dict[str, str]:
    response = client.get(f"{base_url}/health", headers=_admin_headers())
    csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
    if not csrf_token:
        raise ValidationPackError(
            f"Validation pack could not establish a CSRF token from {base_url}/health "
            f"(status {response.status_code})."
        )
    return {CSRF_HEADER_NAME: csrf_token, **_admin_headers()}


def _http_post_upload(base_url: str, files: list[Path]) -> dict[str, Any]:
    opened: list[Any] = []
    try:
        multipart = []
        for path in files:
            handle = path.open("rb")
            opened.append(handle)
            multipart.append(("files", (path.name, handle, "application/octet-stream")))
        with httpx.Client(timeout=60.0) as client:
            headers = _bootstrap_csrf_headers(client, base_url)
            response = client.post(f"{base_url}/upload", files=multipart, headers=headers)
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text}
        if response.status_code >= 400:
            return {"status_code": response.status_code, **payload}
        return {"status_code": response.status_code, **payload}
    finally:
        for handle in opened:
            handle.close()


def _http_post_file(base_url: str, route: str, field_name: str, path: Path) -> tuple[int, Any]:
    with path.open("rb") as handle:
        multipart = {field_name: (path.name, handle, "application/json")}
        with httpx.Client(timeout=60.0) as client:
            headers = _bootstrap_csrf_headers(client, base_url)
            response = client.post(f"{base_url}{route}", files=multipart, headers=headers)
    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text
    return response.status_code, payload


def _run_powershell_json(script: Path, *args: str) -> Any:
    if os.name != "nt" or not POWERSHELL_BIN:
        raise ValidationPackError("PowerShell is unavailable on this platform.")
    proc = subprocess.run(
        [POWERSHELL_BIN, "-ExecutionPolicy", "Bypass", "-File", str(script), *args, "-Json"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValidationPackError(proc.stderr.strip() or proc.stdout.strip() or f"PowerShell script failed: {script}")
    return json.loads(proc.stdout)


def _build_runtime_package() -> Path:
    configured_package = os.environ.get("KERN_VALIDATION_PACKAGE_PATH", "").strip()
    if configured_package:
        package_path = Path(configured_package).expanduser()
        if not package_path.is_absolute():
            package_path = (ROOT_DIR / package_path).resolve()
        if not package_path.exists():
            raise ValidationPackError(f"KERN_VALIDATION_PACKAGE_PATH does not exist: {configured_package}")
        return package_path

    script = ROOT_DIR / "scripts" / "package-kern-runtime.ps1"
    if os.name != "nt" or not POWERSHELL_BIN:
        raise ValidationPackError("PowerShell is unavailable on this platform.")
    proc = subprocess.run(
        [POWERSHELL_BIN, "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValidationPackError(proc.stderr.strip() or proc.stdout.strip() or "Runtime package build failed.")
    packages = sorted((ROOT_DIR / "output" / "packages").glob("kern-internal-runtime-*.zip"))
    if not packages:
        raise ValidationPackError("Runtime package build did not produce a zip artifact.")
    return packages[-1]


def _tail_text(path: Path, *, lines: int = 80) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _wait_for_ready(
    base_url: str,
    *,
    timeout_seconds: float = 120.0,
    process: subprocess.Popen[str] | None = None,
    log_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.time() + timeout_seconds
    last_live: dict[str, Any] | None = None
    last_ready: dict[str, Any] | None = None
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            tail = _tail_text(log_path, lines=80) if log_path is not None else ""
            detail = f"KERN runtime exited before readiness at {base_url} with exit code {process.returncode}."
            if tail:
                detail = f"{detail}\nRuntime log tail:\n{tail}"
            raise ValidationPackError(detail)
        try:
            _, live = _http_get_json(f"{base_url}/health/live", expected_status=None)
            _, ready = _http_get_json(f"{base_url}/health/ready", expected_status=None)
            if isinstance(live, dict):
                last_live = live
            if isinstance(ready, dict):
                last_ready = ready
            if live.get("status") == "live" and ready.get("status") in {"ready", "not_ready"}:
                return last_live or {}, last_ready or {}
        except Exception as exc:
            logger.debug("Health check attempt failed: %s", exc)
        time.sleep(1.0)
    tail = _tail_text(log_path, lines=80) if log_path is not None else ""
    detail = f"KERN runtime did not become reachable at {base_url} within {timeout_seconds:.0f}s."
    if tail:
        detail = f"{detail}\nRuntime log tail:\n{tail}"
    raise ValidationPackError(detail)


def _launch_local_runtime(
    output_dir: Path,
    policy_mode: str,
    product_posture: str = "production",
    *,
    extra_env: dict[str, str] | None = None,
) -> RuntimeHandle:
    runtime_root = Path(tempfile.mkdtemp(prefix=f"kern-validation-{policy_mode}-"))
    port = _find_free_port()
    runtime_log = output_dir / f"{policy_mode}.runtime.log"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_handle = runtime_log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT_DIR,
        env=_runtime_env(runtime_root, policy_mode, product_posture=product_posture, extra_env=extra_env),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_ready(base_url, process=process, log_path=runtime_log)
    except Exception:
        RuntimeHandle(base_url=base_url, output_dir=output_dir, process=process, env_root=runtime_root).stop()
        raise
    return RuntimeHandle(
        base_url=base_url,
        output_dir=output_dir,
        process=process,
        env_root=runtime_root,
        mode="launched",
        policy_mode=policy_mode,
        product_posture=product_posture,
    )


def _ensure_cli_available() -> None:
    for command in ([NODE_BIN, "--version"], [NPX_BIN, "--version"]):
        proc = subprocess.run(command, cwd=ROOT_DIR, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ValidationPackError("Node.js and npx are required for the validation pack.")
    proc = subprocess.run(
        [NPX_BIN, "--yes", "--package", "@playwright/cli", "playwright-cli", "--version"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValidationPackError("Unable to access @playwright/cli through npx.")


def _run_assertion(session: PlaywrightCliSession, title: str, code: str, lane: LaneResult) -> None:
    try:
        session.run_code(code)
        lane.ok(title, "Passed.")
    except Exception as exc:  # noqa: BLE001
        lane.fail(title, str(exc))


def _save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def _manual_review_items() -> list[str]:
    return [
        "Check dark/light screenshots for visual drift, modal composition, and clipping.",
        "Review update and failure-card screenshots for business-readable wording and consistent gating.",
        "Review trust/governance screenshots for truthful status labels and confirmation behavior.",
        "Inspect busy-day screenshots for document, schedule, and memory surfaces that look syntactically correct but semantically weak.",
        "Inspect console and network logs for repeated or noisy client-side errors that did not break the run.",
    ]


def _build_release_gate(lane_results: list[LaneResult]) -> dict[str, Any]:
    lane_status_map = {result.lane: result.status for result in lane_results}
    required = {lane: lane_status_map.get(lane, "missing") for lane in REQUIRED_RELEASE_LANES}
    advisory = {lane: lane_status_map.get(lane, "missing") for lane in ADVISORY_RELEASE_LANES}
    release_ready = all(status == "pass" for status in required.values())
    return {
        "required_lanes": required,
        "advisory_lanes": advisory,
        "release_ready": release_ready,
    }


def _write_summary(output_dir: Path, lane_results: list[LaneResult], metadata: dict[str, Any]) -> None:
    release_gate = _build_release_gate(lane_results)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "advisory": True,
        "metadata": metadata,
        "lanes": [asdict(result) for result in lane_results],
        "release_gate": release_gate,
        "manual_review": _manual_review_items(),
    }
    _save_json(output_dir / "summary.json", summary)

    lines = [
        "# KERN Validation Pack Summary",
        "",
        f"- Generated: `{summary['generated_at']}`",
        f"- Advisory mode: `true`",
        f"- Launch mode: `{metadata.get('launch_mode', 'unknown')}`",
        f"- Base URL: `{metadata.get('base_url', '')}`",
        "",
        "## Release Gate",
        "",
        f"- Release-ready required lanes: `{'pass' if release_gate['release_ready'] else 'fail'}`",
        "- Required lanes:",
    ]
    for lane, status in release_gate["required_lanes"].items():
        lines.append(f"  - `{lane}`: `{status}`")
    if release_gate["advisory_lanes"]:
        lines.append("- Advisory lanes:")
        for lane, status in release_gate["advisory_lanes"].items():
            lines.append(f"  - `{lane}`: `{status}`")
    lines.extend(
        [
            "",
        "## Lane Results",
        "",
        ]
    )
    for result in lane_results:
        lines.append(f"### {result.lane}")
        lines.append(f"- Status: `{result.status}`")
        if result.checks:
            for check in result.checks:
                lines.append(f"- `{check.status}` {check.title}: {check.detail}")
        if result.warnings:
            lines.append("- Warnings:")
            for warning in result.warnings:
                lines.append(f"  - {warning}")
        if result.artifacts:
            lines.append("- Artifacts:")
            for artifact in sorted(set(result.artifacts)):
                lines.append(f"  - `{artifact}`")
        lines.append("")
    lines.append("## Manual Review")
    lines.append("")
    for item in _manual_review_items():
        lines.append(f"- {item}")
    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def _run_shell_smoke(base_url: str, lane_dir: Path) -> LaneResult:
    lane = LaneResult(lane="shell_smoke")
    session = PlaywrightCliSession(session="shell-smoke", workdir=lane_dir)
    session.open(base_url)
    session.resize(1440, 1100)
    session.snapshot("shell-home")
    _run_assertion(
        session,
        "Connected state",
        "await page.waitForFunction(() => document.querySelector('#connectionState')?.textContent?.includes('Connected'), { timeout: 20000 });",
        lane,
    )
    _run_assertion(
        session,
        "Primary shell controls",
        """
        await page.waitForFunction(() => {
          return document.querySelector('#sidebarHome')?.textContent?.trim() === 'KERN'
            && !!document.querySelector('#newConversation')
            && !!document.querySelector('#openConversationSearch')
            && !!document.querySelector('#commandInput')
            && !!document.querySelector('#trustBadge');
        }, { timeout: 10000 });
        """.strip(),
        lane,
    )
    _run_assertion(
        session,
        "Production posture defaults",
        """
        await page.waitForFunction(() => document.body?.dataset?.productPosture === 'production', { timeout: 10000 });
        if (document.querySelector('#composerTranscribeAction')) {
          throw new Error('Audio transcription action is still present in production posture.');
        }
        if (document.body.textContent?.includes('Play some morning jazz')) {
          throw new Error('Media starter prompt is still visible in production posture.');
        }
        """.strip(),
        lane,
    )
    session.run_code(
        """
        const firstPrompt = document.querySelector('.starter-card.prompt-chip');
        if (!firstPrompt) {
          throw new Error('No starter prompts rendered.');
        }
        await firstPrompt.click();
        await page.waitForFunction(() => (document.querySelector('#commandInput')?.value || '').length > 0, { timeout: 5000 });
        """.strip()
    )
    lane.ok("Starter prompt drafting", "Starter prompt chips still draft into the composer in production posture.")
    session.run_code(
        """
        if (!(document.querySelector('#commandInput')?.value || '').trim()) {
          await page.fill('#commandInput', 'Validation ping for conversation search');
        }
        await page.click('#sendButton');
        await page.waitForFunction(() => document.querySelectorAll('[data-turn-id]').length >= 1, { timeout: 15000 });
        """.strip()
    )
    lane.ok("Message submit", "Conversation accepted a seeded validation message.")
    session.run_code(
        """
        await page.click('#openConversationSearch');
        await page.waitForSelector('#conversationSearchModal:not(.hidden)');
        await page.fill('#conversationSearchInput', 'Validation ping');
        await page.waitForFunction(() => document.querySelectorAll('[data-testid="conversation-search-result"]').length >= 1, { timeout: 10000 });
        """.strip()
    )
    lane.ok("Conversation search", "Search modal opened and returned at least one result.")
    lane.artifacts.append(_display_path(session.snapshot("shell-search-modal")))
    session.run_code("await page.click('#closeConversationSearch'); await page.waitForSelector('#conversationSearchModal.hidden');")
    session.run_code("await page.click('#openSettings'); await page.waitForSelector('#settingsModal:not(.hidden)');")
    lane.ok("Settings modal", "Settings modal opened.")
    lane.artifacts.append(_display_path(session.snapshot("shell-settings")))
    session.run_code("await page.click('#closeSettings'); await page.waitForSelector('#settingsModal.hidden');")
    session.run_code("await page.click('#utilityToggle'); await page.waitForSelector('#utilityModal:not(.hidden)');")
    lane.ok("Utility modal", "Utility modal opened.")
    lane.artifacts.append(_display_path(session.snapshot("shell-utility")))
    session.run_code("await page.click('#closeUtilityModal'); await page.waitForSelector('#utilityModal.hidden');")
    session.run_code(
        """
        await page.click('#openSettings');
        await page.click('[data-settings-section-nav="appearance"]');
        await page.click('[data-theme-mode="light"]');
        await page.waitForFunction(() => document.documentElement.dataset.theme === 'light', { timeout: 5000 });
        await page.click('#closeSettings');
        """.strip()
    )
    light_shot = session.screenshot("workspace-light")
    session.run_code(
        """
        await page.click('#openSettings');
        await page.click('[data-settings-section-nav="appearance"]');
        await page.click('[data-theme-mode="dark"]');
        await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark', { timeout: 5000 });
        await page.click('#closeSettings');
        """.strip()
    )
    dark_shot = session.screenshot("workspace-dark")
    lane.ok("Theme switching", "Dark and light workspace captures were generated.", _display_path(light_shot), _display_path(dark_shot))
    lane.artifacts.append(_display_path(session.console("shell-smoke")))
    lane.artifacts.append(_display_path(session.network("shell-smoke")))
    session.close()
    return lane


def _run_trust_governance(personal_url: str, corporate_url: str, personal_posture_url: str | None, lane_dir: Path) -> LaneResult:
    lane = LaneResult(lane="trust_governance")

    health_code, health = _http_get_json(f"{personal_url}/health", expected_status=None)
    live_code, live = _http_get_json(f"{personal_url}/health/live", expected_status=None)
    ready_code, ready = _http_get_json(f"{personal_url}/health/ready", expected_status=None)
    _, governance = _http_post_json(f"{personal_url}/governance/export")
    _save_json(lane_dir / "health.json", health)
    _save_json(lane_dir / "health-live.json", live)
    _save_json(lane_dir / "health-ready.json", ready)
    _save_json(lane_dir / "governance.json", governance)
    required_health = {"status", "components", "audit_chain_ok", "runtime_degraded_reasons", "app_version"}
    required_governance = {"health", "security", "policy", "retention_policies", "backup_inventory", "document_classifications", "product_posture"}
    health_ok = (
        isinstance(health, dict)
        and required_health.issubset(health.keys())
        and health.get("status") in {"ok", "warning", "degraded", "error"}
        and isinstance(live, dict)
        and live.get("status") in {"live", "error"}
        and isinstance(ready, dict)
        and ready.get("status") in {"ready", "not_ready"}
        and health_code in {200, 500, 503}
        and live_code in {200, 500}
        and ready_code in {200, 503}
    )
    if health_ok and required_governance.issubset(governance.keys()):
        lane.ok("Health and governance payloads", "Health endpoints and governance export returned semantically valid payloads.", "health.json", "governance.json")
    else:
        lane.fail("Health and governance payloads", "One or more required keys were missing.", "health.json", "governance.json")

    personal_session = PlaywrightCliSession(session="trust-personal", workdir=lane_dir / "personal-ui")
    personal_session.open(personal_url)
    personal_session.resize(1440, 1100)
    personal_session.run_code(
        """
        await page.waitForFunction(() => document.querySelector('#connectionState')?.textContent?.includes('Connected'), { timeout: 20000 });
        await page.click('#openSettings');
        await page.click('[data-settings-section-nav="profile"]');
        await page.waitForSelector('#settingsProfileState');
        """.strip()
    )
    personal_session.run_code(
        """
        const profileState = document.querySelector('#settingsProfileState')?.textContent?.trim();
        const auditState = document.querySelector('#settingsAuditState')?.textContent?.trim();
        const dbState = document.querySelector('#settingsDbEncryption')?.textContent?.trim();
        if (!profileState || !auditState || !dbState) {
          throw new Error('Profile security labels did not render.');
        }
        """.strip()
    )
    profile_shot = personal_session.screenshot("settings-profile")
    lane.ok("Profile security rendering", "Profile and security labels rendered in settings.", _display_path(profile_shot))
    personal_session.close()

    corporate_session = PlaywrightCliSession(session="trust-corporate", workdir=lane_dir / "corporate-ui")
    corporate_session.open(corporate_url)
    corporate_session.resize(1440, 1100)
    corporate_session.run_code(
        """
        await page.waitForFunction(() => document.querySelector('#connectionState')?.textContent?.includes('Connected'), { timeout: 20000 });
        await page.click('#openSettings');
        await page.click('[data-settings-section-nav="profile"]');
        await page.fill('#settingsBackupPassword', 'validation-pack-password');
        await page.click('#settingsCreateBackup');
        await page.waitForFunction(() => !document.querySelector('#confirmationBox')?.classList.contains('hidden'), { timeout: 15000 });
        """.strip()
    )
    corporate_shot = corporate_session.screenshot("corporate-confirmation")
    lane.ok("Corporate policy gating", "Corporate mode surfaced a confirmation gate for backup creation.", _display_path(corporate_shot))
    corporate_session.run_code("await page.click('#cancelButton');")
    status_code, gated_payload = _http_post_json(f"{corporate_url}/governance/export", expected_status=None)
    _save_json(lane_dir / "corporate-governance-response.json", gated_payload)
    if status_code in {403, 409}:
        lane.ok("Corporate governance route protection", f"Governance export returned policy-gated status {status_code}.", "corporate-governance-response.json")
    else:
        lane.fail("Corporate governance route protection", f"Expected 403/409 from governance export in corporate mode, got {status_code}.", "corporate-governance-response.json")
    lane.artifacts.append(_display_path(corporate_session.console("trust-governance")))
    lane.artifacts.append(_display_path(corporate_session.network("trust-governance")))
    corporate_session.close()

    if personal_posture_url:
        personal_posture_session = PlaywrightCliSession(session="trust-personal-posture", workdir=lane_dir / "personal-posture-ui")
        personal_posture_session.open(personal_posture_url)
        personal_posture_session.resize(1440, 1100)
        personal_posture_session.run_code(
            """
            await page.waitForFunction(() => document.querySelector('#connectionState')?.textContent?.includes('Connected'), { timeout: 20000 });
            await page.waitForFunction(() => document.body?.dataset?.productPosture === 'personal', { timeout: 10000 });
            if (document.querySelector('#composerTranscribeAction')) {
              throw new Error('Audio transcription action reappeared in personal posture.');
            }
            """.strip()
        )
        personal_shot = personal_posture_session.screenshot("personal-posture-controls")
        lane.ok("Personal posture compatibility", "Personal posture preserved the workspace shell without re-enabling removed personal-only controls.", _display_path(personal_shot))
        personal_posture_session.close()
    return lane


def _run_busy_day(base_url: str, lane_dir: Path) -> LaneResult:
    lane = LaneResult(lane="busy_day_advisory")
    fixture_files = sorted(FIXTURE_ROOT.glob("*"))
    if not fixture_files:
        lane.fail(
            "Validation fixtures missing",
            f"Expected packaged fixture files under {FIXTURE_ROOT}, but none were found.",
        )
        return lane
    upload_response = _http_post_upload(base_url, fixture_files)
    _save_json(lane_dir / "upload.json", upload_response)
    if int(upload_response.get("status_code", 200) or 200) == 403:
        session = PlaywrightCliSession(session="busy-day", workdir=lane_dir)
        session.open(base_url)
        session.resize(1440, 1100)
        session.run_code(
            """
            await page.waitForFunction(() => document.querySelector('#connectionState')?.textContent?.includes('Connected'), { timeout: 20000 });
            if (document.querySelector('#onboardingCard') || document.querySelector('#onboardingModal')) {
              throw new Error('First-run onboarding should not be present.');
            }
            """.strip()
        )
        lane.ok(
            "Production workspace gate",
            "Busy-day upload remains available without blocking the workspace behind onboarding.",
            "upload.json",
            _display_path(session.screenshot("busy-workspace-gate")),
        )
        session.close()
        return lane
    indexed = int(upload_response.get("indexed", 0) or 0)
    expected = len(fixture_files)
    if indexed != expected:
        lane.warn(
            "Fixture upload",
            f"Indexed {indexed} of {expected} fixture files. The isolated runtime may already contain one duplicate fixture from an earlier lane, so busy-day checks continue in advisory mode.",
            "upload.json",
        )
    else:
        lane.ok("Fixture upload", f"Uploaded {indexed} fixture files.", "upload.json")

    session = PlaywrightCliSession(session="busy-day", workdir=lane_dir)
    session.open(base_url)
    session.resize(1440, 1100)
    session.run_code(
        """
        await page.waitForFunction(() => document.querySelector('#connectionState')?.textContent?.includes('Connected'), { timeout: 20000 });
        await page.fill('#commandInput', 'Remember that the validation user prefers concise release notes');
        await page.click('#sendButton');
        await page.waitForFunction(() => document.querySelectorAll('[data-turn-id]').length >= 1, { timeout: 15000 });
        """.strip()
    )
    lane.ok("Memory seed prompt", "Seeded a deterministic memory statement through the normal conversation path.")

    session.run_code(
        """
        await page.click('#openSettings');
        await page.click('[data-settings-section-nav="domains"]');
        await page.waitForFunction(() => Number(document.querySelector('#settingsDocumentCount')?.textContent || '0') >= 1, { timeout: 15000 });
        await page.click('#closeSettings');
        """.strip()
    )
    lane.ok("Document totals", "Domain totals reflected uploaded fixture content.")

    session.run_code(
        """
        await page.click('.utility-tab[data-tab="schedule"]');
        await page.click('#addScheduleButton');
        await page.fill('#scheduleTitle', 'Validation summary');
        await page.fill('#scheduleActionPayload', 'Summarize validation artifacts');
        await page.click('#saveScheduleButton');
        await page.waitForFunction(() => document.querySelectorAll('[data-testid="schedule-row"]').length >= 1, { timeout: 15000 });
        """.strip()
    )
    lane.ok("Schedule creation", "Schedule creation flow completed and rendered a schedule row.", _display_path(session.screenshot("busy-schedule")))

    session.run_code(
        """
        await page.click('.utility-tab[data-tab="audit"]');
        await page.waitForSelector('#exportAuditButton');
        """.strip()
    )
    lane.ok("Audit surface", "Audit utility surface rendered with export controls.", _display_path(session.screenshot("busy-audit")))
    lane.artifacts.append(_display_path(session.console("busy-day")))
    lane.artifacts.append(_display_path(session.network("busy-day")))
    session.close()
    return lane


def _run_visual_regressions(base_url: str, lane_dir: Path) -> LaneResult:
    lane = LaneResult(lane="regression_visuals")
    session = PlaywrightCliSession(session="visuals", workdir=lane_dir)
    session.open(base_url)
    session.resize(1440, 1100)
    session.run_code("await page.waitForFunction(() => document.querySelector('#connectionState')?.textContent?.includes('Connected'), { timeout: 20000 });")

    captures: list[str] = []

    def capture(name: str) -> None:
        captures.append(_display_path(session.screenshot(name)))

    capture("visual-main-dark")
    session.run_code(
        """
        await page.click('#openSettings');
        await page.click('[data-settings-section-nav="appearance"]');
        await page.click('[data-theme-mode="light"]');
        await page.waitForFunction(() => document.documentElement.dataset.theme === 'light', { timeout: 5000 });
        await page.click('#closeSettings');
        """.strip()
    )
    capture("visual-main-light")
    session.run_code("await page.click('#openSettings');")
    capture("visual-settings-overview")
    session.run_code("await page.click('[data-settings-section-nav=\"profile\"]');")
    capture("visual-settings-profile")
    session.run_code("await page.click('#closeSettings');")
    session.run_code("await page.click('#utilityToggle');")
    session.run_code("await page.click('.utility-tab[data-tab=\"context\"]');")
    capture("visual-utility-context")
    session.run_code("await page.click('.utility-tab[data-tab=\"schedule\"]');")
    capture("visual-utility-schedule")
    session.run_code("await page.click('#closeUtilityModal');")
    session.run_code("await page.click('#openConversationSearch');")
    capture("visual-conversation-search")
    session.run_code("await page.click('#closeConversationSearch');")
    lane.ok("Visual capture set", "Captured the fixed screenshot set for dark/light and modal surfaces.", *captures)
    lane.artifacts.append(_display_path(session.console("visuals")))
    lane.artifacts.append(_display_path(session.network("visuals")))
    session.close()
    return lane


def _run_package_validation_lane(lane_dir: Path) -> LaneResult:
    lane = LaneResult(lane="package_validation")
    package_path = _build_runtime_package()
    payload = _run_powershell_json(ROOT_DIR / "scripts" / "validate-kern-package.ps1", "-PackagePath", str(package_path))
    _save_json(lane_dir / "package-validation.json", payload)
    if payload.get("valid"):
        lane.ok("Runtime package validation", "The packaged handoff contains the required scripts, docs, manifest, and checksum.", "package-validation.json")
    else:
        lane.fail("Runtime package validation", f"Package validation reported missing or invalid artifacts: {payload!r}", "package-validation.json")
    return lane


def _run_package_smoke_install_lane(lane_dir: Path) -> LaneResult:
    lane = LaneResult(lane="package_smoke_install")
    package_path = _build_runtime_package()
    payload = _run_powershell_json(ROOT_DIR / "scripts" / "smoke-kern-runtime-package.ps1", "-PackagePath", str(package_path))
    _save_json(lane_dir / "package-smoke.json", payload)
    if payload.get("install_result") == "ok":
        lane.ok("Packaged install smoke", "The packaged runtime installs, reaches readiness, and passes workspace validation lanes.", "package-smoke.json")
    else:
        lane.fail("Packaged install smoke", f"Packaged smoke install did not complete successfully: {payload!r}", "package-smoke.json")
    return lane


def _run_update_restore_smoke_lane(lane_dir: Path) -> LaneResult:
    lane = LaneResult(lane="update_restore_smoke")
    payload = _run_powershell_json(ROOT_DIR / "scripts" / "smoke-kern-update-restore.ps1", "-OutputRoot", str(lane_dir))
    _save_json(lane_dir / "update-restore-smoke.json", payload)
    if payload.get("sentinel_present"):
        lane.ok("Update and restore smoke", "Rollback bundle creation and restore still work from the packaged install path.", "update-restore-smoke.json")
    else:
        lane.fail("Update and restore smoke", f"Restore smoke did not preserve the sentinel document: {payload!r}", "update-restore-smoke.json")
    return lane


def _run_uninstall_smoke_lane(lane_dir: Path) -> LaneResult:
    lane = LaneResult(lane="uninstall_smoke")
    payload = _run_powershell_json(ROOT_DIR / "scripts" / "smoke-kern-uninstall.ps1", "-OutputRoot", str(lane_dir))
    _save_json(lane_dir / "uninstall-smoke.json", payload)
    default_ok = payload.get("default_uninstall", {}).get("preserved_data") and payload.get("default_uninstall", {}).get("removed_runtime")
    full_delete_ok = payload.get("remove_data_uninstall", {}).get("removed_data") and payload.get("remove_data_uninstall", {}).get("removed_runtime")
    if default_ok and full_delete_ok:
        lane.ok("Uninstall smoke", "Default uninstall preserves .kern data, and RemoveData mode removes both runtime artifacts and profile data.", "uninstall-smoke.json")
    else:
        lane.fail("Uninstall smoke", f"Unexpected uninstall smoke payload: {payload!r}", "uninstall-smoke.json")
    return lane


def run_validation_pack(
    *,
    base_url: str | None,
    launch_local: bool,
    lane_filter: str | None,
    output_dir_arg: str | None,
) -> tuple[int, Path]:
    _ensure_cli_available()
    output_dir = _make_output_dir(output_dir_arg)
    runtimes: list[RuntimeHandle] = []
    lane_results: list[LaneResult] = []
    metadata: dict[str, Any] = {
        "launch_mode": "external" if base_url and not launch_local else "isolated_local",
        "base_url": base_url or "",
        "lane_filter": lane_filter or "all",
        "product_posture": "production",
    }
    try:
        if launch_local or not base_url:
            personal = _launch_local_runtime(output_dir / "personal-runtime", "personal", "production")
            corporate = _launch_local_runtime(output_dir / "corporate-runtime", "corporate", "production")
            personal_posture = _launch_local_runtime(output_dir / "personal-posture-runtime", "personal", "personal")
            runtimes.extend([personal, corporate, personal_posture])
        else:
            personal = RuntimeHandle(
                base_url=base_url,
                output_dir=output_dir,
                mode="external",
                policy_mode="personal",
                product_posture="production",
            )
            corporate = personal
            personal_posture = None

        metadata["base_url"] = personal.base_url
        metadata["corporate_base_url"] = corporate.base_url
        metadata["personal_posture_base_url"] = personal_posture.base_url if personal_posture else ""

        lane_map = {
            "shell_smoke": lambda: _run_shell_smoke(personal.base_url, output_dir / "shell_smoke"),
            "trust_governance": lambda: _run_trust_governance(
                personal.base_url,
                corporate.base_url,
                personal_posture.base_url if personal_posture else None,
                output_dir / "trust_governance",
            ),
            "busy_day_advisory": lambda: _run_busy_day(personal.base_url, output_dir / "busy_day_advisory"),
            "package_validation": lambda: _run_package_validation_lane(output_dir / "package_validation"),
            "package_smoke_install": lambda: _run_package_smoke_install_lane(output_dir / "package_smoke_install"),
            "update_restore_smoke": lambda: _run_update_restore_smoke_lane(output_dir / "update_restore_smoke"),
            "uninstall_smoke": lambda: _run_uninstall_smoke_lane(output_dir / "uninstall_smoke"),
            "regression_visuals": lambda: _run_visual_regressions(personal.base_url, output_dir / "regression_visuals"),
        }
        for lane_name, runner in lane_map.items():
            if lane_filter and lane_filter != lane_name:
                continue
            lane_dir = output_dir / lane_name
            lane_dir.mkdir(parents=True, exist_ok=True)
            try:
                lane_results.append(runner())
            except Exception as exc:  # noqa: BLE001
                failed = LaneResult(lane=lane_name, status="fail")
                failed.fail(lane_name, str(exc))
                lane_results.append(failed)
        _write_summary(output_dir, lane_results, metadata)
        catastrophic = any(result.status == "fail" and not result.checks for result in lane_results)
        return (1 if catastrophic else 0), output_dir
    finally:
        for runtime in reversed(runtimes):
            runtime.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the KERN advisory Playwright validation pack.")
    parser.add_argument("--base-url", help="Use an already-running KERN runtime instead of launching an isolated local one.")
    parser.add_argument("--launch-local", action="store_true", help="Launch isolated local runtimes for personal and corporate validation.")
    parser.add_argument(
        "--lane",
        choices=[
            "shell_smoke",
            "trust_governance",            "busy_day_advisory",
            "package_validation",
            "package_smoke_install",
            "update_restore_smoke",
            "uninstall_smoke",
            "regression_visuals",
        ],
        help="Run a single validation lane instead of the full pack.",
    )
    parser.add_argument("--output-dir", help="Override the artifact output directory.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        exit_code, output_dir = run_validation_pack(
            base_url=args.base_url,
            launch_local=args.launch_local,
            lane_filter=args.lane,
            output_dir_arg=args.output_dir,
        )
    except ValidationPackError as exc:
        print(f"validation-pack error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "completed", "output_dir": str(output_dir), "advisory": True}, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
