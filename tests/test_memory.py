import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database import connect
from app.documents import DocumentService
from app.memory import MemoryRepository


def test_memory_tracks_morning_greeting(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    assert not repo.has_morning_greeting("2026-03-16")
    repo.mark_morning_greeting("2026-03-16")
    assert repo.has_morning_greeting("2026-03-16")


def test_memory_stores_and_reads_reminders(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    due_at = datetime.now() + timedelta(minutes=5)
    reminder_id = repo.create_reminder("Stretch", due_at)

    pending = repo.list_pending_reminders()

    assert reminder_id > 0
    assert pending
    assert pending[0].title == "Stretch"


def test_memory_can_complete_local_task(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    repo.create_local_task("Review runtime", priority=3)

    completed = repo.complete_local_task("Review runtime")
    tasks = repo.list_local_tasks()

    assert completed is True
    assert all(task.title != "Review runtime" for task in tasks)


def test_memory_stores_runtime_logs(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    repo.append_runtime_log("system", "Runtime boot complete.")

    logs = repo.list_runtime_logs(limit=5)

    assert logs
    assert logs[0]["category"] == "system"
    assert "Runtime boot complete" in logs[0]["message"]


def test_memory_entries_are_append_only(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))

    repo.upsert_fact("preferred_editor", "VS Code")
    repo.upsert_fact("preferred_editor", "Neovim")

    facts = repo.list_facts(limit=5)
    entry_count = repo.connection.execute("SELECT COUNT(*) AS count FROM memory_entries WHERE key = 'preferred_editor'").fetchone()["count"]

    assert entry_count == 2
    assert len(facts) == 1
    assert facts[0].value == "Neovim"
    assert facts[0].status == "active"


def test_memory_migrates_legacy_facts_and_dialogue(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE assistant_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user',
            confidence REAL NOT NULL DEFAULT 1.0,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE conversation_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            summary TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO assistant_facts (key, value, source, confidence, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("preferred_workspace", "Desktop setup", "user", 1.0, datetime.now(timezone.utc).isoformat()),
    )
    connection.execute(
        "INSERT INTO conversation_summaries (created_at, summary) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), "User: hello | Kern: hello back"),
    )
    connection.commit()
    connection.close()

    repo = MemoryRepository(connect(db_path))

    facts = repo.list_facts(limit=5)
    entries = repo.list_recent_conversation_entries(limit=5)

    assert facts
    assert facts[0].key == "preferred_workspace"
    assert entries
    assert "Kern" in entries[0]


def test_new_memory_schema_does_not_create_email_draft_surface(tmp_path: Path):
    db_path = tmp_path / "legacy-email-drafts.db"
    repo = MemoryRepository(connect(db_path))
    tables = {
        row["name"]
        for row in repo.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }

    assert "email_drafts" not in tables


def test_memory_repairs_missing_scheduler_tables(tmp_path: Path):
    db_path = tmp_path / "missing-scheduler.db"
    connect(db_path).close()
    connection = sqlite3.connect(db_path)
    connection.execute("DROP TABLE scheduled_tasks")
    connection.execute("DROP TABLE watch_rules")
    connection.commit()
    connection.close()

    repo = MemoryRepository(connect(db_path))

    assert repo.connection.execute("SELECT COUNT(*) AS count FROM scheduled_tasks").fetchone()["count"] == 0
    assert repo.connection.execute("SELECT COUNT(*) AS count FROM watch_rules").fetchone()["count"] == 0


def test_seed_defaults_can_skip_demo_data(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))

    repo.seed_defaults(include_demo_data=False)

    assert repo.get_value("user_profile", "name") == "Murat"
    assert repo.get_value("preferences", "preferred_title") == ""
    assert repo.list_local_tasks() == []
    assert repo.list_local_events() == []
    assert repo.list_pending_reminders() == []
    assert repo.list_open_loops(limit=5) == []
    assert repo.list_facts(limit=5) == []


def test_memory_maintenance_trims_append_only_tables(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))

    for index in range(repo.CONVERSATION_RETENTION + 25):
        repo.append_conversation_entry(f"turn-{index}")
    for index in range(repo.RUNTIME_LOG_RETENTION + 25):
        repo.append_runtime_log("system", f"log-{index}")
    for index in range(repo.RECEIPT_RETENTION + 25):
        repo.connection.execute(
            """
            INSERT INTO execution_receipts (
                created_at,
                capability_name,
                status,
                message,
                evidence_json,
                side_effects_json,
                suggested_follow_up,
                data_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                "test_capability",
                "attempted",
                f"receipt-{index}",
                "[]",
                "[]",
                None,
                "{}",
            ),
        )
    repo.connection.commit()

    repo.run_maintenance()

    conversation_count = repo.connection.execute("SELECT COUNT(*) AS count FROM conversation_log").fetchone()["count"]
    log_count = repo.connection.execute("SELECT COUNT(*) AS count FROM runtime_logs").fetchone()["count"]
    receipt_count = repo.connection.execute("SELECT COUNT(*) AS count FROM execution_receipts").fetchone()["count"]

    assert conversation_count == repo.CONVERSATION_RETENTION
    assert log_count == repo.RUNTIME_LOG_RETENTION
    assert receipt_count == repo.RECEIPT_RETENTION


def test_memory_consolidation_produces_structured_summary(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    repo.connection.execute("INSERT INTO conversation_log (created_at, content) VALUES (?, ?)", (old, "Remember that I prefer concise answers."))
    repo.connection.execute("INSERT INTO conversation_log (created_at, content) VALUES (?, ?)", (old, "Decision: keep KERN local-first."))
    repo.connection.execute("INSERT INTO conversation_log (created_at, content) VALUES (?, ?)", (old, "Next step: review the invoice before 2026-04-02."))
    repo.connection.commit()

    count = repo.consolidate_memory(older_than_days=30)
    summary = repo.connection.execute(
        "SELECT summary FROM conversation_summaries ORDER BY id DESC LIMIT 1"
    ).fetchone()["summary"]

    assert count == 3
    assert "Preferences:" in summary
    assert "Decisions:" in summary
    assert "Commitments:" in summary
    assert "2026-04-02" in summary
    facts = repo.search_facts("response style")
    loops = repo.list_open_loops(limit=10)
    assert any("concise" in fact.value.lower() for fact in facts)
    assert any("review the invoice" in loop.title.lower() for loop in loops)
    structured = repo.list_facts(limit=20)
    assert any(item.memory_kind == "decision" for item in structured)
    assert any(item.memory_kind == "commitment" for item in structured)
    assert any(item.memory_kind == "episodic_summary" for item in structured)


def test_structured_memory_supersedes_conflicting_preference(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))

    repo.upsert_fact("preferred editor", "VS Code")
    repo.upsert_fact("preferred editor", "Neovim")

    facts = repo.search_facts("working preferences", limit=5)
    rows = repo.connection.execute(
        """
        SELECT value, status
        FROM structured_memory_items
        WHERE key = 'preferred editor'
        ORDER BY created_at ASC
        """
    ).fetchall()

    assert facts
    assert facts[0].value == "Neovim"
    assert rows[0]["status"] == "superseded"
    assert rows[-1]["status"] == "active"


def test_prompt_cache_revision_changes_for_structured_memory(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    before = repo.prompt_cache_revision()

    repo.remember_memory_item(
        "project posture",
        "Keep KERN local-first.",
        memory_kind="decision",
        source="user",
        confidence=0.91,
        provenance={"origin": "unit_test"},
    )

    after = repo.prompt_cache_revision()

    assert before != after


def test_memory_search_document_chunks_matches_natural_language_prompt_terms(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    documents = DocumentService(repo, tmp_path / "documents", tmp_path / "archives")
    source = tmp_path / "acme-offer.txt"
    source.write_text(
        "\n".join(
            [
                "ACME GmbH UI Modernization Offer",
                "Project: UI modernization and validation reporting",
                "Target amount: 4,850 EUR",
            ]
        ),
        encoding="utf-8",
    )
    documents.ingest_document(source)

    hits = repo.search_document_chunks(
        "Aus den hochgeladenen ACME-Dokumenten: Worum geht es im Angebot und wie hoch ist der Zielbetrag?",
        limit=4,
        include_archived=True,
    )

    assert hits
    assert hits[0].metadata["title"] == "acme-offer"
    assert "4,850" in hits[0].text


# ── Extractive summarizer ────────────────────────────────────────────


def test_extractive_summarize_returns_top_sentences():
    contents = [
        "The KERN project uses SQLite for local storage.",
        "ok",
        "We decided to keep the system offline-first.",
        "Random filler that has no key tokens.",
        "KERN project architecture follows a modular pattern with SQLite.",
        "The weather is nice today.",
        "Invoice processing uses German VAT rates of 19 percent.",
        "More random filler without meaning.",
        "SQLite concurrency requires db_retry with exponential backoff.",
        "Short line.",
    ]
    token_freq = {"kern": 3, "sqlite": 3, "project": 2, "offline": 1, "invoice": 1}
    result = MemoryRepository._extractive_summarize(contents, token_freq, top_n=3)
    assert len(result) <= 3
    assert any("KERN" in s or "SQLite" in s for s in result)


def test_extractive_summarize_empty_input():
    assert MemoryRepository._extractive_summarize([], {}) == []


def test_extractive_summarize_preserves_order():
    contents = [
        "First important sentence about KERN architecture.",
        "Second filler line.",
        "Third crucial point about KERN deployment strategy.",
    ]
    token_freq = {"kern": 2, "architecture": 1, "deployment": 1}
    result = MemoryRepository._extractive_summarize(contents, token_freq, top_n=2)
    if len(result) == 2:
        # First selected should come before second in original order
        assert contents.index(result[0]) < contents.index(result[1])


def test_consolidation_includes_key_points(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"))
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    for i in range(20):
        repo.connection.execute(
            "INSERT INTO conversation_log (created_at, content) VALUES (?, ?)",
            (old, f"KERN project discussion item {i}: SQLite performance tuning for concurrent writes."),
        )
    repo.connection.commit()

    count = repo.consolidate_memory(older_than_days=30)
    summary = repo.connection.execute(
        "SELECT summary FROM conversation_summaries ORDER BY id DESC LIMIT 1"
    ).fetchone()["summary"]

    assert count == 20
    assert "Key points:" in summary
    assert "KERN" in summary or "kern" in summary


def test_consolidate_memory_llm_client_stored(tmp_path: Path):
    """Verify llm_client parameter is stored on MemoryRepository."""
    repo = MemoryRepository(connect(tmp_path / "kern.db"), "test", llm_client="fake_client")
    assert repo._llm_client == "fake_client"


@pytest.mark.asyncio
async def test_llm_summarize_returns_none_when_no_client(tmp_path: Path):
    repo = MemoryRepository(connect(tmp_path / "kern.db"), "test")
    result = await repo._llm_summarize(["hello", "world"])
    assert result is None
