from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def db_retry(func, *args, max_attempts: int = 3, **kwargs) -> T:
    """Execute *func* with exponential backoff on ``sqlite3.OperationalError``.

    Retries after 100 ms, 200 ms, 400 ms by default.  Logs each attempt.
    """
    delay = 0.1
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc) or attempt == max_attempts:
                raise
            logger.warning("DB locked (attempt %d/%d), retrying in %.0fms: %s", attempt, max_attempts, delay * 1000, exc)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("db_retry: unreachable")

from app.encrypted_db import EncryptedProfileConnection, hydrate_encrypted_connection


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS key_value_store (
    bucket TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (bucket, key)
);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 1,
    due_at TEXT,
    completed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS local_calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    importance INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS local_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    kind TEXT NOT NULL DEFAULT 'reminder',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assistant_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    confidence REAL NOT NULL DEFAULT 1.0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS open_loops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    details TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    due_at TEXT,
    source TEXT NOT NULL DEFAULT 'assistant',
    related_type TEXT,
    related_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    capability_name TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    side_effects_json TEXT NOT NULL DEFAULT '[]',
    suggested_follow_up TEXT,
    data_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS active_context (
    bucket TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (bucket, key)
);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_type TEXT NOT NULL,
    content_id TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS memory_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user',
            confidence REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_entries_updated_at ON memory_entries(updated_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_entries_key ON memory_entries(key);

        CREATE TABLE IF NOT EXISTS conversation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            content TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conversation_log_created_at ON conversation_log(created_at DESC, id DESC);
        """,
    ),
    (
        2,
        """
        INSERT INTO memory_entries (key, value, source, confidence, created_at, updated_at)
        SELECT key, value, source, confidence, updated_at, updated_at
        FROM assistant_facts
        WHERE NOT EXISTS (SELECT 1 FROM memory_entries);

        INSERT INTO conversation_log (created_at, content)
        SELECT created_at, summary
        FROM conversation_summaries
        WHERE NOT EXISTS (SELECT 1 FROM conversation_log);
        """,
    ),
    (
        3,
        """
        CREATE INDEX IF NOT EXISTS idx_runtime_logs_created_at ON runtime_logs(created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_execution_receipts_created_at ON execution_receipts(created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_local_reminders_status_due_at ON local_reminders(status, due_at, id);
        CREATE INDEX IF NOT EXISTS idx_open_loops_status_due_at ON open_loops(status, due_at, id);
        """,
    ),
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS document_records (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            category TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            archived INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_document_records_imported_at ON document_records(imported_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_document_records_category ON document_records(category);

        CREATE TABLE IF NOT EXISTS document_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES document_records(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id ON document_chunks(document_id, chunk_index);

        CREATE TABLE IF NOT EXISTS conversation_archives (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            file_path TEXT NOT NULL,
            imported_turns INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            archived_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conversation_archives_archived_at ON conversation_archives(archived_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS email_accounts (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            email_address TEXT NOT NULL,
            imap_host TEXT NOT NULL,
            smtp_host TEXT NOT NULL,
            username TEXT,
            password_ref TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mailbox_messages (
            id TEXT PRIMARY KEY,
            account_id TEXT,
            message_id TEXT,
            folder TEXT NOT NULL DEFAULT 'INBOX',
            subject TEXT NOT NULL,
            sender TEXT NOT NULL,
            recipients_json TEXT NOT NULL DEFAULT '[]',
            received_at TEXT NOT NULL,
            body_text TEXT NOT NULL DEFAULT '',
            has_attachments INTEGER NOT NULL DEFAULT 0,
            attachment_paths_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES email_accounts(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mailbox_messages_received_at ON mailbox_messages(received_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_mailbox_messages_account_id ON mailbox_messages(account_id, received_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS meeting_records (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            audio_path TEXT NOT NULL,
            transcript_path TEXT,
            status TEXT NOT NULL DEFAULT 'recorded',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_meeting_records_created_at ON meeting_records(created_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS transcript_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            file_path TEXT,
            content TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(meeting_id) REFERENCES meeting_records(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_transcript_artifacts_meeting_id ON transcript_artifacts(meeting_id, id DESC);

        CREATE TABLE IF NOT EXISTS transcript_action_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id TEXT NOT NULL,
            title TEXT NOT NULL,
            details TEXT,
            due_hint TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_transcript_action_items_meeting_id ON transcript_action_items(meeting_id, id ASC);

        CREATE TABLE IF NOT EXISTS german_business_documents (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            file_path TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_german_business_documents_created_at ON german_business_documents(created_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS sync_targets (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            label TEXT NOT NULL,
            path_or_url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

CREATE TABLE IF NOT EXISTS recovery_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    profile_slug TEXT,
    stage TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recovery_checkpoints_job_id ON recovery_checkpoints(job_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_recovery_checkpoints_profile_slug ON recovery_checkpoints(profile_slug, updated_at DESC, id DESC);
        """,
    ),
    (
        5,
        """
        CREATE TABLE IF NOT EXISTS email_drafts (
            id TEXT PRIMARY KEY,
            to_json TEXT NOT NULL DEFAULT '[]',
            cc_json TEXT NOT NULL DEFAULT '[]',
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            attachments_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_email_drafts_created_at ON email_drafts(created_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS compliance_rules (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            cadence TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_compliance_rules_created_at ON compliance_rules(created_at DESC);
        """,
    ),
    (
        6,
        """
        ALTER TABLE document_records ADD COLUMN profile_slug TEXT;
        UPDATE document_records SET profile_slug = COALESCE(profile_slug, 'default');
        CREATE INDEX IF NOT EXISTS idx_document_records_profile_slug ON document_records(profile_slug, imported_at DESC, id DESC);

        ALTER TABLE conversation_archives ADD COLUMN profile_slug TEXT;
        UPDATE conversation_archives SET profile_slug = COALESCE(profile_slug, 'default');
        CREATE INDEX IF NOT EXISTS idx_conversation_archives_profile_slug ON conversation_archives(profile_slug, archived_at DESC, id DESC);

        ALTER TABLE email_accounts ADD COLUMN profile_slug TEXT;
        UPDATE email_accounts SET profile_slug = COALESCE(profile_slug, 'default');
        CREATE INDEX IF NOT EXISTS idx_email_accounts_profile_slug ON email_accounts(profile_slug, updated_at DESC, id DESC);

        ALTER TABLE mailbox_messages ADD COLUMN profile_slug TEXT;
        UPDATE mailbox_messages SET profile_slug = COALESCE(profile_slug, 'default');
        CREATE INDEX IF NOT EXISTS idx_mailbox_messages_profile_slug ON mailbox_messages(profile_slug, received_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_mailbox_messages_profile_message_id ON mailbox_messages(profile_slug, account_id, message_id);

        ALTER TABLE meeting_records ADD COLUMN profile_slug TEXT;
        UPDATE meeting_records SET profile_slug = COALESCE(profile_slug, 'default');
        CREATE INDEX IF NOT EXISTS idx_meeting_records_profile_slug ON meeting_records(profile_slug, created_at DESC, id DESC);

        ALTER TABLE german_business_documents ADD COLUMN profile_slug TEXT;
        UPDATE german_business_documents SET profile_slug = COALESCE(profile_slug, 'default');
        CREATE INDEX IF NOT EXISTS idx_german_business_documents_profile_slug ON german_business_documents(profile_slug, created_at DESC, id DESC);

        ALTER TABLE sync_targets ADD COLUMN profile_slug TEXT;
        UPDATE sync_targets SET profile_slug = COALESCE(profile_slug, 'default');
        CREATE INDEX IF NOT EXISTS idx_sync_targets_profile_slug ON sync_targets(profile_slug, updated_at DESC, id DESC);

        ALTER TABLE email_drafts ADD COLUMN profile_slug TEXT;
        UPDATE email_drafts SET profile_slug = COALESCE(profile_slug, 'default');
        CREATE INDEX IF NOT EXISTS idx_email_drafts_profile_slug ON email_drafts(profile_slug, created_at DESC, id DESC);

        ALTER TABLE compliance_rules ADD COLUMN profile_slug TEXT;
        UPDATE compliance_rules SET profile_slug = COALESCE(profile_slug, 'default');
        CREATE INDEX IF NOT EXISTS idx_compliance_rules_profile_slug ON compliance_rules(profile_slug, created_at DESC);
        """,
    ),
    (
        7,
        """
        DELETE FROM mailbox_messages
        WHERE id NOT IN (
            SELECT id
            FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY profile_slug, COALESCE(account_id, ''), COALESCE(message_id, '')
                           ORDER BY received_at DESC, id DESC
                       ) AS row_number
                FROM mailbox_messages
                WHERE message_id IS NOT NULL AND message_id != ''
            )
            WHERE row_number = 1
        )
        AND message_id IS NOT NULL
        AND message_id != '';

        CREATE UNIQUE INDEX IF NOT EXISTS idx_mailbox_messages_profile_message_id_unique
        ON mailbox_messages(profile_slug, account_id, message_id)
        WHERE message_id IS NOT NULL AND message_id != '';

        ALTER TABLE transcript_artifacts ADD COLUMN profile_slug TEXT;
        UPDATE transcript_artifacts
        SET profile_slug = COALESCE(
            profile_slug,
            (
                SELECT mr.profile_slug
                FROM meeting_records mr
                WHERE mr.id = transcript_artifacts.meeting_id
                LIMIT 1
            ),
            'default'
        );
        CREATE INDEX IF NOT EXISTS idx_transcript_artifacts_profile_slug
        ON transcript_artifacts(profile_slug, created_at DESC, id DESC);

        ALTER TABLE transcript_action_items ADD COLUMN profile_slug TEXT;
        UPDATE transcript_action_items
        SET profile_slug = COALESCE(
            profile_slug,
            (
                SELECT mr.profile_slug
                FROM meeting_records mr
                WHERE mr.id = transcript_action_items.meeting_id
                LIMIT 1
            ),
            'default'
        );
        CREATE INDEX IF NOT EXISTS idx_transcript_action_items_profile_slug
        ON transcript_action_items(profile_slug, created_at DESC, id DESC);
        """,
    ),
    (
        8,
        """
        ALTER TABLE document_records ADD COLUMN file_hash TEXT;
        CREATE INDEX IF NOT EXISTS idx_document_records_file_hash ON document_records(file_hash);
        """,
    ),
    (
        9,
        """
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            title TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            action_type TEXT NOT NULL DEFAULT 'custom_prompt',
            action_payload_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            last_run_at TEXT,
            next_run_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_profile_slug ON scheduled_tasks(profile_slug, enabled, next_run_at);

        CREATE TABLE IF NOT EXISTS watch_rules (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            rule_type TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            last_triggered_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_watch_rules_profile_slug ON watch_rules(profile_slug, rule_type, enabled);
        """,
    ),
    (
        10,
        """
        ALTER TABLE conversation_log ADD COLUMN topic_tags_json TEXT DEFAULT '[]';
        ALTER TABLE conversation_log ADD COLUMN session_id TEXT;
        CREATE INDEX IF NOT EXISTS idx_conversation_log_session_id ON conversation_log(session_id);
        """,
    ),
    (
        11,
        """
        CREATE TABLE IF NOT EXISTS knowledge_entities (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            entity_type TEXT NOT NULL,
            name TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_entities_profile_slug ON knowledge_entities(profile_slug, entity_type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_knowledge_entities_name ON knowledge_entities(profile_slug, name);

        CREATE TABLE IF NOT EXISTS knowledge_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_entity_id TEXT NOT NULL,
            target_entity_id TEXT NOT NULL,
            relationship TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            source_document_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(source_entity_id) REFERENCES knowledge_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(target_entity_id) REFERENCES knowledge_entities(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_edges_source ON knowledge_edges(source_entity_id);
        CREATE INDEX IF NOT EXISTS idx_knowledge_edges_target ON knowledge_edges(target_entity_id);
        """,
    ),
    (
        12,
        """
        ALTER TABLE scheduled_tasks ADD COLUMN run_status TEXT NOT NULL DEFAULT 'idle';
        ALTER TABLE scheduled_tasks ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE scheduled_tasks ADD COLUMN retry_attempts INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE scheduled_tasks ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 2;
        ALTER TABLE scheduled_tasks ADD COLUMN last_error TEXT;
        ALTER TABLE scheduled_tasks ADD COLUMN last_result_json TEXT NOT NULL DEFAULT '{}';
        """,
    ),
    (
        13,
        """
        CREATE TABLE IF NOT EXISTS structured_memory_items (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            memory_kind TEXT NOT NULL DEFAULT 'fact',
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            entity_key TEXT,
            source TEXT NOT NULL DEFAULT 'user',
            confidence REAL NOT NULL DEFAULT 1.0,
            status TEXT NOT NULL DEFAULT 'active',
            superseded_by_id TEXT,
            provenance_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_structured_memory_items_profile_slug
        ON structured_memory_items(profile_slug, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_structured_memory_items_kind
        ON structured_memory_items(profile_slug, memory_kind, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_structured_memory_items_key
        ON structured_memory_items(profile_slug, key, status);
        CREATE INDEX IF NOT EXISTS idx_structured_memory_items_entity
        ON structured_memory_items(profile_slug, entity_key, status);

        INSERT INTO structured_memory_items (
            id,
            profile_slug,
            memory_kind,
            key,
            value,
            entity_key,
            source,
            confidence,
            status,
            superseded_by_id,
            provenance_json,
            created_at,
            updated_at
        )
        SELECT
            'legacy-memory-' || id,
            'default',
            CASE
                WHEN lower(key) LIKE '%prefer%' OR lower(key) IN ('response style', 'preferred editor', 'preferred_title')
                    THEN 'preference'
                ELSE 'fact'
            END,
            key,
            value,
            NULL,
            source,
            confidence,
            'active',
            NULL,
            '{}',
            created_at,
            updated_at
        FROM memory_entries
        WHERE NOT EXISTS (SELECT 1 FROM structured_memory_items);
        """,
    ),
    (
        14,
        """
        ALTER TABLE document_records ADD COLUMN organization_id TEXT;
        ALTER TABLE document_records ADD COLUMN workspace_id TEXT;
        ALTER TABLE document_records ADD COLUMN actor_user_id TEXT;

        ALTER TABLE structured_memory_items ADD COLUMN organization_id TEXT;
        ALTER TABLE structured_memory_items ADD COLUMN workspace_slug TEXT;
        ALTER TABLE structured_memory_items ADD COLUMN user_id TEXT;
        ALTER TABLE structured_memory_items ADD COLUMN data_class TEXT NOT NULL DEFAULT 'operational';
        ALTER TABLE structured_memory_items ADD COLUMN promotion_state TEXT NOT NULL DEFAULT 'none';
        ALTER TABLE structured_memory_items ADD COLUMN approved_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE structured_memory_items ADD COLUMN rejected_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE structured_memory_items ADD COLUMN last_feedback_at TEXT;

        CREATE TABLE IF NOT EXISTS regulated_documents (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            workspace_slug TEXT NOT NULL DEFAULT 'default',
            organization_id TEXT,
            data_class TEXT NOT NULL DEFAULT 'regulated_business',
            title TEXT NOT NULL,
            document_id TEXT,
            business_document_id TEXT,
            current_version_id TEXT,
            current_version_number INTEGER NOT NULL DEFAULT 0,
            immutability_state TEXT NOT NULL DEFAULT 'draft',
            retention_state TEXT NOT NULL DEFAULT 'standard',
            finalized_at TEXT,
            finalized_by_user_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_regulated_documents_profile_slug
        ON regulated_documents(profile_slug, updated_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_regulated_documents_workspace_slug
        ON regulated_documents(workspace_slug, updated_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS regulated_document_versions (
            id TEXT PRIMARY KEY,
            regulated_document_id TEXT NOT NULL,
            version_number INTEGER NOT NULL,
            supersedes_version_id TEXT,
            document_id TEXT,
            business_document_id TEXT,
            file_path TEXT,
            content_digest TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(regulated_document_id) REFERENCES regulated_documents(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_regulated_document_versions_document
        ON regulated_document_versions(regulated_document_id, version_number DESC, id DESC);

        CREATE TABLE IF NOT EXISTS memory_feedback_signals (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            organization_id TEXT,
            workspace_slug TEXT,
            user_id TEXT,
            signal_type TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            memory_item_id TEXT,
            approved_for_training INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_feedback_profile_slug
        ON memory_feedback_signals(profile_slug, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_feedback_memory_item
        ON memory_feedback_signals(memory_item_id, created_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS training_examples (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            organization_id TEXT,
            workspace_slug TEXT,
            user_id TEXT,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            input_text TEXT NOT NULL,
            output_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate',
            approved_for_training INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_training_examples_profile_slug
        ON training_examples(profile_slug, status, updated_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_training_examples_source
        ON training_examples(source_type, source_id, updated_at DESC, id DESC);
        """,
    ),
    (
        15,
        """
        CREATE TABLE IF NOT EXISTS workflow_records (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            organization_id TEXT,
            workspace_slug TEXT,
            actor_user_id TEXT,
            workflow_type TEXT NOT NULL,
            subject_refs_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'open',
            last_event TEXT NOT NULL DEFAULT '',
            next_expected_step TEXT NOT NULL DEFAULT '',
            blocking_reasons_json TEXT NOT NULL DEFAULT '[]',
            due_at TEXT,
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL DEFAULT 0.0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_records_scope
        ON workflow_records(profile_slug, workspace_slug, updated_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_workflow_records_type
        ON workflow_records(profile_slug, workflow_type, status, updated_at DESC);

        CREATE TABLE IF NOT EXISTS workflow_events (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            organization_id TEXT,
            workspace_slug TEXT,
            actor_user_id TEXT,
            workflow_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(workflow_id) REFERENCES workflow_records(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_events_workflow
        ON workflow_events(workflow_id, created_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS obligation_records (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            organization_id TEXT,
            workspace_slug TEXT,
            actor_user_id TEXT,
            workflow_id TEXT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            reason TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 1,
            due_at TEXT,
            blocking_reasons_json TEXT NOT NULL DEFAULT '[]',
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(workflow_id) REFERENCES workflow_records(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_obligation_records_scope
        ON obligation_records(profile_slug, workspace_slug, status, priority DESC, due_at ASC, updated_at DESC);

        CREATE TABLE IF NOT EXISTS decision_records (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            organization_id TEXT,
            workspace_slug TEXT,
            actor_user_id TEXT,
            decision_kind TEXT NOT NULL,
            decision_value TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            reasoning_source TEXT NOT NULL DEFAULT 'system_decision',
            rationale TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_decision_records_scope
        ON decision_records(profile_slug, workspace_slug, created_at DESC, id DESC);
        """,
    ),
    (
        16,
        """
        CREATE TABLE IF NOT EXISTS workflow_domain_events (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            organization_id TEXT,
            workspace_slug TEXT,
            actor_user_id TEXT,
            workflow_id TEXT NOT NULL,
            workflow_type TEXT NOT NULL,
            event_type TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_domain_events_workflow
        ON workflow_domain_events(workflow_id, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_workflow_domain_events_scope
        ON workflow_domain_events(profile_slug, workspace_slug, workflow_type, created_at DESC);

        CREATE TABLE IF NOT EXISTS shadow_ranking_records (
            id TEXT PRIMARY KEY,
            profile_slug TEXT NOT NULL DEFAULT 'default',
            organization_id TEXT,
            workspace_slug TEXT,
            actor_user_id TEXT,
            workflow_id TEXT,
            recommendation_id TEXT,
            policy_name TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0.0,
            features_json TEXT NOT NULL DEFAULT '{}',
            outcome_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_shadow_ranking_scope
        ON shadow_ranking_records(profile_slug, workspace_slug, created_at DESC, id DESC);
        """,
    ),
]


def _current_schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def get_schema_version(connection: sqlite3.Connection) -> int:
    return _current_schema_version(connection)


def _set_schema_version(connection: sqlite3.Connection, version: int) -> None:
    connection.execute(
        """
        INSERT INTO schema_meta (key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(version),),
    )


def _apply_migrations(connection: sqlite3.Connection) -> None:
    version = _current_schema_version(connection)
    for target_version, script in MIGRATIONS:
        if version >= target_version:
            continue
        connection.executescript(script)
        _set_schema_version(connection, target_version)
        version = target_version


def _ensure_profile_compat(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(recovery_checkpoints)").fetchall()}
    if "profile_slug" not in columns:
        connection.execute("ALTER TABLE recovery_checkpoints ADD COLUMN profile_slug TEXT")
    connection.execute("UPDATE recovery_checkpoints SET profile_slug = COALESCE(profile_slug, 'default')")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_recovery_checkpoints_profile_slug ON recovery_checkpoints(profile_slug, updated_at DESC, id DESC)"
    )
    scheduled_task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(scheduled_tasks)").fetchall()}
    if scheduled_task_columns and "run_started_at" not in scheduled_task_columns:
        connection.execute("ALTER TABLE scheduled_tasks ADD COLUMN run_started_at TEXT")
    structured_memory_columns = {row["name"] for row in connection.execute("PRAGMA table_info(structured_memory_items)").fetchall()}
    if structured_memory_columns:
        for name, definition in (
            ("organization_id", "TEXT"),
            ("workspace_slug", "TEXT"),
            ("user_id", "TEXT"),
            ("data_class", "TEXT NOT NULL DEFAULT 'operational'"),
            ("promotion_state", "TEXT NOT NULL DEFAULT 'none'"),
            ("approved_count", "INTEGER NOT NULL DEFAULT 0"),
            ("rejected_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_feedback_at", "TEXT"),
        ):
            if name not in structured_memory_columns:
                connection.execute(f"ALTER TABLE structured_memory_items ADD COLUMN {name} {definition}")
    regulated_version_columns = {row["name"] for row in connection.execute("PRAGMA table_info(regulated_document_versions)").fetchall()}
    if regulated_version_columns and "version_chain_digest" not in regulated_version_columns:
        connection.execute("ALTER TABLE regulated_document_versions ADD COLUMN version_chain_digest TEXT NOT NULL DEFAULT ''")


def load_sqlite_vec(connection: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension. Returns True on success."""
    try:
        import sqlite_vec

        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        return True
    except Exception as exc:
        logger.debug("sqlite-vec extension unavailable: %s", exc)
        return False


def ensure_vec_table(connection: sqlite3.Connection, dimensions: int) -> None:
    """Create the vec_embeddings virtual table if it doesn't exist."""
    connection.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
            content_type TEXT,
            content_id TEXT,
            embedding float[{dimensions}]
        )
        """
    )
    connection.commit()


def connect(
    db_path: Path,
    *,
    encryption_mode: str = "off",
    encryption_key: str | None = None,
    key_version: int = 0,
    key_derivation_version: str = "v1",
) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if encryption_mode != "off":
        if not encryption_key:
            raise RuntimeError("Encrypted database mode requires an encryption key.")
        connection = sqlite3.connect(":memory:", check_same_thread=False, factory=EncryptedProfileConnection)
        if not isinstance(connection, EncryptedProfileConnection):
            raise RuntimeError("Encrypted database factory did not return an EncryptedProfileConnection.")
        connection.configure_encrypted_storage(
            encrypted_path=db_path,
            fernet_key=encryption_key,
            key_version=key_version,
            key_derivation_version=key_derivation_version,
        )
        hydrate_encrypted_connection(connection, db_path, encryption_key)
    else:
        connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(SCHEMA)
    _apply_migrations(connection)
    _ensure_profile_compat(connection)
    connection.commit()
    return connection
