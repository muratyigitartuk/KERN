from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.artifacts import ArtifactStore
from app.encrypted_db import EncryptedProfileConnection, hydrate_encrypted_connection
from app.path_safety import safe_content_disposition, validate_user_import_path, validate_workspace_slug


@pytest.mark.parametrize("slug", ["default", "workspace-1", "a" * 63])
def test_validate_workspace_slug_accepts_canonical_slugs(slug: str) -> None:
    assert validate_workspace_slug(slug) == slug


@pytest.mark.parametrize(
    "slug",
    ["", "..", "../x", "x/y", "x\\y", "C:", "Upper", "-bad", "bad-", "con", "a" * 64],
)
def test_validate_workspace_slug_rejects_path_and_reserved_values(slug: str) -> None:
    with pytest.raises(ValueError):
        validate_workspace_slug(slug)


def test_validate_user_import_path_rejects_outside_roots_and_symlinks(tmp_path: Path) -> None:
    documents = tmp_path / "profile" / "documents"
    documents.mkdir(parents=True)
    approved = documents / "ok.txt"
    approved.write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("no", encoding="utf-8")
    profile = SimpleNamespace(documents_root=documents, archives_root=tmp_path / "archives", attachments_root=tmp_path / "attachments")

    assert validate_user_import_path(approved, profile) == approved.resolve()
    with pytest.raises(ValueError):
        validate_user_import_path(outside, profile)

    symlink = documents / "linked.txt"
    try:
        symlink.symlink_to(outside)
    except (OSError, NotImplementedError):
        return
    with pytest.raises(ValueError):
        validate_user_import_path(symlink, profile)


def test_safe_content_disposition_sanitizes_header_filename() -> None:
    headers = safe_content_disposition('report"; bad=\r\nx-evil: 1.json')
    value = headers["Content-Disposition"]

    assert "\r" not in value
    assert "\n" not in value
    assert 'filename="' in value
    assert "filename*=UTF-8''" in value


def test_encrypted_artifact_import_converts_in_root_plaintext(tmp_path: Path, monkeypatch) -> None:
    from app import artifacts as artifacts_module
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    profile = SimpleNamespace(slug="default", documents_root=tmp_path, attachments_root=tmp_path, archives_root=tmp_path, meetings_root=tmp_path, backups_root=tmp_path, profile_root=tmp_path)
    platform = SimpleNamespace(
        assert_profile_unlocked=lambda *args, **kwargs: None,
        ensure_profile_artifact_encryption=lambda *args, **kwargs: {"artifact_key_ref": "artifact-key"},
        resolve_secret=lambda *args, **kwargs: key,
    )
    monkeypatch.setattr(artifacts_module.settings, "artifact_encryption_enabled", True)
    source = tmp_path / "plain.txt"
    source.write_text("secret", encoding="utf-8")

    imported = ArtifactStore(platform, profile).import_file(source, tmp_path)

    assert imported.suffix == ".kenc"
    assert imported.exists()
    assert not source.exists()
    payload = json.loads(imported.read_text(encoding="utf-8"))
    assert payload["ciphertext"]


def test_encrypted_db_plaintext_migration_requires_explicit_opt_in(tmp_path: Path, monkeypatch) -> None:
    encrypted_path = tmp_path / "encrypted.db"
    plaintext = sqlite3.connect(encrypted_path)
    plaintext.execute("CREATE TABLE sample(id INTEGER)")
    plaintext.commit()
    plaintext.close()
    monkeypatch.delenv("KERN_ALLOW_PLAINTEXT_DB_MIGRATION", raising=False)

    connection = EncryptedProfileConnection(":memory:")
    with pytest.raises(RuntimeError):
        hydrate_encrypted_connection(connection, encrypted_path, "x" * 44)
    connection.close()
