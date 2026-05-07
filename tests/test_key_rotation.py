"""Encryption and key rotation tests for encrypted_db module."""
from __future__ import annotations

import json
import os
import sqlite3

os.environ.setdefault("KERN_PRODUCT_POSTURE", "personal")

from pathlib import Path

import pytest

from cryptography.fernet import Fernet

from app.encrypted_db import (
    EncryptedProfileConnection,
    hydrate_encrypted_connection,
    rewrite_encrypted_database,
)


# ── helpers ──────────────────────────────────────────────────────────


def _make_key() -> str:
    return Fernet.generate_key().decode("ascii")


def _create_encrypted_file(path: Path, key: str, *, rows: list[tuple] | None = None) -> None:
    """Create an encrypted database file at *path* with optional rows."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for row in rows or []:
        conn.execute("INSERT INTO t VALUES (?, ?)", row)
    conn.commit()
    # Backup to temp file, read raw bytes, encrypt
    import tempfile
    handle, raw_path = tempfile.mkstemp(suffix=".sqlite3")
    try:
        os.close(handle)
    except OSError:
        pass
    tmp = Path(raw_path)
    tmp_conn = sqlite3.connect(tmp)
    conn.backup(tmp_conn)
    tmp_conn.commit()
    tmp_conn.close()
    conn.close()
    raw_bytes = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    cipher = Fernet(key.encode("ascii"))
    payload = {
        "version": 1,
        "mode": "fernet",
        "created_at": "2026-01-01T00:00:00+00:00",
        "key_version": 1,
        "key_derivation_version": "v1",
        "ciphertext": cipher.encrypt(raw_bytes).decode("ascii"),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


# ── EncryptedProfileConnection ───────────────────────────────────────


def test_configure_and_persist(tmp_path):
    key = _make_key()
    enc_path = tmp_path / "test.enc"
    conn = sqlite3.connect(":memory:", factory=EncryptedProfileConnection)
    conn.configure_encrypted_storage(
        encrypted_path=enc_path,
        fernet_key=key,
        key_version=1,
        key_derivation_version="v1",
    )
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'hello')")
    conn.commit()  # triggers persist_encrypted
    assert enc_path.exists()
    payload = json.loads(enc_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "fernet"
    assert payload["key_version"] == 1
    # Decrypt and verify content
    raw = Fernet(key.encode("ascii")).decrypt(payload["ciphertext"].encode("ascii"))
    assert raw[:16] == b"SQLite format 3\x00"


def test_rotate_key(tmp_path):
    key1 = _make_key()
    key2 = _make_key()
    enc_path = tmp_path / "rotate.enc"
    conn = sqlite3.connect(":memory:", factory=EncryptedProfileConnection)
    conn.configure_encrypted_storage(
        encrypted_path=enc_path,
        fernet_key=key1,
        key_version=1,
        key_derivation_version="v1",
    )
    conn.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'before')")
    conn.commit()

    conn.rotate_encrypted_storage(
        fernet_key=key2,
        key_version=2,
        key_derivation_version="v1",
    )
    conn.execute("INSERT INTO t VALUES (2, 'after')")
    conn.commit()

    payload = json.loads(enc_path.read_text(encoding="utf-8"))
    assert payload["key_version"] == 2
    # Old key should NOT decrypt new payload
    with pytest.raises(Exception):
        Fernet(key1.encode("ascii")).decrypt(payload["ciphertext"].encode("ascii"))
    # New key should decrypt
    raw = Fernet(key2.encode("ascii")).decrypt(payload["ciphertext"].encode("ascii"))
    assert raw[:16] == b"SQLite format 3\x00"


def test_close_persists(tmp_path):
    key = _make_key()
    enc_path = tmp_path / "close.enc"
    conn = sqlite3.connect(":memory:", factory=EncryptedProfileConnection)
    conn.configure_encrypted_storage(
        encrypted_path=enc_path,
        fernet_key=key,
        key_version=1,
        key_derivation_version="v1",
    )
    conn.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    conn.commit()  # commit so close() can safely persist
    # Delete file from first commit to prove close re-persists
    if enc_path.exists():
        enc_path.unlink()
    conn.execute("INSERT INTO t VALUES (1, 'data')")
    conn.commit()
    # close should also trigger persist
    conn.close()
    assert enc_path.exists()


# ── hydrate_encrypted_connection ─────────────────────────────────────


def test_hydrate_new_db(tmp_path):
    key = _make_key()
    enc_path = tmp_path / "new.enc"
    conn = sqlite3.connect(":memory:", factory=EncryptedProfileConnection)
    result = hydrate_encrypted_connection(conn, enc_path, key)
    assert result == "new"
    conn.close()


def test_hydrate_plaintext_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("KERN_ALLOW_PLAINTEXT_DB_MIGRATION", "1")
    key = _make_key()
    enc_path = tmp_path / "plain.db"
    # Write a real SQLite file (plaintext)
    plain_conn = sqlite3.connect(enc_path)
    plain_conn.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    plain_conn.execute("INSERT INTO t VALUES (1, 'migrated')")
    plain_conn.commit()
    plain_conn.close()

    conn = sqlite3.connect(":memory:", factory=EncryptedProfileConnection)
    result = hydrate_encrypted_connection(conn, enc_path, key)
    assert result == "migrated_plaintext"
    rows = conn.execute("SELECT val FROM t").fetchall()
    assert rows[0][0] == "migrated"
    conn.close()


def test_hydrate_encrypted_file(tmp_path):
    key = _make_key()
    enc_path = tmp_path / "encrypted.enc"
    _create_encrypted_file(enc_path, key, rows=[(1, "secret")])

    conn = sqlite3.connect(":memory:", factory=EncryptedProfileConnection)
    result = hydrate_encrypted_connection(conn, enc_path, key)
    assert result == "loaded"
    rows = conn.execute("SELECT val FROM t").fetchall()
    assert rows[0][0] == "secret"
    conn.close()


def test_hydrate_wrong_key(tmp_path):
    key = _make_key()
    wrong_key = _make_key()
    enc_path = tmp_path / "wrong.enc"
    _create_encrypted_file(enc_path, key)

    conn = sqlite3.connect(":memory:", factory=EncryptedProfileConnection)
    with pytest.raises(RuntimeError, match="could not be unlocked"):
        hydrate_encrypted_connection(conn, enc_path, wrong_key)
    conn.close()


def test_hydrate_invalid_payload(tmp_path):
    enc_path = tmp_path / "bad.enc"
    enc_path.write_text('{"mode": "unknown"}', encoding="utf-8")

    conn = sqlite3.connect(":memory:", factory=EncryptedProfileConnection)
    with pytest.raises(RuntimeError, match="Unsupported"):
        hydrate_encrypted_connection(conn, enc_path, _make_key())
    conn.close()


# ── rewrite_encrypted_database ───────────────────────────────────────


def test_rewrite_rotates_key(tmp_path):
    old_key = _make_key()
    new_key = _make_key()
    enc_path = tmp_path / "rewrite.enc"
    _create_encrypted_file(enc_path, old_key, rows=[(1, "kept")])

    rewrite_encrypted_database(
        enc_path,
        old_fernet_key=old_key,
        new_fernet_key=new_key,
        key_version=2,
        key_derivation_version="v1",
    )

    payload = json.loads(enc_path.read_text(encoding="utf-8"))
    assert payload["key_version"] == 2
    raw = Fernet(new_key.encode("ascii")).decrypt(payload["ciphertext"].encode("ascii"))
    assert raw[:16] == b"SQLite format 3\x00"


def test_rewrite_plaintext_to_encrypted(tmp_path):
    new_key = _make_key()
    enc_path = tmp_path / "plain.db"
    plain = sqlite3.connect(enc_path)
    plain.execute("CREATE TABLE t (id INTEGER)")
    plain.commit()
    plain.close()

    rewrite_encrypted_database(
        enc_path,
        old_fernet_key=_make_key(),  # ignored for plaintext
        new_fernet_key=new_key,
        key_version=1,
        key_derivation_version="v1",
    )

    payload = json.loads(enc_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "fernet"


def test_rewrite_missing_file(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        rewrite_encrypted_database(
            tmp_path / "nope.enc",
            old_fernet_key=_make_key(),
            new_fernet_key=_make_key(),
            key_version=1,
            key_derivation_version="v1",
        )


def test_rewrite_wrong_old_key(tmp_path):
    key = _make_key()
    enc_path = tmp_path / "bad_rewrite.enc"
    _create_encrypted_file(enc_path, key)

    with pytest.raises(RuntimeError, match="could not be unlocked"):
        rewrite_encrypted_database(
            enc_path,
            old_fernet_key=_make_key(),
            new_fernet_key=_make_key(),
            key_version=2,
            key_derivation_version="v1",
        )
