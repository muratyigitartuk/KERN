from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path


RISKY_FILE_NAMES = {
    ".env",
    "kern-system.db",
    "kern-system.key",
}

RISKY_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".key",
    ".pem",
    ".pfx",
    ".p12",
    ".log",
    ".pyc",
    ".pyo",
    ".gguf",
    ".safetensors",
    ".bin",
}

RISKY_PATH_PARTS = {
    ".kern",
    ".kern-desktop",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "desktop-runtime",
    "models",
    "output",
    "target",
    "tools",
}

SECRET_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\s*=\s*['\"][^'\"\s#]{12,}['\"]"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(r"(?i)postgres(?:ql)?://[^:\s]+:[^@\s]+@"),
    re.compile(r"(?i)redis://[^:\s]+:[^@\s]+@"),
    re.compile(r"C:\\Users\\[^\\\r\n]+\\"),
]

ALLOWLISTED_TRACKED = {
    ".env.example",
    "docs/troubleshooting-guide.md",
    "docs/windows-deployment.md",
    "README.md",
    "README-CURRENT.md",
    "scripts/validate-publish-hygiene.py",
    "tests/test_powershell_scripts.py",
}


@dataclass(frozen=True)
class Finding:
    scope: str
    path: str
    reason: str


def run_git(root: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def is_text_file(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\x00" not in chunk


def risky_path_reason(path_text: str) -> str | None:
    normalized = path_text.replace("\\", "/")
    name = Path(normalized).name
    suffix = Path(normalized).suffix.lower()
    lowered = normalized.lower()
    if name in RISKY_FILE_NAMES:
        return f"forbidden file name: {name}"
    if suffix in RISKY_SUFFIXES:
        return f"forbidden file type: {suffix}"
    for part in RISKY_PATH_PARTS:
        if lowered == part or lowered.startswith(f"{part}/"):
            return f"forbidden local/generated path segment: {part}"
    return None


def scan_text(scope: str, display_path: str, text: str, *, allow_examples: bool) -> list[Finding]:
    findings: list[Finding] = []
    normalized_path = display_path.replace("\\", "/")
    if allow_examples and normalized_path.startswith("tests/"):
        return findings
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            if allow_examples and normalized_path in ALLOWLISTED_TRACKED:
                continue
            findings.append(Finding(scope, display_path, f"matched sensitive pattern: {pattern.pattern}"))
    return findings


def scan_tracked_files(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    paths = sorted(set(run_git(root, "ls-files") + run_git(root, "ls-files", "--others", "--exclude-standard")))
    for relative in paths:
        reason = risky_path_reason(relative)
        if reason:
            findings.append(Finding("git", relative, reason))
            continue
        path = root / relative
        if path.is_file() and is_text_file(path):
            findings.extend(scan_text("git", relative, path.read_text(encoding="utf-8", errors="replace"), allow_examples=True))
    return findings


def scan_zip(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            reason = risky_path_reason(info.filename)
            if reason:
                findings.append(Finding("package", info.filename, reason))
                continue
            if info.file_size <= 2_000_000:
                data = archive.read(info)
                if b"\x00" not in data[:4096]:
                    text = data.decode("utf-8", errors="replace")
                    findings.extend(scan_text("package", info.filename, text, allow_examples=True))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate KERN publishing hygiene.")
    parser.add_argument("--root", default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--package", action="append", default=[], help="Optional release zip to inspect.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    findings = scan_tracked_files(root)
    for package in args.package:
        findings.extend(scan_zip(Path(package).resolve()))

    payload = {
        "ok": not findings,
        "findings": [finding.__dict__ for finding in findings],
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        if findings:
            print("Publishing hygiene failed:")
            for finding in findings:
                print(f"- [{finding.scope}] {finding.path}: {finding.reason}")
        else:
            print("Publishing hygiene passed.")

    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
