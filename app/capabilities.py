from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.tools.base import Tool
from app.types import CapabilityDescriptor, PlanStep, ToolRequest


@dataclass(slots=True)
class Capability:
    descriptor: CapabilityDescriptor
    tool: Tool


class CapabilityRegistry:
    def __init__(self, capabilities: dict[str, Capability], *, product_posture: str = "production") -> None:
        self._capabilities = capabilities
        self._product_posture = product_posture

    def _posture_gate(self, capability_name: str) -> tuple[bool, str | None]:
        if self._product_posture != "production":
            return True, None
        return True, None

    def get(self, name: str) -> Capability:
        return self._capabilities[name]

    def get_capability(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    def get_descriptor(self, name: str) -> CapabilityDescriptor | None:
        capability = self._capabilities.get(name)
        return capability.descriptor if capability else None

    def available_descriptors(self) -> list[CapabilityDescriptor]:
        descriptors: list[CapabilityDescriptor] = []
        for name, item in self._capabilities.items():
            descriptor = item.descriptor.model_copy()
            posture_available, posture_note = self._posture_gate(name)
            tool_available, dynamic_note = item.tool.availability()
            available = posture_available and tool_available
            descriptor.available = available
            note_parts = [part for part in (descriptor.notes, posture_note, dynamic_note) if part]
            if note_parts:
                descriptor.notes = " ".join(note_parts)
            else:
                descriptor.notes = None
            descriptors.append(descriptor)
        return descriptors

    def build_request(self, step: PlanStep, user_utterance: str, trigger_source: str = "manual_ui") -> ToolRequest:
        return ToolRequest(
            tool_name=step.capability_name,
            arguments=step.arguments,
            user_utterance=user_utterance,
            reason=step.reason,
            trigger_source=trigger_source,
        )

    def update_last_status(self, capability_name: str, status: str) -> None:
        if capability_name not in self._capabilities:
            return
        self._capabilities[capability_name].descriptor.last_status = status

    def is_available(self, capability_name: str) -> tuple[bool, str | None]:
        capability = self._capabilities.get(capability_name)
        if capability is None:
            return False, "Capability is not registered."
        posture_available, posture_note = self._posture_gate(capability_name)
        if not posture_available:
            return False, posture_note
        return capability.tool.availability()


def build_capability_registry(tools: dict[str, Tool]) -> CapabilityRegistry:
    descriptors = {
        "open_app": CapabilityDescriptor(
            name="open_app",
            title="Launch App",
            summary="Launch or focus a desktop application.",
            domain="core",
            risk_level="medium",
            confirmation_rule="on_risk",
            side_effectful=True,
            verification_support="heuristic",
        ),
        "open_website": CapabilityDescriptor(
            name="open_website",
            title="Open Website",
            summary="Open a website in the default browser.",
            domain="core",
            risk_level="medium",
            confirmation_rule="on_risk",
            side_effectful=True,
            verification_support="heuristic",
        ),
        "browser_search": CapabilityDescriptor(
            name="browser_search",
            title="Browser Search",
            summary="Search the web in the default browser.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="heuristic",
        ),
        "get_today_calendar": CapabilityDescriptor(
            name="get_today_calendar",
            title="Calendar Read",
            summary="Read today's local calendar.",
            domain="calendar",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "get_pending_tasks": CapabilityDescriptor(
            name="get_pending_tasks",
            title="Task Read",
            summary="Read active local tasks.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "create_note": CapabilityDescriptor(
            name="create_note",
            title="Save Note",
            summary="Persist a note in local memory.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "list_notes": CapabilityDescriptor(
            name="list_notes",
            title="Read Notes",
            summary="Read recent notes from local memory.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "create_task": CapabilityDescriptor(
            name="create_task",
            title="Create Task",
            summary="Add a local task and open loop.",
            domain="core",
            risk_level="medium",
            confirmation_rule="always",
            side_effectful=True,
            verification_support="database",
        ),
        "complete_task": CapabilityDescriptor(
            name="complete_task",
            title="Complete Task",
            summary="Resolve a local task and its open loop.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "set_preference": CapabilityDescriptor(
            name="set_preference",
            title="Update Preference",
            summary="Update a local runtime preference.",
            domain="security",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "read_status": CapabilityDescriptor(
            name="read_status",
            title="Runtime Status",
            summary="Read system and local assistant status.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "generate_morning_brief": CapabilityDescriptor(
            name="generate_morning_brief",
            title="Morning Brief",
            summary="Generate a local brief from reminders, tasks, and events.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "create_reminder": CapabilityDescriptor(
            name="create_reminder",
            title="Create Reminder",
            summary="Create a reminder or timer.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "set_timer": CapabilityDescriptor(
            name="set_timer",
            title="Set Timer",
            summary="Create a timer from local memory.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "snooze_reminder": CapabilityDescriptor(
            name="snooze_reminder",
            title="Snooze Reminder",
            summary="Delay a reminder in local memory.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "dismiss_reminder": CapabilityDescriptor(
            name="dismiss_reminder",
            title="Dismiss Reminder",
            summary="Dismiss a pending reminder.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "run_routine": CapabilityDescriptor(
            name="run_routine",
            title="Run Routine",
            summary="Run a named local routine.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "remember_fact": CapabilityDescriptor(
            name="remember_fact",
            title="Remember Fact",
            summary="Store a durable fact about the user or context.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "recall_memory": CapabilityDescriptor(
            name="recall_memory",
            title="Recall Memory",
            summary="Read remembered facts and commitments.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "focus_mode": CapabilityDescriptor(
            name="focus_mode",
            title="Focus Mode",
            summary="Start a focus block and reduce interruptions.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "search_files": CapabilityDescriptor(
            name="search_files",
            title="File Search",
            summary="Search local workspace files read-only.",
            domain="documents",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "read_file_excerpt": CapabilityDescriptor(
            name="read_file_excerpt",
            title="File Inspect",
            summary="Read a local file excerpt without modification.",
            domain="documents",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "system_status": CapabilityDescriptor(
            name="system_status",
            title="System Status",
            summary="Read CPU, memory, and battery signals when available.",
            domain="security",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="heuristic",
        ),
        "ingest_document": CapabilityDescriptor(
            name="ingest_document",
            title="Ingest Document",
            summary="Import a local document into the profile archive and retrieval index.",
            domain="documents",
            risk_level="medium",
            confirmation_rule="on_risk",
            side_effectful=True,
            verification_support="database",
        ),
        "search_documents": CapabilityDescriptor(
            name="search_documents",
            title="Search Documents",
            summary="Search ingested documents and archives.",
            domain="documents",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "import_conversation_archive": CapabilityDescriptor(
            name="import_conversation_archive",
            title="Import Archive",
            summary="Import archived ChatGPT or Claude conversation history.",
            domain="documents",
            risk_level="medium",
            confirmation_rule="always",
            side_effectful=True,
            verification_support="database",
        ),
        "set_memory_scope": CapabilityDescriptor(
            name="set_memory_scope",
            title="Memory Scope",
            summary="Update the active retrieval scope for profile memory and archives.",
            domain="documents",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "bulk_ingest": CapabilityDescriptor(
            name="bulk_ingest",
            title="Bulk Ingest",
            summary="Import an entire folder or a batch of files into the profile document archive.",
            domain="documents",
            risk_level="medium",
            confirmation_rule="on_risk",
            side_effectful=True,
            verification_support="database",
        ),
        "compare_documents": CapabilityDescriptor(
            name="compare_documents",
            title="Compare Documents",
            summary="Answer a question by comparing content across multiple indexed documents.",
            domain="documents",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "summarize_document": CapabilityDescriptor(
            name="summarize_document",
            title="Summarize Document",
            summary="Summarize a named local document from the indexed document store.",
            domain="documents",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "query_spreadsheet": CapabilityDescriptor(
            name="query_spreadsheet",
            title="Query Spreadsheet",
            summary="Ask a question about a local CSV or Excel spreadsheet.",
            domain="documents",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="heuristic",
        ),
        "create_angebot": CapabilityDescriptor(
            name="create_angebot",
            title="Create Angebot",
            summary="Generate a German offer draft from structured fields.",
            domain="german_business",
            risk_level="medium",
            confirmation_rule="always",
            side_effectful=True,
            verification_support="database",
        ),
        "create_rechnung": CapabilityDescriptor(
            name="create_rechnung",
            title="Create Rechnung",
            summary="Generate a German invoice draft from structured fields.",
            domain="german_business",
            risk_level="high",
            confirmation_rule="always",
            side_effectful=True,
            verification_support="database",
        ),
        "draft_behoerde_letter": CapabilityDescriptor(
            name="draft_behoerde_letter",
            title="BehÃ¶rde Draft",
            summary="Create a formal German administrative correspondence draft.",
            domain="german_business",
            risk_level="medium",
            confirmation_rule="always",
            side_effectful=True,
            verification_support="database",
        ),
        "create_dsgvo_reminders": CapabilityDescriptor(
            name="create_dsgvo_reminders",
            title="DSGVO Reminders",
            summary="Create compliance reminder rules as local reminders.",
            domain="german_business",
            risk_level="medium",
            confirmation_rule="always",
            side_effectful=True,
            verification_support="database",
        ),
        "tax_support_query": CapabilityDescriptor(
            name="tax_support_query",
            title="Tax Support",
            summary="Provide document-backed tax support guidance with disclaimers.",
            domain="german_business",
            risk_level="medium",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "sync_profile_data": CapabilityDescriptor(
            name="sync_profile_data",
            title="Sync Profile Data",
            summary="Sync selected profile data to NAS or a registered Nextcloud target.",
            domain="sync",
            risk_level="high",
            confirmation_rule="always",
            side_effectful=True,
            verification_support="database",
        ),
        "create_backup": CapabilityDescriptor(
            name="create_backup",
            title="Create Backup",
            summary="Create an encrypted profile backup.",
            domain="security",
            risk_level="high",
            confirmation_rule="always",
            side_effectful=True,
            verification_support="database",
        ),
        "restore_backup": CapabilityDescriptor(
            name="restore_backup",
            title="Restore Backup",
            summary="Restore an encrypted profile backup into a target directory.",
            domain="security",
            risk_level="high",
            confirmation_rule="always",
            side_effectful=True,
            verification_support="database",
        ),
        "list_backups": CapabilityDescriptor(
            name="list_backups",
            title="List Backups",
            summary="Read available encrypted backups for the active profile.",
            domain="security",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "create_schedule": CapabilityDescriptor(
            name="create_schedule",
            title="Create Schedule",
            summary="Create a cron-based scheduled task that runs automatically.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "list_schedules": CapabilityDescriptor(
            name="list_schedules",
            title="List Schedules",
            summary="List all configured scheduled tasks.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "manage_schedule": CapabilityDescriptor(
            name="manage_schedule",
            title="Manage Schedule",
            summary="Enable, disable, or delete a scheduled task.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
        "watch_folder": CapabilityDescriptor(
            name="watch_folder",
            title="Watch Folder",
            summary="Add a folder to the file watch list for automatic document ingestion.",
            domain="documents",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="heuristic",
        ),
        "search_conversation_history": CapabilityDescriptor(
            name="search_conversation_history",
            title="Search Conversation History",
            summary="Search past conversation turns by keyword and optional date range.",
            domain="memory",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "build_topic_timeline": CapabilityDescriptor(
            name="build_topic_timeline",
            title="Build Topic Timeline",
            summary="Build a chronological timeline of past conversations grouped by topic.",
            domain="memory",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "read_audit_events": CapabilityDescriptor(
            name="read_audit_events",
            title="Read Audit Events",
            summary="Read recent audit events for the active profile.",
            domain="security",
            risk_level="medium",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "export_audit_trail": CapabilityDescriptor(
            name="export_audit_trail",
            title="Export Audit Trail",
            summary="Export the audit trail for the active profile.",
            domain="security",
            risk_level="high",
            confirmation_rule="always",
            side_effectful=True,
            verification_support="database",
        ),
        "read_runtime_snapshot": CapabilityDescriptor(
            name="read_runtime_snapshot",
            title="Read Runtime Snapshot",
            summary="Read the current runtime snapshot summary.",
            domain="core",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "read_current_context": CapabilityDescriptor(
            name="read_current_context",
            title="Read Current Context",
            summary="Read the current local window, media, and clipboard context when available.",
            domain="core",
            risk_level="medium",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
        "read_profile_security": CapabilityDescriptor(
            name="read_profile_security",
            title="Read Profile Security",
            summary="Read active profile, memory scope, and encryption state.",
            domain="security",
            risk_level="medium",
            confirmation_rule="never",
            side_effectful=False,
            verification_support="database",
        ),
    }

    capabilities: dict[str, Capability] = {}
    for name, descriptor in descriptors.items():
        tool = tools.get(name)
        if tool is None:
            continue
        capabilities[name] = Capability(descriptor=descriptor, tool=tool)
    return CapabilityRegistry(capabilities, product_posture=settings.product_posture)
