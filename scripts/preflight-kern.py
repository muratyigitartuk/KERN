from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.readiness import build_readiness_report


def build_preflight_report(runtime_url: str | None = None) -> dict[str, object]:
    readiness = build_readiness_report(runtime_url=runtime_url)
    legacy_status = "error" if readiness["status"] == "not_ready" else readiness["status"]
    return {
        "status": legacy_status,
        "readiness_status": readiness["status"],
        "headline": readiness["headline"],
        "errors": readiness["errors"],
        "warnings": readiness["warnings"],
        "readiness_checks": readiness["checks"],
        **{key: value for key, value in readiness.items() if key not in {"status", "headline", "checks"}},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KERN deployment preflight checks")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    parser.add_argument("--runtime-url", default=None, help="Optional runtime URL to probe, for example http://127.0.0.1:8000")
    args = parser.parse_args(argv)
    payload = build_preflight_report(runtime_url=args.runtime_url)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"KERN preflight status: {payload['readiness_status']}")
        print(payload["headline"])
        for message in payload["errors"]:
            print(f"ERROR: {message}")
        for message in payload["warnings"]:
            print(f"WARNING: {message}")
    return 1 if payload["readiness_status"] == "not_ready" else 0


if __name__ == "__main__":
    raise SystemExit(main())
