from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re

from app.types import IntentType, ToolRequest


KNOWN_APPS = {
    "spotify",
    "notepad",
    "calculator",
    "chrome",
    "edge",
    "discord",
    "explorer",
    "paint",
    "vscode",
    "visual studio code",
    "terminal",
    "cmd",
}


@dataclass(slots=True)
class ParsedIntent:
    intent_type: IntentType
    intent_name: str
    response_hint: str
    confidence: float
    missing_slots: list[str]
    follow_up_expected: bool = False
    tool_request: ToolRequest | None = None


class RuleBasedIntentEngine:
    def parse(self, text: str, dialogue_context: dict[str, str] | None = None) -> ParsedIntent:
        lowered = text.lower().strip()
        normalized = self._normalize_text(text)
        dialogue_context = dialogue_context or {}

        if any(token in lowered for token in ["play something calmer", "something calmer", "not that"]):
            fallback_query = dialogue_context.get("last_media_query") or "morning jazz"
            query = "calm instrumental focus" if "calmer" in lowered else fallback_query
            return self._parsed(
                "action",
                "media_refine",
                "Refine the last media request.",
                0.82,
                ToolRequest(
                    tool_name="play_spotify",
                    arguments={"query": query, "mode": "search_and_play"},
                    user_utterance=text,
                    reason="The user refined the last media request.",
                ),
            )

        if lowered.startswith("set memory scope"):
            scope = lowered.replace("set memory scope", "", 1).strip(" :.")
            return self._parsed(
                "action",
                "set_memory_scope",
                "Update the active memory scope.",
                0.9,
                ToolRequest(
                    tool_name="set_memory_scope",
                    arguments={"scope": scope.replace(" ", "_")},
                    user_utterance=text,
                    reason="The user explicitly asked to update memory scope.",
                ),
            )

        if lowered.startswith("import document") or lowered.startswith("ingest document"):
            file_path = text.split("document", 1)[-1].strip(" :.")
            return ParsedIntent(
                intent_type="action",
                intent_name="ingest_document",
                response_hint="Import a local document.",
                confidence=0.84,
                missing_slots=["file_path"] if not file_path else [],
                follow_up_expected=not file_path,
                tool_request=ToolRequest(
                    tool_name="ingest_document",
                    arguments={"file_path": file_path},
                    user_utterance=text,
                    reason="The user asked to ingest a document.",
                ) if file_path else None,
            )

        eval_style_document_query = self._extract_eval_style_document_search_query(text)
        if eval_style_document_query is not None:
            return self._parsed(
                "query",
                "search_documents",
                "Search ingested documents.",
                0.93,
                ToolRequest(
                    tool_name="search_documents",
                    arguments={"query": eval_style_document_query},
                    user_utterance=text,
                    reason="The user asked to search the local document store for a named document.",
                ),
            )

        document_query = self._extract_document_search_query(text)
        if document_query is not None:
            return self._parsed(
                "query",
                "search_documents",
                "Search ingested documents.",
                0.82,
                ToolRequest(
                    tool_name="search_documents",
                    arguments={"query": document_query},
                    user_utterance=text,
                    reason="The user asked to search documents.",
                ),
            )

        compare_pair = self._extract_compare_document_names(text)
        if compare_pair is not None:
            left_document, right_document = compare_pair
            return self._parsed(
                "query",
                "compare_documents",
                "Compare two named local documents.",
                0.94,
                ToolRequest(
                    tool_name="compare_documents",
                    arguments={
                        "left_document": left_document,
                        "right_document": right_document,
                    },
                    user_utterance=text,
                    reason="The user asked to compare two named local documents.",
                ),
            )

        document_to_summarize = self._extract_document_summary_name(text)
        if document_to_summarize is not None:
            return self._parsed(
                "query",
                "summarize_document",
                "Summarize one named local document.",
                0.94,
                ToolRequest(
                    tool_name="summarize_document",
                    arguments={"document_name": document_to_summarize},
                    user_utterance=text,
                    reason="The user asked for a concise local summary of a named document.",
                ),
            )

        if lowered.startswith("import chatgpt archive") or lowered.startswith("import claude archive"):
            source = "chatgpt" if "chatgpt" in lowered else "claude"
            file_path = text.split("archive", 1)[-1].strip(" :.")
            return ParsedIntent(
                intent_type="action",
                intent_name="import_conversation_archive",
                response_hint="Import a conversation archive.",
                confidence=0.8,
                missing_slots=["file_path"] if not file_path else [],
                follow_up_expected=not file_path,
                tool_request=ToolRequest(
                    tool_name="import_conversation_archive",
                    arguments={"file_path": file_path, "source": source},
                    user_utterance=text,
                    reason="The user asked to import a conversation archive.",
                ) if file_path else None,
            )

        if (
            lowered.startswith("schedule") or "create schedule" in lowered or "add schedule" in lowered
            or "erstelle termin" in lowered or "neuer termin" in lowered
        ):
            title = text.split("schedule", 1)[-1].strip(" :.") if "schedule" in lowered else text.split("termin", 1)[-1].strip(" :.")
            return self._parsed(
                "action",
                "create_schedule",
                "Create a scheduled task.",
                0.82,
                ToolRequest(
                    tool_name="create_schedule",
                    arguments={"title": title or "Scheduled task", "cron_expression": "0 8 * * *"},
                    user_utterance=text,
                    reason="The user asked to create a schedule.",
                ),
            )

        if (
            "list schedule" in lowered or "show schedule" in lowered or "my schedule" in lowered
            or "zeige termine" in lowered or "meine termine" in lowered or "termine anzeigen" in lowered
        ):
            return self._parsed(
                "query",
                "list_schedules",
                "List scheduled tasks.",
                0.85,
                ToolRequest(
                    tool_name="list_schedules",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked to see scheduled tasks.",
                ),
            )

        if (
            "watch folder" in lowered or "monitor folder" in lowered
            or "ordner beobachten" in lowered or "ordner überwachen" in lowered or "verzeichnis überwachen" in lowered
        ):
            if "folder" in lowered:
                folder = text.split("folder", 1)[-1].strip(" :.")
            elif "ordner" in lowered:
                folder = text.split("ordner", 1)[-1].replace("beobachten", "").replace("überwachen", "").strip(" :.")
            elif "verzeichnis" in lowered:
                folder = text.split("verzeichnis", 1)[-1].replace("überwachen", "").strip(" :.")
            else:
                folder = ""
            return self._parsed(
                "action",
                "watch_folder",
                "Watch a folder for new files.",
                0.83,
                ToolRequest(
                    tool_name="watch_folder",
                    arguments={"folder_path": folder},
                    user_utterance=text,
                    reason="The user asked to watch a folder.",
                ),
            )

        if (
            "search conversation" in lowered
            or "search history" in lowered
            or "what did we discuss" in lowered
            or "past conversation" in lowered
            or "konversation suchen" in lowered
        ):
            query = text.split("about", 1)[-1].strip() if "about" in lowered else text
            return self._parsed(
                "query",
                "search_conversation_history",
                "Search past conversation history.",
                0.85,
                ToolRequest(
                    tool_name="search_conversation_history",
                    arguments={"query": query},
                    user_utterance=text,
                    reason="The user asked to search conversation history.",
                ),
            )

        if (
            ("timeline" in lowered and ("topic" in lowered or "show" in lowered or "build" in lowered))
            or "zeige zeitverlauf" in lowered or "themen zeitverlauf" in lowered or "zeitverlauf zu" in lowered
        ):
            topic = text.split("about", 1)[-1].strip() if "about" in lowered else text
            return self._parsed(
                "query",
                "build_topic_timeline",
                "Build a topic timeline from conversation history.",
                0.82,
                ToolRequest(
                    tool_name="build_topic_timeline",
                    arguments={"topic": topic},
                    user_utterance=text,
                    reason="The user asked to build a topic timeline.",
                ),
            )

        if lowered.startswith("ingest folder") or lowered.startswith("bulk ingest") or "ingest all files" in lowered:
            folder = text.split("folder", 1)[-1].strip(" :.") if "folder" in lowered else ""
            return self._parsed(
                "action",
                "bulk_ingest",
                "Bulk ingest a folder of documents.",
                0.85,
                ToolRequest(
                    tool_name="bulk_ingest",
                    arguments={"folder_path": folder},
                    user_utterance=text,
                    reason="The user asked to bulk ingest a folder.",
                ),
            )

        if normalized.startswith("compare_documents "):
            payload = text[len("compare_documents ") :].strip()
            document_ids: list[str] = []
            query = "Compare the selected documents and summarize the key similarities and differences."
            if "::" in payload:
                ids_part, query_part = payload.split("::", 1)
                payload = ids_part.strip()
                query = query_part.strip() or query
            try:
                parsed_ids = json.loads(payload)
                if isinstance(parsed_ids, list):
                    document_ids = [str(item) for item in parsed_ids if str(item).strip()]
            except json.JSONDecodeError:
                document_ids = []
            return self._parsed(
                "query",
                "compare_documents",
                "Compare multiple selected documents.",
                0.92,
                ToolRequest(
                    tool_name="compare_documents",
                    arguments={"document_ids": document_ids, "query": query},
                    user_utterance=text,
                    reason="The user asked to compare selected documents.",
                ),
            )

        if "compare" in lowered and "document" in lowered:
            query = text.split("compare", 1)[-1].strip(" :.")
            return self._parsed(
                "query",
                "compare_documents",
                "Compare multiple documents.",
                0.8,
                ToolRequest(
                    tool_name="compare_documents",
                    arguments={"document_ids": [], "query": query},
                    user_utterance=text,
                    reason="The user asked to compare documents.",
                ),
            )

        if ("query" in lowered or "analyse" in lowered or "analyze" in lowered) and (
            "spreadsheet" in lowered or "csv" in lowered or "excel" in lowered
        ):
            query = text.split("spreadsheet", 1)[-1].strip(" :.") if "spreadsheet" in lowered else text
            return self._parsed(
                "query",
                "query_spreadsheet",
                "Query a local spreadsheet.",
                0.82,
                ToolRequest(
                    tool_name="query_spreadsheet",
                    arguments={"file_path": "", "query": query},
                    user_utterance=text,
                    reason="The user asked to query a spreadsheet.",
                ),
            )

        if (
            "knowledge graph" in lowered or "entity graph" in lowered
            or "wissensgraph" in lowered or "entitäten suchen" in lowered or "wissenssuche" in lowered
        ):
            if "build" in lowered or "create" in lowered or "generate" in lowered:
                return self._parsed(
                    "action",
                    "build_knowledge_graph",
                    "Build the knowledge graph from documents.",
                    0.85,
                    ToolRequest(
                        tool_name="build_knowledge_graph",
                        arguments={},
                        user_utterance=text,
                        reason="The user asked to build the knowledge graph.",
                    ),
                )
            query = text.split("about", 1)[-1].strip() if "about" in lowered else text
            return self._parsed(
                "query",
                "query_knowledge_graph",
                "Search the knowledge graph.",
                0.84,
                ToolRequest(
                    tool_name="query_knowledge_graph",
                    arguments={"query": query},
                    user_utterance=text,
                    reason="The user asked to search the knowledge graph.",
                ),
            )

        if any(token in normalized for token in [
            "current context", "foreground window", "active window", "current app", "clipboard",
            "aktueller kontext", "aktives fenster", "was mache ich gerade",
        ]):
            return self._parsed(
                "query",
                "read_current_context",
                "Read the current local context.",
                0.83,
                ToolRequest(
                    tool_name="read_current_context",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked to read the current local context.",
                ),
            )

        if self._is_mailbox_sync_request(normalized):
            urgent_only = "urgent" in normalized or "today" in normalized
            return self._parsed(
                "action",
                "sync_mailbox",
                "Synchronize the mailbox.",
                0.84,
                ToolRequest(
                    tool_name="sync_mailbox",
                    arguments={"limit": 8, "urgent_only": urgent_only, "today_only": "today" in normalized},
                    user_utterance=text,
                    reason="The user asked to synchronize mailbox state.",
                ),
            )

        if self._is_mailbox_read_request(normalized):
            return self._parsed(
                "query",
                "read_mailbox_summary",
                "Read recent email.",
                0.82,
                ToolRequest(
                    tool_name="read_mailbox_summary",
                    arguments={
                        "limit": 5,
                        "urgent_only": "urgent" in normalized,
                        "today_only": "today" in normalized,
                    },
                    user_utterance=text,
                    reason="The user asked to read recent email.",
                ),
            )

        send_email = re.match(r"send email to (?P<to>[^ ]+) about (?P<subject>.+)", lowered)
        if send_email:
            return self._parsed(
                "action",
                "compose_email",
                "Send an email after confirmation.",
                0.78,
                ToolRequest(
                    tool_name="compose_email",
                    arguments={
                        "to": [send_email.group("to")],
                        "subject": send_email.group("subject").strip().title(),
                        "body": text,
                    },
                    user_utterance=text,
                    reason="The user asked to send an email.",
                ),
            )

        if any(token in lowered for token in ["create reminder from email", "email reminder"]):
            return self._parsed(
                "action",
                "create_email_reminder",
                "Create a reminder from the latest indexed email.",
                0.76,
                ToolRequest(
                    tool_name="create_email_reminder",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked to derive a reminder from email.",
                ),
            )

        if lowered.startswith("schedule meeting"):
            title = text.split("meeting", 1)[-1].strip(" :.") or "Meeting"
            return self._parsed(
                "action",
                "schedule_meeting_and_invite",
                "Schedule a meeting and send an invite.",
                0.74,
                ToolRequest(
                    tool_name="schedule_meeting_and_invite",
                    arguments={"title": title},
                    user_utterance=text,
                    reason="The user asked to schedule a meeting invite workflow.",
                ),
            )

        if lowered.startswith("notify phone") or lowered.startswith("send ntfy"):
            message = text.split(" ", 2)[-1].strip()
            return self._parsed(
                "action",
                "send_ntfy_notification",
                "Send a mobile notification.",
                0.75,
                ToolRequest(
                    tool_name="send_ntfy_notification",
                    arguments={"title": "KERN", "message": message},
                    user_utterance=text,
                    reason="The user asked to send an ntfy notification.",
                ),
            )

        if self._is_start_meeting_request(normalized):
            title = self._extract_meeting_title(text) or "Meeting"
            return self._parsed(
                "action",
                "start_meeting_recording",
                "Start local meeting recording.",
                0.88,
                ToolRequest(
                    tool_name="start_meeting_recording",
                    arguments={"title": title},
                    user_utterance=text,
                    reason="The user asked to start meeting recording.",
                ),
            )

        if self._is_stop_meeting_request(normalized):
            return self._parsed(
                "action",
                "stop_meeting_recording",
                "Stop local meeting recording.",
                0.88,
                ToolRequest(
                    tool_name="stop_meeting_recording",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked to stop meeting recording.",
                ),
            )

        angebot_customer = self._extract_angebot_customer(text)
        if angebot_customer is not None:
            return self._parsed(
                "action",
                "create_angebot",
                "Create a German offer draft.",
                0.82,
                ToolRequest(
                    tool_name="create_angebot",
                    arguments={"customer_name": angebot_customer},
                    user_utterance=text,
                    reason="The user asked to create an Angebot draft.",
                ),
            )

        rechnung_customer = self._extract_rechnung_customer(text)
        if rechnung_customer is not None:
            return self._parsed(
                "action",
                "create_rechnung",
                "Create a German invoice draft.",
                0.82,
                ToolRequest(
                    tool_name="create_rechnung",
                    arguments={"customer_name": rechnung_customer},
                    user_utterance=text,
                    reason="The user asked to create a Rechnung draft.",
                ),
            )

        if (
            lowered.startswith("draft behorde letter") or lowered.startswith("draft behörde letter")
            or "behördenbrief erstellen" in lowered or "brief an behörde" in lowered
        ):
            subject = text.split("letter", 1)[-1].strip(" :.") or "Anliegen"
            return self._parsed(
                "action",
                "draft_behoerde_letter",
                "Create a formal Behörde draft.",
                0.8,
                ToolRequest(
                    tool_name="draft_behoerde_letter",
                    arguments={"subject": subject, "body_points": [subject]},
                    user_utterance=text,
                    reason="The user asked for a Behörde correspondence draft.",
                ),
            )

        if any(token in lowered for token in ["dsgvo reminder", "create dsgvo reminders"]):
            return self._parsed(
                "action",
                "create_dsgvo_reminders",
                "Create DSGVO reminder rules.",
                0.8,
                ToolRequest(
                    tool_name="create_dsgvo_reminders",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked to create DSGVO reminders.",
                ),
            )

        if lowered.startswith("tax support"):
            question = text.split("support", 1)[-1].strip(" :.")
            return self._parsed(
                "query",
                "tax_support_query",
                "Prepare a tax support summary.",
                0.76,
                ToolRequest(
                    tool_name="tax_support_query",
                    arguments={"question": question},
                    user_utterance=text,
                    reason="The user asked for tax support guidance.",
                ),
            )

        if lowered.startswith("sync profile to"):
            target_path = text.split("to", 1)[-1].strip(" :.")
            return self._parsed(
                "action",
                "sync_profile_data",
                "Sync selected profile data.",
                0.74,
                ToolRequest(
                    tool_name="sync_profile_data",
                    arguments={"kind": "nas", "label": "Manual sync", "path_or_url": target_path},
                    user_utterance=text,
                    reason="The user asked to sync profile data.",
                ),
            )

        if lowered.startswith("call me "):
            preferred_title = text[8:].strip()
            return self._parsed(
                "action",
                "set_title",
                "Update preferred title.",
                0.97,
                ToolRequest(
                    tool_name="set_preference",
                    arguments={"key": "preferred_title", "value": preferred_title},
                    user_utterance=text,
                    reason="The user explicitly asked to change their title.",
                ),
            )

        if any(token in lowered for token in ["mute kern", "be quiet", "stop talking"]):
            return self._parsed(
                "action",
                "mute_kern",
                "Mute Kern voice output.",
                0.95,
                ToolRequest(
                    tool_name="set_preference",
                    arguments={"key": "muted", "value": "true"},
                    user_utterance=text,
                    reason="The user explicitly asked Kern to mute itself.",
                ),
            )

        if any(token in lowered for token in ["unmute kern", "you can speak", "start talking again"]):
            return self._parsed(
                "action",
                "unmute_kern",
                "Restore Kern voice output.",
                0.95,
                ToolRequest(
                    tool_name="set_preference",
                    arguments={"key": "muted", "value": "false"},
                    user_utterance=text,
                    reason="The user explicitly asked Kern to restore voice output.",
                ),
            )

        backup_arguments = self._extract_backup_request(text)
        if backup_arguments is not None:
            return self._parsed(
                "action",
                "create_backup",
                "Create an encrypted backup.",
                0.8,
                ToolRequest(
                    tool_name="create_backup",
                    arguments=backup_arguments,
                    user_utterance=text,
                    reason="The user asked to create an encrypted backup.",
                ),
            )

        if self._is_list_backups_request(normalized):
            return self._parsed(
                "query",
                "list_backups",
                "List available backups.",
                0.8,
                ToolRequest(
                    tool_name="list_backups",
                    arguments={"newest_only": "newest" in normalized or "latest" in normalized},
                    user_utterance=text,
                    reason="The user asked to list backups.",
                ),
            )

        if self._is_audit_read_request(normalized):
            return self._parsed(
                "query",
                "read_audit_events",
                "Read recent audit events.",
                0.78,
                ToolRequest(
                    tool_name="read_audit_events",
                    arguments={"query": self._extract_after_phrase(text, ["related to", "for", "about"]), "limit": 8},
                    user_utterance=text,
                    reason="The user asked to inspect audit events.",
                ),
            )

        if self._is_runtime_snapshot_request(normalized):
            return self._parsed(
                "query",
                "read_runtime_snapshot",
                "Read the runtime snapshot.",
                0.8,
                ToolRequest(
                    tool_name="read_runtime_snapshot",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked to inspect the current runtime snapshot.",
                ),
            )

        if self._is_profile_security_request(normalized):
            return self._parsed(
                "query",
                "read_profile_security",
                "Read profile and security state.",
                0.8,
                ToolRequest(
                    tool_name="read_profile_security",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked to inspect active profile or security state.",
                ),
            )

        remember = self._parse_memory_request(text)
        if remember is not None:
            arguments = dict(remember)
            return self._parsed(
                "action",
                "remember_fact",
                "Store a durable fact.",
                0.84,
                ToolRequest(
                    tool_name="remember_fact",
                    arguments=arguments,
                    user_utterance=text,
                    reason="The user asked the assistant to remember a fact.",
                ),
            )

        if any(token in lowered for token in ["what did i ask you to remember", "what do you remember", "recall what you know"]):
            if "about" in lowered:
                query = lowered.replace("what do you remember about", "").replace("what did i ask you to remember about", "").strip(" ?.!")
            else:
                query = ""
            return self._parsed(
                "query",
                "recall_memory",
                "Recall stored memory.",
                0.82,
                ToolRequest(
                    tool_name="recall_memory",
                    arguments={"query": query},
                    user_utterance=text,
                    reason="The user asked for remembered information.",
                ),
            )

        if any(token in lowered for token in ["dismiss that reminder", "dismiss the last one", "dismiss the one you just mentioned"]):
            reminder_id = self._resolve_contextual_reminder(dialogue_context)
            return self._contextual_reminder_intent(text, reminder_id, "dismiss_reminder", "Tell me which reminder you want dismissed.")

        if any(token in lowered for token in ["snooze that reminder", "snooze the last one", "snooze the one you just mentioned"]):
            reminder_id = self._resolve_contextual_reminder(dialogue_context)
            if reminder_id is None:
                return ParsedIntent(
                    intent_type="action",
                    intent_name="snooze_reminder",
                    response_hint="Tell me which reminder you want snoozed.",
                    confidence=0.82,
                    missing_slots=["reminder_id"],
                    follow_up_expected=True,
                )
            return self._parsed(
                "action",
                "snooze_reminder",
                "Snooze a reminder.",
                0.82,
                ToolRequest(
                    tool_name="snooze_reminder",
                    arguments={"reminder_id": reminder_id, "minutes": 10},
                    user_utterance=text,
                    reason="The user referred to the most recent reminder.",
                ),
            )

        if "good morning" in lowered or "guten morgen" in lowered:
            return self._parsed(
                "query",
                "morning_brief",
                "Generate a morning brief.",
                0.99,
                ToolRequest(
                    tool_name="generate_morning_brief",
                    arguments={},
                    user_utterance=text,
                    reason="The user requested a morning briefing.",
                ),
            )

        if any(token in lowered for token in ["status report", "system status", "what is my status", "what's my status"]):
            return self._parsed(
                "query",
                "status",
                "Read local status.",
                0.93,
                ToolRequest(
                    tool_name="read_status",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked for a local runtime status summary.",
                ),
            )

        if any(token in lowered for token in ["cpu", "battery", "memory pressure", "system health"]):
            return self._parsed(
                "query",
                "system_status",
                "Read system status.",
                0.73,
                ToolRequest(
                    tool_name="system_status",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked for host machine status.",
                ),
            )

        if any(token in lowered for token in ["what do i have today", "what's on today", "whats on today"]):
            return self._parsed(
                "query",
                "day_overview",
                "Generate a day overview.",
                0.92,
                ToolRequest(
                    tool_name="generate_morning_brief",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked for today's agenda and workload.",
                ),
            )

        if self._is_music_status_question(lowered):
            return self._parsed(
                "query",
                "status",
                "Read local status.",
                0.78,
                ToolRequest(
                    tool_name="read_status",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked about configured music, not playback.",
                ),
            )

        if self._is_calendar_query(lowered):
            return self._parsed(
                "query",
                "calendar",
                "Read today's calendar.",
                0.9,
                ToolRequest(
                    tool_name="get_today_calendar",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked about today's agenda.",
                ),
            )

        if "task" in lowered and ("what" in lowered or "pending" in lowered or "have" in lowered):
            return self._parsed(
                "query",
                "tasks",
                "Read pending tasks.",
                0.9,
                ToolRequest(
                    tool_name="get_pending_tasks",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked about pending tasks.",
                ),
            )

        if "remind me" in lowered or "set a timer" in lowered or "erstelle erinnerung" in lowered or "erinnere mich" in lowered or "neue erinnerung" in lowered or "erstelle eine erinnerung" in lowered:
            parsed_due = self._parse_reminder_phrase(text)
            if parsed_due is None:
                return ParsedIntent(
                    intent_type="action",
                    intent_name="create_reminder",
                    response_hint="Clarify when the reminder should happen.",
                    confidence=0.58,
                    missing_slots=["due_at"],
                    follow_up_expected=True,
                )
            title, due_at, kind, due_at_argument = parsed_due
            return self._parsed(
                "action",
                "create_reminder",
                "Create a local reminder.",
                0.88,
                ToolRequest(
                    tool_name="create_reminder",
                    arguments={"title": title, "due_at": due_at_argument, "kind": kind},
                    user_utterance=text,
                    reason="The user asked for a local reminder or timer.",
                ),
            )

        if lowered.startswith("snooze reminder"):
            reminder_id, minutes = self._parse_reminder_control(lowered, default_minutes=10)
            if reminder_id <= 0:
                return ParsedIntent(
                    intent_type="action",
                    intent_name="snooze_reminder",
                    response_hint="Snooze a reminder.",
                    confidence=0.86,
                    missing_slots=["reminder_id"],
                    follow_up_expected=True,
                )
            return self._parsed(
                "action",
                "snooze_reminder",
                "Snooze a reminder.",
                0.86,
                ToolRequest(
                    tool_name="snooze_reminder",
                    arguments={"reminder_id": reminder_id, "minutes": minutes},
                    user_utterance=text,
                    reason="The user asked to snooze a local reminder.",
                ),
            )

        if lowered.startswith("dismiss reminder"):
            reminder_id, _ = self._parse_reminder_control(lowered, default_minutes=0)
            return self._contextual_reminder_intent(text, reminder_id if reminder_id > 0 else None, "dismiss_reminder", "Dismiss a reminder.")

        if any(token in lowered for token in ["pause music", "pause spotify", "hold the music"]):
            return self._media_intent(text, "pause", "Pause media playback.")

        if any(token in lowered for token in ["resume spotify", "resume music", "continue spotify", "continue music", "bring the music back"]):
            return self._media_intent(text, "resume", "Resume media playback.")

        if any(token in lowered for token in ["next song", "skip song", "next track", "skip track"]):
            return self._media_intent(text, "next", "Skip to the next track.")

        if lowered.startswith("play ") or lowered.startswith("put on ") or lowered.startswith("start ") and ("music" in lowered or "spotify" in lowered):
            query = text.replace("play", "", 1).strip() if lowered.startswith("play ") else text.strip()
            return self._parsed(
                "action",
                "media",
                "Play media.",
                0.87,
                ToolRequest(
                    tool_name="play_spotify",
                    arguments={"query": query or "morning jazz", "mode": "search_and_play"},
                    user_utterance=text,
                    reason="The user asked for media playback.",
                ),
            )

        if any(token in lowered for token in ["focus mode", "start focus", "begin focus", "deep work mode"]):
            return self._parsed(
                "action",
                "focus_mode",
                "Start focus mode.",
                0.81,
                ToolRequest(
                    tool_name="focus_mode",
                    arguments={"minutes": 50, "title": "Focus block"},
                    user_utterance=text,
                    reason="The user asked to start focus mode.",
                ),
            )

        if any(token in lowered for token in ["search the web for", "look up", "google "]):
            query = text.lower().replace("search the web for", "").replace("look up", "").replace("google", "", 1).strip(" ?.!")
            if query:
                return self._parsed(
                    "action",
                    "browser_search",
                    "Search the web.",
                    0.76,
                    ToolRequest(
                        tool_name="browser_search",
                        arguments={"query": query},
                        user_utterance=text,
                        reason="The user asked to search the web.",
                    ),
                )

        if any(token in lowered for token in ["search files", "find file", "find files"]):
            query = text.lower().replace("search files for", "").replace("search files", "").replace("find file", "").replace("find files", "").strip(" :.?!")
            return self._parsed(
                "query",
                "search_files",
                "Search the workspace.",
                0.72,
                ToolRequest(
                    tool_name="search_files",
                    arguments={"query": query},
                    user_utterance=text,
                    reason="The user asked to search local files.",
                ),
            )

        if lowered.startswith("read file") or lowered.startswith("inspect file"):
            path = text.split("file", 1)[-1].strip(" :.?!")
            return self._parsed(
                "query",
                "read_file",
                "Inspect a file.",
                0.8,
                ToolRequest(
                    tool_name="read_file_excerpt",
                    arguments={"path": path},
                    user_utterance=text,
                    reason="The user asked to inspect a local file.",
                ),
            )

        if lowered.startswith("open "):
            target = text[5:].strip()
            if self._looks_like_website(target):
                return self._parsed(
                    "action",
                    "open_website",
                    "Open a website.",
                    0.9,
                    ToolRequest(
                        tool_name="open_website",
                        arguments={"url": target},
                        user_utterance=text,
                        reason="The user explicitly provided a website-like target.",
                    ),
                )
            if target.lower() in KNOWN_APPS or " " in target:
                return self._parsed(
                    "action",
                    "open_app",
                    "Open a desktop app.",
                    0.9,
                    ToolRequest(
                        tool_name="open_app",
                        arguments={"app": target},
                        user_utterance=text,
                        reason="The user asked to open a known local application.",
                    ),
                )
            return ParsedIntent(
                intent_type="action",
                intent_name="open_target",
                response_hint="Tell me whether you want the app or the website.",
                confidence=0.52,
                missing_slots=["target_type"],
                follow_up_expected=True,
            )

        if "http" in lowered or ".com" in lowered or "website" in lowered:
            url = text.split("open", 1)[-1].replace("website", "").strip()
            return self._parsed(
                "action",
                "open_website",
                "Open a website.",
                0.84,
                ToolRequest(
                    tool_name="open_website",
                    arguments={"url": url},
                    user_utterance=text,
                    reason="The user asked to open a website.",
                ),
            )

        if "list notes" in lowered or "show notes" in lowered:
            return self._parsed(
                "query",
                "list_notes",
                "Read recent notes.",
                0.82,
                ToolRequest(
                    tool_name="list_notes",
                    arguments={},
                    user_utterance=text,
                    reason="The user asked for recent notes.",
                ),
            )

        if any(token in lowered for token in ["morning routine", "focus routine", "shutdown routine"]):
            routine = "morning" if "morning" in lowered else "focus" if "focus" in lowered else "shutdown"
            return self._parsed(
                "action",
                "run_routine",
                "Run a local routine.",
                0.89,
                ToolRequest(
                    tool_name="run_routine",
                    arguments={"name": routine},
                    user_utterance=text,
                    reason="The user asked to run a named local routine.",
                ),
            )

        note_content = self._extract_note_creation_content(text)
        if note_content is not None:
            return self._parsed(
                "action",
                "create_note",
                "Create a local note.",
                0.85,
                ToolRequest(
                    tool_name="create_note",
                    arguments={"content": note_content},
                    user_utterance=text,
                    reason="The user asked to create a note.",
                ),
            )

        if any(token in lowered for token in ["complete the first one", "mark that task done", "complete that task"]):
            task_titles = self._resolve_contextual_tasks(dialogue_context)
            title = task_titles[0] if task_titles else ""
            if not title:
                return ParsedIntent(
                    intent_type="action",
                    intent_name="complete_task",
                    response_hint="Tell me which task you want to complete.",
                    confidence=0.76,
                    missing_slots=["title"],
                    follow_up_expected=True,
                )
            return self._parsed(
                "action",
                "complete_task",
                "Complete a task.",
                0.76,
                ToolRequest(
                    tool_name="complete_task",
                    arguments={"title": title},
                    user_utterance=text,
                    reason="The user referred to the most recently listed task.",
                ),
            )

        if lowered.startswith("complete task") or lowered.startswith("finish task"):
            title = text.split("task", 1)[-1].strip(": ").strip()
            if not title:
                return ParsedIntent(
                    intent_type="action",
                    intent_name="complete_task",
                    response_hint="Complete a task.",
                    confidence=0.8,
                    missing_slots=["title"],
                    follow_up_expected=True,
                )
            return self._parsed(
                "action",
                "complete_task",
                "Complete a task.",
                0.8,
                ToolRequest(
                    tool_name="complete_task",
                    arguments={"title": title},
                    user_utterance=text,
                    reason="The user asked to complete a task.",
                ),
            )

        if any(token in lowered for token in ["hallo", "guten tag", "guten abend"]):
            return ParsedIntent(
                intent_type="query",
                intent_name="greeting",
                response_hint="Greet the user warmly.",
                confidence=0.9,
                missing_slots=[],
            )

        if "create a task" in lowered or "add a task" in lowered:
            title = text.split("task", 1)[-1].strip(": ").strip()
            return ParsedIntent(
                intent_type="action",
                intent_name="create_task",
                response_hint="Create a task after confirmation.",
                confidence=0.86,
                missing_slots=["title"] if not title else [],
                follow_up_expected=not title,
                tool_request=ToolRequest(
                    tool_name="create_task",
                    arguments={"title": title},
                    user_utterance=text,
                    reason="The user asked to create a task.",
                ) if title else None,
            )

        return ParsedIntent(
            intent_type="chat",
            intent_name="chat",
            response_hint="Reply conversationally.",
            confidence=0.55,
            missing_slots=[],
        )

    def _parsed(
        self,
        intent_type: IntentType,
        intent_name: str,
        response_hint: str,
        confidence: float,
        tool_request: ToolRequest,
    ) -> ParsedIntent:
        return ParsedIntent(
            intent_type=intent_type,
            intent_name=intent_name,
            response_hint=response_hint,
            confidence=confidence,
            missing_slots=[],
            tool_request=tool_request,
        )

    def _contextual_reminder_intent(
        self,
        text: str,
        reminder_id: int | None,
        tool_name: str,
        hint: str,
    ) -> ParsedIntent:
        if reminder_id is None:
            return ParsedIntent(
                intent_type="action",
                intent_name=tool_name,
                response_hint=hint,
                confidence=0.82,
                missing_slots=["reminder_id"],
                follow_up_expected=True,
            )
        return self._parsed(
            "action",
            tool_name,
            hint,
            0.82,
            ToolRequest(
                tool_name=tool_name,
                arguments={"reminder_id": reminder_id},
                user_utterance=text,
                reason="The user referred to the most recent reminder.",
            ),
        )

    def _media_intent(self, text: str, mode: str, hint: str) -> ParsedIntent:
        return self._parsed(
            "action",
            f"media_{mode}",
            hint,
            0.91,
            ToolRequest(
                tool_name="play_spotify",
                arguments={"query": "", "mode": mode},
                user_utterance=text,
                reason="The user issued a media playback command.",
            ),
        )

    def _parse_reminder_phrase(self, text: str) -> tuple[str, datetime, str, str] | None:
        lowered = text.lower().strip()
        kind = "timer" if "timer" in lowered else "reminder"
        explicit_due_raw = self._extract_iso_datetime(text)
        if explicit_due_raw is not None:
            parsed_due = self._parse_iso_datetime(explicit_due_raw)
            if parsed_due is not None:
                title = self._extract_reminder_title(text) or "follow up"
                return title, parsed_due, kind, explicit_due_raw
        if " in " in lowered and "minute" in lowered:
            _, suffix = lowered.split(" in ", 1)
            digits = "".join(character for character in suffix if character.isdigit())
            if digits:
                minutes = max(1, int(digits))
                cleaned = text
                for marker in ["remind me to", "remind me", "set a timer for", "set timer for"]:
                    cleaned = cleaned.replace(marker, "")
                    cleaned = cleaned.replace(marker.title(), "")
                title = cleaned.split(" in ", 1)[0].strip(" ,.") or "follow up"
                due_at = datetime.now() + timedelta(minutes=minutes)
                return title, due_at, kind, due_at.isoformat()
        if "tomorrow" in lowered or "morgen" in lowered:
            cleaned = lowered
            for marker in [
                "remind me to", "remind me", "set a timer for", "set timer for",
                "erstelle eine erinnerung für", "erstelle erinnerung für",
                "erinnere mich an", "erinnere mich", "neue erinnerung für",
                "erstelle eine erinnerung", "erstelle erinnerung", "neue erinnerung",
            ]:
                cleaned = cleaned.replace(marker, "")
            cleaned = cleaned.replace("tomorrow", "").replace("morgen", "").strip(" ,.")
            due_at = datetime.now() + timedelta(days=1)
            return cleaned or "follow up tomorrow", due_at, kind, due_at.isoformat()
        return None

    def _extract_iso_datetime(self, text: str) -> str | None:
        match = re.search(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})\b", text)
        if not match:
            return None
        return match.group(0)

    def _parse_iso_datetime(self, value: str) -> datetime | None:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    def _extract_reminder_title(self, text: str) -> str | None:
        patterns = [
            r"(?:remind me to|remind me about|remind me)\s+(?P<title>[^;,.]+)",
            r"(?:erinnere mich an|erinnere mich)\s+(?P<title>[^;,.]+)",
            r"(?:erstelle eine erinnerung für|erstelle erinnerung für|neue erinnerung für)\s+(?P<title>[^;,.]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group("title").strip(" .,:;?!")
        return None

    def _parse_reminder_control(self, lowered: str, default_minutes: int) -> tuple[int, int]:
        numbers = [int(chunk) for chunk in lowered.split() if chunk.isdigit()]
        if not numbers:
            return 0, default_minutes
        reminder_id = numbers[0]
        minutes = numbers[1] if len(numbers) > 1 else default_minutes
        return reminder_id, minutes

    def _parse_memory_request(self, text: str) -> dict[str, object] | None:
        lowered = text.lower().strip()
        if not lowered.startswith("remember"):
            return None
        payload = text[8:].strip(" :.?!")
        payload = payload.removeprefix("that ").removeprefix("That ")
        lowered_payload = payload.lower()
        if " and i prefer " in lowered_payload:
            before, after = re.split(r"\band i prefer\b", payload, maxsplit=1, flags=re.IGNORECASE)
            facts: list[dict[str, str]] = []
            left = before.strip(" ,.")
            right = after.strip(" ,.")
            if " is " in left:
                key, value = left.split(" is ", 1)
                facts.append({"key": key.replace("my ", "", 1).strip(), "value": value.strip()})
            elif left:
                facts.append({"key": "memory", "value": left})
            if right:
                facts.append({"key": "response style", "value": right})
            if facts:
                return {"facts": facts}
        if " is " in payload:
            key, value = payload.split(" is ", 1)
            return {"key": key.replace("my ", "", 1).strip(), "value": value.strip()}
        if payload.lower().startswith("i prefer "):
            return {"key": "preference", "value": payload[9:].strip()}
        if payload:
            return {"key": "memory", "value": payload}
        return None

    def _normalize_text(self, text: str) -> str:
        lowered = text.lower()
        lowered = re.sub(r"[^\w\s]", " ", lowered)
        return re.sub(r"\s+", " ", lowered).strip()

    def _extract_after_phrase(self, text: str, markers: list[str]) -> str:
        lowered = text.lower()
        for marker in markers:
            if marker in lowered:
                index = lowered.index(marker) + len(marker)
                return text[index:].strip(" :.?!")
        return ""

    def _extract_eval_style_document_search_query(self, text: str) -> str | None:
        stripped = text.strip()
        patterns = [
            r"letzte fassung zu (?P<query>.+?) lieg\w*",
            r"englische version von (?P<query>.+?)(?:;|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, stripped, flags=re.IGNORECASE)
            if match:
                return match.group("query").strip(" .;:?!")
        return None

    def _extract_compare_document_names(self, text: str) -> tuple[str, str] | None:
        stripped = text.strip()
        patterns = [
            r"zwischen (?P<left>[A-Za-z0-9_\\-]+) und (?P<right>[A-Za-z0-9_\\-]+)",
            r"compare (?P<left>[A-Za-z0-9_\\-]+) and (?P<right>[A-Za-z0-9_\\-]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, stripped, flags=re.IGNORECASE)
            if match:
                return (
                    match.group("left").strip(" .;:?!"),
                    match.group("right").strip(" .;:?!"),
                )
        return None

    def _extract_document_summary_name(self, text: str) -> str | None:
        stripped = text.strip()
        patterns = [
            r"zusammenfassung von (?P<document>[A-Za-z0-9_\\-]+)",
            r"summary of (?P<document>[A-Za-z0-9_\\-]+)",
            r"fasse(?: bitte)? (?P<document>[A-Za-z0-9_\\-]+) zusammen",
        ]
        for pattern in patterns:
            match = re.search(pattern, stripped, flags=re.IGNORECASE)
            if match:
                return match.group("document").strip(" .;:?!")
        return None

    def _extract_note_creation_content(self, text: str) -> str | None:
        stripped = text.strip()
        lowered = stripped.lower()
        explicit_prefixes = (
            "note ",
            "note:",
            "create a note",
            "save a note",
            "take a note",
            "make a note of this",
            "write this down",
            "jot this down",
            "capture this",
            "capture this for me",
        )
        for prefix in explicit_prefixes:
            if lowered.startswith(prefix):
                content = stripped[len(prefix):].strip(" :.?!")
                return content or stripped
        return None

    def _extract_document_search_query(self, text: str) -> str | None:
        normalized = self._normalize_text(text)
        patterns = [
            r"^(?:can you |could you |please |show |tell me |give me )*search (?:my )?documents(?: for)? (?P<query>.+)$",
            r"^(?:can you |could you |please |show |tell me |give me )*look in (?:my )?documents(?: for)? (?P<query>.+)$",
            r"^(?:can you |could you |please |show |tell me |give me )*find in (?:my )?docs(?: for)? (?P<query>.+)$",
            r"^search documents(?: for)? (?P<query>.+)$",
            # German document search patterns
            r"^suche in meinen dokumenten(?: nach)? (?P<query>.+)$",
            r"^dokumente durchsuchen(?: nach)? (?P<query>.+)$",
            r"^finde in dokumenten(?: nach)? (?P<query>.+)$",
            r"^zeig mir dokumente(?: zu| über| für)? (?P<query>.+)$",
            r"^zeig mir meine dokumente(?: zu| über| für)? (?P<query>.+)$",
        ]
        for pattern in patterns:
            match = re.match(pattern, normalized)
            if match:
                return match.group("query").strip()
        # German triggers without explicit query (return full text as query)
        german_doc_triggers = [
            "suche in meinen dokumenten",
            "dokumente durchsuchen",
            "finde in dokumenten",
            "zeig mir dokumente",
            "zeig mir meine dokumente",
        ]
        for trigger in german_doc_triggers:
            if normalized.startswith(trigger):
                remainder = normalized[len(trigger):].strip()
                return remainder if remainder else text.strip()
        return None

    def _is_mailbox_read_request(self, normalized: str) -> bool:
        return any(
            phrase in normalized
            for phrase in [
                "read my email",
                "check my email",
                "read recent email",
                "show recent email",
                "show my recent mailbox messages",
                "show my mailbox messages",
                "show my recent email messages",
                "e mail lesen",
                "mails prüfen",
                "e mails lesen",
                "posteingang",
                "postfach prüfen",
            ]
        ) or ("mailbox" in normalized and any(token in normalized for token in ["show", "read", "recent"]))

    def _is_mailbox_sync_request(self, normalized: str) -> bool:
        return any(
            phrase in normalized
            for phrase in [
                "sync email",
                "sync my mailbox",
                "sync mailbox",
                "check if anything urgent arrived",
                "e mail synchronisieren",
            ]
        )

    def _is_start_meeting_request(self, normalized: str) -> bool:
        return any(
            phrase in normalized
            for phrase in ["start meeting recording", "start a meeting recording", "record this meeting"]
        )

    def _is_stop_meeting_request(self, normalized: str) -> bool:
        return any(phrase in normalized for phrase in ["stop meeting recording", "end meeting recording"])

    def _extract_meeting_title(self, text: str) -> str | None:
        match = re.search(r"(?:recording|meeting)\s+(?:for|called|named)\s+(.+)$", text, flags=re.IGNORECASE)
        return match.group(1).strip(" :.?!") if match else None

    def _extract_angebot_customer(self, text: str) -> str | None:
        normalized = self._normalize_text(text)
        if not any(phrase in normalized for phrase in [
            "create angebot", "create a draft angebot", "make an angebot",
            "erstelle angebot", "angebot erstellen", "neues angebot",
        ]):
            return None
        parts = re.split(r"angebot", text, maxsplit=1, flags=re.IGNORECASE)
        customer_name = parts[-1].strip(" :.?!") if len(parts) > 1 else ""
        return customer_name or "Kunde"

    def _extract_rechnung_customer(self, text: str) -> str | None:
        normalized = self._normalize_text(text)
        if not any(
            phrase in normalized for phrase in [
                "create rechnung", "create a rechnung", "make an invoice", "create an invoice",
                "erstelle rechnung", "rechnung erstellen", "neue rechnung",
            ]
        ):
            return None
        if re.search(r"(?:invoice|rechnung)", text, flags=re.IGNORECASE):
            customer_name = re.split(r"(?:invoice|rechnung)", text, maxsplit=1, flags=re.IGNORECASE)[-1].strip(" :.?!")
        else:
            customer_name = ""
        return customer_name or "Kunde"

    def _extract_backup_request(self, text: str) -> dict[str, object] | None:
        normalized = self._normalize_text(text)
        if "encrypted backup" not in normalized:
            return None
        label_match = re.search(r"\bnamed\s+(.+?)(?:\s+with password\s+.+)?$", text, flags=re.IGNORECASE)
        password_match = re.search(r"\bwith password\s+(.+)$", text, flags=re.IGNORECASE)
        arguments: dict[str, object] = {
            "label": (label_match.group(1).strip(" :.?!") if label_match else "Manual backup"),
        }
        if password_match:
            arguments["password"] = password_match.group(1).strip(" :.?!")
        return arguments

    def _is_list_backups_request(self, normalized: str) -> bool:
        if "audit" in normalized:
            return False
        return any(phrase in normalized for phrase in ["list backups", "show backups", "available backups"]) or (
            "backups" in normalized and any(token in normalized for token in ["newest", "latest", "which"])
        )

    def _is_audit_read_request(self, normalized: str) -> bool:
        return "audit" in normalized and any(token in normalized for token in ["show", "read", "recent", "list"])

    def _is_runtime_snapshot_request(self, normalized: str) -> bool:
        return any(
            phrase in normalized
            for phrase in ["runtime snapshot", "system snapshot", "snapshot summary", "runtime summary"]
        )

    def _is_profile_security_request(self, normalized: str) -> bool:
        return any(
            phrase in normalized
            for phrase in [
                "active profile",
                "memory scope",
                "profile security",
                "security state",
                "current profile",
            ]
        )

    def _is_music_status_question(self, lowered: str) -> bool:
        return (
            ("music" in lowered or "playlist" in lowered)
            and any(token in lowered for token in ["what", "which", "set", "configured", "default"])
            and "play " not in lowered
        )

    def _is_calendar_query(self, lowered: str) -> bool:
        explicit_phrases = [
            "what meetings do i have",
            "what meeting do i have",
            "what's on my calendar",
            "whats on my calendar",
            "what is on my calendar",
            "calendar today",
        ]
        return "calendar" in lowered or any(phrase in lowered for phrase in explicit_phrases)

    def _looks_like_website(self, target: str) -> bool:
        lowered = target.lower()
        return lowered.startswith(("http://", "https://", "www.")) or "." in lowered or "website" in lowered

    def _resolve_contextual_reminder(self, dialogue_context: dict[str, str]) -> int | None:
        raw = dialogue_context.get("last_announced_reminder_id") or ""
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        listed = [int(chunk) for chunk in (dialogue_context.get("last_listed_reminder_ids") or "").split(",") if chunk.isdigit()]
        return listed[0] if listed else None

    def _resolve_contextual_tasks(self, dialogue_context: dict[str, str]) -> list[str]:
        raw = dialogue_context.get("last_listed_task_titles") or ""
        return [title for title in raw.split("||") if title.strip()]
