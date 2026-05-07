from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.config import settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


@dataclass(slots=True)
class LicenseEvaluation:
    status: str
    plan: str
    activation_mode: str
    expires_at: str
    grace_state: str
    message: str
    renewal_hint: str
    production_access: bool
    sample_access: bool
    install_id: str
    source_path: str
    features: list[str]


class LicenseService:
    def __init__(self) -> None:
        self.license_root = settings.license_root
        self.license_root.mkdir(parents=True, exist_ok=True)
        self.license_path = self.license_root / "current.kern-license.json"
        self.install_id_path = self.license_root / "install-id"

    def install_id(self) -> str:
        if self.install_id_path.exists():
            install_id = self.install_id_path.read_text(encoding="utf-8").strip()
            if install_id:
                return install_id
        install_id = secrets.token_hex(16)
        self.install_id_path.write_text(install_id, encoding="utf-8")
        return install_id

    def current_license_payload(self) -> dict[str, Any] | None:
        if not self.license_path.exists():
            return None
        try:
            return json.loads(self.license_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def evaluate(self) -> LicenseEvaluation:
        install_id = self.install_id()
        source_path = str(self.license_path) if self.license_path.exists() else ""
        payload = self.current_license_payload()
        if payload is None:
            return LicenseEvaluation(
                status="unlicensed",
                plan="No license",
                activation_mode="offline_license_file",
                expires_at="",
                grace_state="",
                message="No offline license file is installed. Production actions are disabled on this machine.",
                renewal_hint="Import a signed offline license file before using the production workspace.",
                production_access=False,
                sample_access=True,
                install_id=install_id,
                source_path=source_path,
                features=[],
            )

        verified, verified_payload, reason = self._verify_payload(payload)
        if not verified or verified_payload is None:
            return LicenseEvaluation(
                status="invalid",
                plan="Invalid license",
                activation_mode="offline_license_file",
                expires_at="",
                grace_state="",
                message=reason or "KERN could not validate the installed license file. Production actions are disabled.",
                renewal_hint="Replace the license file with a valid signed copy issued for this install.",
                production_access=False,
                sample_access=True,
                install_id=install_id,
                source_path=source_path,
                features=[],
            )

        plan = str(verified_payload.get("plan") or "Pilot")
        activation_mode = str(verified_payload.get("activation_mode") or "offline_license_file")
        expires = _parse_datetime(str(verified_payload.get("expires_at") or ""))
        issued = _parse_datetime(str(verified_payload.get("issued_at") or ""))
        grace_days = int(verified_payload.get("grace_days") or 0)
        bound_install = str(verified_payload.get("bound_install") or "").strip()
        features = [str(item) for item in list(verified_payload.get("features") or []) if str(item).strip()]
        allow_sample = bool(verified_payload.get("sample_access", True))
        explicit_status = str(verified_payload.get("status") or "").strip().lower()

        if bound_install and bound_install != install_id:
            return LicenseEvaluation(
                status="invalid",
                plan=plan,
                activation_mode=activation_mode,
                expires_at=expires.isoformat() if expires else "",
                grace_state="",
                message="This license file belongs to a different KERN install. Production actions are disabled.",
                renewal_hint="Import a license file issued for this machine/install before using production features.",
                production_access=False,
                sample_access=True,
                install_id=install_id,
                source_path=source_path,
                features=features,
            )

        now = _utcnow()
        grace_state = ""
        production_access = True
        status = explicit_status if explicit_status in {"trial", "active"} else "active"
        message = "Offline pilot license is active."

        if expires:
            grace_until = expires + timedelta(days=max(grace_days, 0))
            if now > grace_until:
                status = "expired"
                production_access = False
                message = "The offline license expired and the grace period ended. Production actions are disabled."
            elif now > expires:
                status = "active"
                production_access = True
                grace_state = f"Grace period active until {grace_until.date().isoformat()}."
                message = "The offline license is past its expiry date, but the grace period still allows production use."
            elif explicit_status == "trial" or "trial" in plan.lower():
                status = "trial"
                message = "Trial license is active for this install."

        return LicenseEvaluation(
            status=status,
            plan=plan,
            activation_mode=activation_mode,
            expires_at=expires.isoformat() if expires else "",
            grace_state=grace_state,
            message=message,
            renewal_hint="Replace the offline license file to renew or extend access.",
            production_access=production_access,
            sample_access=allow_sample,
            install_id=install_id,
            source_path=source_path,
            features=features,
        )

    def import_license_file(self, source_path: Path) -> LicenseEvaluation:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        verified, _, reason = self._verify_payload(payload)
        if not verified:
            raise ValueError(reason or "KERN could not validate the offline license file.")
        self.license_root.mkdir(parents=True, exist_ok=True)
        self.license_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return self.evaluate()

    def _verify_payload(self, payload: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str]:
        if not isinstance(payload, dict):
            return False, None, "The license file is not valid JSON."
        license_payload = payload.get("payload")
        signature = str(payload.get("signature") or "").strip()
        if not isinstance(license_payload, dict) or not signature:
            return False, None, "The license file is missing payload or signature fields."
        public_key = self._load_public_key()
        if public_key is None:
            return False, None, "No license verification key is configured for this install."
        try:
            signature_bytes = base64.b64decode(signature.encode("ascii"))
            public_key.verify(signature_bytes, _canonical_json(license_payload))
        except Exception:
            return False, None, "The license signature is invalid or has been tampered with."
        return True, license_payload, ""

    def _load_public_key(self) -> Ed25519PublicKey | None:
        raw = (settings.license_public_key or "").strip()
        if raw:
            key = self._parse_public_key(raw)
            if key is not None:
                return key
        if settings.license_public_key_path and settings.license_public_key_path.exists():
            key = self._parse_public_key(settings.license_public_key_path.read_text(encoding="utf-8"))
            if key is not None:
                return key
        return None

    def _parse_public_key(self, raw: str) -> Ed25519PublicKey | None:
        text = raw.strip()
        if not text:
            return None
        try:
            if "BEGIN PUBLIC KEY" in text:
                key = serialization.load_pem_public_key(text.encode("utf-8"))
                if isinstance(key, Ed25519PublicKey):
                    return key
            decoded = base64.b64decode(text.encode("ascii"))
            if len(decoded) == 32:
                return Ed25519PublicKey.from_public_bytes(decoded)
        except Exception:
            return None
        return None
