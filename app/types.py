from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

AssistantState = Literal["idle", "attentive", "capturing", "processing", "responding", "muted", "error"]
AssistantMode = Literal["manual", "attentive", "focus", "proactive"]
IntentType = Literal["chat", "query", "action"]
PolicyVerdict = Literal["allow", "confirm", "deny"]
CapabilityStatus = Literal["observed", "attempted", "failed"]
ConfirmationRule = Literal["never", "always", "on_risk"]
VerificationSupport = Literal["none", "heuristic", "database"]
AssistantTrigger = Literal["manual_ui", "scheduler"]
VerificationSource = Literal["none", "tool", "process", "database", "filesystem"]
CapabilityDomain = Literal["core", "documents", "calendar", "security", "german_business", "sync", "memory"]
BackgroundJobStatus = Literal["queued", "running", "waiting_for_commit", "completed", "failed", "recoverable", "rolled_back", "cancelled"]
BackupTargetKind = Literal["local_folder", "usb", "external_drive", "nas"]
MemoryScope = Literal["off", "session", "profile", "profile_plus_archive"]
ProductPosture = Literal["production", "personal"]
ThreadVisibility = Literal["private", "shared", "system_audit"]
ResourceScope = Literal["private", "workspace", "organization", "system_audit"]
ReminderSuggestionStatus = Literal["suggested", "accepted", "rejected"]
ReviewState = Literal["pending", "accepted", "rejected"]
DocumentClassification = Literal["public", "internal", "confidential", "finance", "legal", "hr"]
WorkspaceRole = Literal["org_owner", "org_admin", "member", "auditor"]
UserStatus = Literal["pending", "active", "suspended", "deleted"]
ComplianceRecordStatus = Literal["requested", "approved", "blocked", "completed", "failed"]
DataClass = Literal["personal", "regulated_business", "operational", "security_audit"]
RetentionDecision = Literal["allow_delete", "retain", "blocked_by_legal_hold", "pseudonymize"]
ErasureStepStatus = Literal["pending", "completed", "blocked", "failed", "skipped"]
MemoryPromotionState = Literal["none", "candidate", "workspace_promoted", "personal_only", "rejected"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
WorkflowType = Literal[
    "correspondence_follow_up",
    "regulated_document_lifecycle",
    "review_approval_queue",
    "compliance_export_erasure",
    "scheduling_follow_through",
]
RecommendationActionType = Literal[
    "draft_reply",
    "follow_up_contact",
    "request_missing_input",
    "finalize_document",
    "review_candidate",
    "run_export",
    "confirm_erasure",
    "schedule_task",
    "defer",
    "escalate",
    "suggested_draft",
    "missing_context",
    "evidence_pack",
    "follow_up_candidate",
    "blocked_item",
    "ready_to_finalize",
]
RiskLevel = Literal["low", "medium", "high"]
ReasoningSource = Literal["system_decision", "retrieved_evidence", "llm_generated_wording"]
ReadinessStatus = Literal["ready_now", "blocked", "waiting_on_input", "needs_review"]
ClaimStatus = Literal["supported", "missing", "conflicted", "inferred"]
ReadinessNodeState = Literal["present", "missing", "blocked", "verified", "candidate"]
GenerationMode = Literal["draft", "summarize", "clarify", "explain_only", "answer", "cite"]
TaskIntentFamily = Literal[
    "prepared_work",
    "document_qa",
    "document_citation",
    "document_summary",
    "document_key_sections",
    "document_compare",
    "thread_qa",
    "person_context",
    "workspace_context",
    "cross_context_question",
    "clarification_needed",
    "general_chat_fallback",
]
DocumentOutputMode = Literal["answer", "citations", "summary", "key_sections", "compare"]
AnswerReadinessStatus = Literal["ready_now", "blocked", "waiting_on_input", "needs_review"]

class IntentCandidate(BaseModel):
    name: str
    intent_type: IntentType
    confidence: float = Field(ge=0.0, le=1.0)
    source: Literal["rule", "semantic", "classifier", "planner", "fallback"] = "fallback"
    reason: str
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolRequest(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    user_utterance: str
    reason: str
    trigger_source: AssistantTrigger = "manual_ui"


class CapabilityDescriptor(BaseModel):
    name: str
    title: str
    summary: str
    domain: CapabilityDomain = "core"
    risk_level: Literal["low", "medium", "high"] = "low"
    confirmation_rule: ConfirmationRule = "on_risk"
    side_effectful: bool = False
    available: bool = True
    verification_support: VerificationSupport = "heuristic"
    notes: str | None = None
    last_status: CapabilityStatus | None = None


class PlanStep(BaseModel):
    capability_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str
    title: str | None = None
    confirmation_required: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    source: Literal["rule", "semantic", "classifier", "planner", "fallback"] = "fallback"
    summary: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    candidates: list[IntentCandidate] = Field(default_factory=list)


class PolicyDecision(BaseModel):
    verdict: PolicyVerdict
    risk_level: Literal["low", "medium", "high"]
    message: str
    step_index: int | None = None
    policy_scope: str | None = None
    policy_reason: str | None = None


class ToolResult(BaseModel):
    success: bool = True
    display_text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    status: CapabilityStatus = "observed"
    evidence: list[str] = Field(default_factory=list)
    side_effects: list[str] = Field(default_factory=list)
    suggested_follow_up: str | None = None

    @model_validator(mode="after")
    def sync_success_with_status(self) -> "ToolResult":
        self.success = self.status != "failed"
        return self


class ExecutionReceipt(BaseModel):
    timestamp: datetime = Field(default_factory=_utcnow)
    capability_name: str
    status: CapabilityStatus
    message: str
    original_utterance: str = ""
    trigger_source: AssistantTrigger = "manual_ui"
    verification_source: VerificationSource = "none"
    evidence: list[str] = Field(default_factory=list)
    side_effects: list[str] = Field(default_factory=list)
    suggested_follow_up: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class AssistantTurn(BaseModel):
    trigger: AssistantTrigger
    transcript: str
    intent_type: IntentType
    response_text: str
    tool_calls: list[ToolRequest] = Field(default_factory=list)
    plan: ExecutionPlan | None = None
    candidates: list[IntentCandidate] = Field(default_factory=list)
    receipts: list[ExecutionReceipt] = Field(default_factory=list)
    reasoning_source: ReasoningSource | None = None
    recommendation_id: str | None = None
    workflow_type: WorkflowType | None = None


class ConversationTurn(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    role: Literal["user", "assistant", "system"]
    text: str
    timestamp: datetime = Field(default_factory=_utcnow)
    kind: Literal["message", "confirmation", "tool_status", "proactive"] = "message"
    status: Literal["complete", "pending", "failed"] = "complete"
    meta: dict[str, Any] = Field(default_factory=dict)


class CalendarEventSummary(BaseModel):
    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    importance: int = 0
    source: str = "local"


class TaskSummary(BaseModel):
    id: int | None = None
    title: str
    priority: int = 1
    due_at: datetime | None = None
    source: str = "local"


class ReminderSummary(BaseModel):
    id: int | None = None
    title: str
    due_at: datetime
    status: Literal["pending", "announced", "dismissed", "snoozed", "completed"] = "pending"
    kind: Literal["reminder", "timer", "event_soft_alert"] = "reminder"
    source: str = "local"


class MorningBrief(BaseModel):
    date: datetime
    events: list[Any] = Field(default_factory=list)
    tasks: list[Any] = Field(default_factory=list)
    reminders: list[Any] = Field(default_factory=list)
    focus_suggestion: str
    next_event: Any | None = None


class ContextFact(BaseModel):
    key: str
    value: str
    source: str = "user"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    memory_kind: str = "fact"
    entity_key: str | None = None
    status: str = "active"
    provenance: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime | None = None


class OpenLoop(BaseModel):
    id: int | None = None
    title: str
    details: str | None = None
    status: Literal["open", "resolved", "dismissed"] = "open"
    due_at: datetime | None = None
    source: str = "assistant"
    related_type: str | None = None
    related_id: int | None = None
    updated_at: datetime | None = None


class ActiveContextSummary(BaseModel):
    preferred_title: str
    facts: list[ContextFact] = Field(default_factory=list)
    open_loops: list[OpenLoop] = Field(default_factory=list)
    reminders: list[ReminderSummary] = Field(default_factory=list)
    tasks: list[TaskSummary] = Field(default_factory=list)
    events: list[CalendarEventSummary] = Field(default_factory=list)
    recent_dialogue: list[str] = Field(default_factory=list)
    summary_lines: list[str] = Field(default_factory=list)
    system_signals: dict[str, Any] = Field(default_factory=dict)
    current_context: "CurrentContextSnapshot | None" = None


class ProactivePrompt(BaseModel):
    reason: str
    message: str
    source: str = "local"
    generated_at: datetime = Field(default_factory=_utcnow)


class PendingConfirmation(BaseModel):
    step: PlanStep
    prompt: str


class PendingInteraction(BaseModel):
    kind: Literal["clarification", "confirmation"]
    prompt: str
    original_utterance: str
    trigger_source: AssistantTrigger
    missing_slots: list[str] = Field(default_factory=list)
    plan: ExecutionPlan | None = None

class ActionHistoryEntry(BaseModel):
    timestamp: datetime
    category: Literal["trigger", "speech", "policy", "tool", "system", "reminder", "plan", "memory", "route"]
    message: str


class PersonaReply(BaseModel):
    display_text: str
    priority: Literal["low", "normal", "high"] = "normal"


class ProfileSummary(BaseModel):
    workspace_id: str | None = None
    organization_id: str | None = None
    slug: str
    title: str
    profile_root: str
    db_path: str
    documents_root: str
    attachments_root: str
    archives_root: str
    meetings_root: str
    backups_root: str
    has_pin: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProfileSession(BaseModel):
    profile_slug: str = "default"
    unlocked: bool = True
    locked_reason: str | None = None
    last_unlocked_at: datetime | None = None


class OrganizationRecord(BaseModel):
    id: str
    slug: str
    name: str
    created_at: datetime
    updated_at: datetime


class UserRecord(BaseModel):
    id: str
    organization_id: str
    email: str
    display_name: str
    status: UserStatus = "pending"
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class WorkspaceMembershipRecord(BaseModel):
    id: str
    organization_id: str
    workspace_id: str
    workspace_slug: str
    user_id: str
    role: WorkspaceRole
    created_at: datetime
    updated_at: datetime


class ThreadRecord(BaseModel):
    id: str
    organization_id: str
    workspace_id: str
    workspace_slug: str
    owner_user_id: str
    title: str
    visibility: ThreadVisibility = "private"
    created_at: datetime
    updated_at: datetime


class MessageRecord(BaseModel):
    id: str
    thread_id: str
    organization_id: str
    workspace_id: str
    workspace_slug: str
    actor_user_id: str | None = None
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class WorkspaceAccessContext(BaseModel):
    organization_id: str
    workspace_id: str
    workspace_slug: str
    user_id: str
    roles: list[WorkspaceRole] = Field(default_factory=list)


class RetentionPolicyRecord(BaseModel):
    id: str
    organization_id: str
    data_class: str
    retention_days: int
    legal_hold_enabled: bool = False
    created_at: datetime
    updated_at: datetime


class LegalHoldRecord(BaseModel):
    id: str
    organization_id: str
    workspace_slug: str | None = None
    target_user_id: str | None = None
    reason: str
    active: bool = True
    created_at: datetime
    updated_at: datetime


class ErasureRequestRecord(BaseModel):
    id: str
    organization_id: str
    target_user_id: str
    requested_by_user_id: str | None = None
    workspace_slug: str | None = None
    status: ComplianceRecordStatus = "requested"
    reason: str = ""
    approved_by_user_id: str | None = None
    retention_decision: RetentionDecision | None = None
    legal_hold_decision: str | None = None
    steps: list["ErasureExecutionStep"] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class DataExportRecord(BaseModel):
    id: str
    organization_id: str
    workspace_slug: str | None = None
    target_user_id: str | None = None
    requested_by_user_id: str | None = None
    status: ComplianceRecordStatus = "requested"
    artifact_path: str | None = None
    approved_by_user_id: str | None = None
    manifest: "EvidenceManifest | None" = None
    artifact_refs: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ErasureExecutionStep(BaseModel):
    name: str
    status: ErasureStepStatus = "pending"
    detail: str = ""
    updated_at: datetime = Field(default_factory=_utcnow)


class EvidenceManifest(BaseModel):
    version: str = "v1"
    generated_at: datetime = Field(default_factory=_utcnow)
    generator_actor_id: str | None = None
    generator_service: str = "compliance_service"
    organization_id: str | None = None
    workspace_slug: str | None = None
    included_datasets: list[str] = Field(default_factory=list)
    excluded_datasets: list[dict[str, str]] = Field(default_factory=list)
    digests: list[dict[str, str]] = Field(default_factory=list)


class RegulatedDocumentVersion(BaseModel):
    id: str
    regulated_document_id: str
    version_number: int
    supersedes_version_id: str | None = None
    document_id: str | None = None
    business_document_id: str | None = None
    file_path: str | None = None
    content_digest: str
    version_chain_digest: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class RegulatedDocumentRecord(BaseModel):
    id: str
    profile_slug: str
    workspace_slug: str
    organization_id: str | None = None
    data_class: DataClass = "regulated_business"
    title: str
    document_id: str | None = None
    business_document_id: str | None = None
    current_version_id: str | None = None
    current_version_number: int = 0
    immutability_state: Literal["draft", "finalized", "superseded"] = "draft"
    retention_state: Literal["standard", "retention_locked", "legal_hold"] = "standard"
    finalized_at: datetime | None = None
    finalized_by_user_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class AuditEvent(BaseModel):
    created_at: datetime
    profile_slug: str | None = None
    category: str
    action: str
    status: Literal["info", "success", "warning", "failure"] = "info"
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str | None = None
    event_hash: str | None = None


class BackgroundJob(BaseModel):
    id: str
    profile_slug: str | None = None
    job_type: str
    status: BackgroundJobStatus = "queued"
    title: str
    detail: str = ""
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    checkpoint_stage: str | None = None
    recoverable: bool = False
    error_code: str | None = None
    error_message: str | None = None

class BackupTarget(BaseModel):
    kind: BackupTargetKind
    path: str
    label: str
    writable: bool = True


class DocumentRecord(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_id: str | None = None
    actor_user_id: str | None = None
    title: str
    source: str
    file_type: str
    file_path: str
    file_hash: str | None = None
    category: str | None = None
    classification: DocumentClassification = "internal"
    data_class: DataClass = "operational"
    retention_state: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    archived: bool = False
    created_at: datetime = Field(default_factory=_utcnow)
    imported_at: datetime = Field(default_factory=_utcnow)


class DocumentChunk(BaseModel):
    document_id: str
    chunk_index: int
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalHit(BaseModel):
    source_type: Literal["document", "archive", "memory"] = "document"
    source_id: str
    score: float = 0.0
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class SecretRef(BaseModel):
    id: str
    profile_slug: str
    name: str


class ConversationArchiveRecord(BaseModel):
    id: str
    profile_slug: str
    source: Literal["chatgpt", "claude", "other"] = "other"
    title: str
    file_path: str
    archived_at: datetime = Field(default_factory=_utcnow)
    imported_turns: int = 0


class CalendarActionPlan(BaseModel):
    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    invite_recipients: list[str] = Field(default_factory=list)
    event_id: int | None = None


class MeetingRecord(BaseModel):
    id: str
    profile_slug: str
    title: str
    audio_path: str
    transcript_path: str | None = None
    status: str = "recorded"
    created_at: datetime = Field(default_factory=_utcnow)


class TranscriptArtifact(BaseModel):
    meeting_id: str
    transcript_path: str
    artifact_type: str = "transcript"
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class TranscriptSummary(BaseModel):
    meeting_id: str
    summary: str
    decisions: list[str] = Field(default_factory=list)
    reminders: list[str] = Field(default_factory=list)


class ExtractedActionItem(BaseModel):
    id: int | None = None
    meeting_id: str
    title: str
    details: str | None = None
    due_hint: str | None = None
    review_state: ReviewState = "pending"
    accepted: bool = False
    related_task_id: int | None = None
    related_reminder_id: int | None = None


class GermanBusinessDocument(BaseModel):
    id: str
    profile_slug: str
    kind: Literal["angebot", "rechnung", "behoerde", "tax_support", "compliance_note"]
    title: str
    status: Literal["draft", "support", "reminder"] = "draft"
    file_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeedbackSignal(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    user_id: str | None = None
    signal_type: Literal[
        "use_again",
        "reject_pattern",
        "promote_workspace",
        "personal_only",
        "packet_accepted",
        "packet_ignored",
        "scaffold_heavily_edited",
        "evidence_used",
        "missing_input_answered",
        "personal_preference",
    ]
    source_type: str
    source_id: str
    memory_item_id: str | None = None
    approved_for_training: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class TrainingExampleRecord(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    user_id: str | None = None
    source_type: str
    source_id: str
    input_text: str
    output_text: str
    status: Literal["candidate", "approved", "rejected", "exported"] = "candidate"
    approved_for_training: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class WorkflowEvent(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    workflow_id: str
    event_type: str
    detail: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class WorkflowRecord(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    workflow_type: WorkflowType
    subject_refs: dict[str, Any] = Field(default_factory=dict)
    status: str = "open"
    last_event: str = ""
    next_expected_step: str = ""
    blocking_reasons: list[str] = Field(default_factory=list)
    due_at: datetime | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ObligationRecord(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    workflow_id: str | None = None
    title: str
    status: Literal["open", "blocked", "completed"] = "open"
    reason: str = ""
    priority: int = Field(default=1, ge=1, le=5)
    due_at: datetime | None = None
    blocking_reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class DecisionRecord(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    decision_kind: str
    decision_value: str
    source_type: str
    source_id: str
    reasoning_source: ReasoningSource = "system_decision"
    rationale: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class RankingFeatureVector(BaseModel):
    workflow_match: float = 0.0
    urgency_score: float = 0.0
    recency_score: float = 0.0
    approval_score: float = 0.0
    entity_match_score: float = 0.0
    document_class_score: float = 0.0
    actor_match_score: float = 0.0
    policy_safe: bool = True


class RankingExplanation(BaseModel):
    score: float = 0.0
    features: RankingFeatureVector = Field(default_factory=RankingFeatureVector)
    reasons: list[str] = Field(default_factory=list)


class WorkflowDomainEvent(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    workflow_id: str
    workflow_type: WorkflowType
    event_type: str
    detail: str = ""
    fingerprint: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class ClaimEvidenceRef(BaseModel):
    ref_id: str
    source_type: str
    title: str = ""
    excerpt: str = ""
    freshness_seconds: float | None = None


class ClaimRecord(BaseModel):
    id: str
    label: str
    status: ClaimStatus = "inferred"
    required: bool = True
    evidence_refs: list[ClaimEvidenceRef] = Field(default_factory=list)
    rationale: str = ""
    derived_from: list[str] = Field(default_factory=list)


class NegativeEvidenceRecord(BaseModel):
    id: str
    expected_signal: str
    searched_sources: list[str] = Field(default_factory=list)
    detail: str = ""


class ReadinessNode(BaseModel):
    id: str
    label: str
    node_type: str = "evidence"
    state: ReadinessNodeState = "candidate"
    reason: str = ""
    claim_id: str | None = None
    required: bool = True
    source_refs: list[str] = Field(default_factory=list)


class ReadinessEdge(BaseModel):
    from_node_id: str
    to_node_id: str
    relationship: str = "requires"


class GenerationContract(BaseModel):
    mode: GenerationMode = "explain_only"
    allow_draft: bool = False
    allow_answer: bool = False
    allow_cite: bool = False
    allow_summarize: bool = True
    allow_clarify: bool = True
    allow_explain_only: bool = True
    note: str = ""


class ShadowRankingRecord(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    workflow_id: str | None = None
    recommendation_id: str | None = None
    policy_name: str
    score: float = 0.0
    features: dict[str, Any] = Field(default_factory=dict)
    outcome: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class EvidenceBundle(BaseModel):
    id: str
    workflow_id: str | None = None
    recommendation_id: str | None = None
    summary: str = ""
    why_selected: list[str] = Field(default_factory=list)
    scope: Literal["user_private", "workspace", "organization_shared"] = "workspace"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    policy_safe: bool = True
    source_refs: list[str] = Field(default_factory=list)
    action_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    items: list[dict[str, Any]] = Field(default_factory=list)
    claims: list[ClaimRecord] = Field(default_factory=list)
    negative_evidence: list[NegativeEvidenceRecord] = Field(default_factory=list)
    coverage_score: float = Field(default=0.0, ge=0.0, le=1.0)
    freshness: float | None = None
    event_refs: list[str] = Field(default_factory=list)
    ranking_explanation: RankingExplanation = Field(default_factory=RankingExplanation)


class MissingInputRecord(BaseModel):
    id: str
    label: str
    reason: str = ""
    required_for: str = ""
    severity: Literal["info", "warning", "blocking"] = "warning"


class SuggestedDraftRecord(BaseModel):
    id: str
    title: str
    subject: str = ""
    body: str
    tone: str = "clear"
    mode: Literal["deterministic_scaffold", "llm_rewrite"] = "deterministic_scaffold"
    based_on_refs: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class FreeformIntentRecord(BaseModel):
    id: str
    transcript: str
    task_family: TaskIntentFamily = "general_chat_fallback"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    selected_document_ids: list[str] = Field(default_factory=list)
    linked_entity_refs: list[str] = Field(default_factory=list)
    ambiguity_count: int = 0
    clarification_required: bool = False
    clarification_prompt: str = ""
    clarification_reason: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class TaskIntentRecord(FreeformIntentRecord):
    pass


class DocumentQueryPlan(BaseModel):
    id: str
    task_type: TaskIntentFamily
    selected_document_ids: list[str] = Field(default_factory=list)
    query_terms: list[str] = Field(default_factory=list)
    required_output_mode: DocumentOutputMode = "answer"
    evidence_requirements: list[str] = Field(default_factory=list)
    citation_required: bool = False
    clarification_required: bool = False
    notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)


class DocumentCitationRecord(BaseModel):
    id: str
    document_id: str
    title: str
    chunk_id: str
    excerpt: str
    page_number: int | None = None
    chunk_index: int | None = None


class FocusHint(BaseModel):
    id: str
    title: str
    summary: str
    recommendation_id: str | None = None
    workflow_id: str | None = None
    score: float = 0.0
    readiness_status: ReadinessStatus = "ready_now"
    why_now: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = "low"


class PreparationPacket(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    recommendation_id: str | None = None
    workflow_id: str | None = None
    workflow_type: WorkflowType | None = None
    preparation_type: RecommendationActionType
    title: str
    summary: str
    readiness_status: ReadinessStatus = "ready_now"
    why_ready: list[str] = Field(default_factory=list)
    why_blocked: list[str] = Field(default_factory=list)
    missing_inputs: list[MissingInputRecord] = Field(default_factory=list)
    readiness_nodes: list[ReadinessNode] = Field(default_factory=list)
    readiness_edges: list[ReadinessEdge] = Field(default_factory=list)
    evidence_pack: EvidenceBundle
    preparation_scope: Literal["user_private", "workspace", "organization_shared"] = "workspace"
    worker_review_required: bool = True
    generation_contract: GenerationContract = Field(default_factory=GenerationContract)
    event_refs: list[str] = Field(default_factory=list)
    resolution_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguity_count: int = 0
    linked_entity_refs: list[str] = Field(default_factory=list)
    support_breadth: float = Field(default=0.0, ge=0.0, le=1.0)
    clarification_reason: str = ""
    suggested_draft: SuggestedDraftRecord | None = None
    focus_hint: FocusHint | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class RecommendationRecord(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    workflow_id: str | None = None
    workflow_type: WorkflowType | None = None
    recommendation_type: RecommendationActionType
    title: str
    reason: str
    required_inputs: list[str] = Field(default_factory=list)
    blocking_conditions: list[str] = Field(default_factory=list)
    evidence_bundle: EvidenceBundle
    risk_level: RiskLevel = "low"
    reversible: bool = True
    recommended_now: bool = True
    reasoning_source: ReasoningSource = "system_decision"
    ranking_explanation: RankingExplanation = Field(default_factory=RankingExplanation)
    readiness_status: ReadinessStatus = "ready_now"
    why_ready: list[str] = Field(default_factory=list)
    why_blocked: list[str] = Field(default_factory=list)
    missing_inputs: list[MissingInputRecord] = Field(default_factory=list)
    readiness_nodes: list[ReadinessNode] = Field(default_factory=list)
    readiness_edges: list[ReadinessEdge] = Field(default_factory=list)
    preparation_scope: Literal["user_private", "workspace", "organization_shared"] = "workspace"
    worker_review_required: bool = True
    generation_contract: GenerationContract = Field(default_factory=GenerationContract)
    event_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)


class DocumentAnswerPacket(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    task_intent: TaskIntentRecord
    query_plan: DocumentQueryPlan
    query_text: str
    title: str
    answer_intent: str
    selected_document_ids: list[str] = Field(default_factory=list)
    selected_documents: list[dict[str, Any]] = Field(default_factory=list)
    readiness_status: AnswerReadinessStatus = "ready_now"
    why_ready: list[str] = Field(default_factory=list)
    why_blocked: list[str] = Field(default_factory=list)
    blocker_details: list[str] = Field(default_factory=list)
    missing_inputs: list[MissingInputRecord] = Field(default_factory=list)
    readiness_nodes: list[ReadinessNode] = Field(default_factory=list)
    readiness_edges: list[ReadinessEdge] = Field(default_factory=list)
    evidence_pack: EvidenceBundle
    citations: list[DocumentCitationRecord] = Field(default_factory=list)
    generation_contract: GenerationContract = Field(default_factory=GenerationContract)
    worker_review_required: bool = True
    event_refs: list[str] = Field(default_factory=list)
    resolution_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguity_count: int = 0
    linked_entity_refs: list[str] = Field(default_factory=list)
    support_breadth: float = Field(default=0.0, ge=0.0, le=1.0)
    clarification_reason: str = ""
    deterministic_answer: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class ContextLinkRecord(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    link_type: Literal["same_contact", "same_thread", "same_customer", "recent_interaction"]
    source_ref: str
    target_ref: str
    strength: float = Field(default=0.0, ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class InteractionOutcomeRecord(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    packet_type: Literal["document_answer", "preparation", "thread_context", "person_context", "clarification"]
    packet_id: str
    outcome_type: Literal["packet_used", "packet_ignored", "clarification_answered", "llm_rewrite_used", "same_thread_packet_accepted", "same_contact_packet_accepted"]
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class ThreadContextPacket(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    task_intent: FreeformIntentRecord
    query_text: str
    title: str
    summary: str
    thread_refs: list[str] = Field(default_factory=list)
    linked_entity_refs: list[str] = Field(default_factory=list)
    resolution_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguity_count: int = 0
    support_breadth: float = Field(default=0.0, ge=0.0, le=1.0)
    clarification_reason: str = ""
    readiness_status: AnswerReadinessStatus = "ready_now"
    why_ready: list[str] = Field(default_factory=list)
    why_blocked: list[str] = Field(default_factory=list)
    blocker_details: list[str] = Field(default_factory=list)
    missing_inputs: list[MissingInputRecord] = Field(default_factory=list)
    readiness_nodes: list[ReadinessNode] = Field(default_factory=list)
    readiness_edges: list[ReadinessEdge] = Field(default_factory=list)
    evidence_pack: EvidenceBundle
    generation_contract: GenerationContract = Field(default_factory=GenerationContract)
    worker_review_required: bool = True
    deterministic_answer: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class PersonContextPacket(BaseModel):
    id: str
    profile_slug: str
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    task_intent: FreeformIntentRecord
    query_text: str
    title: str
    summary: str
    person_ref: str | None = None
    linked_entity_refs: list[str] = Field(default_factory=list)
    resolution_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguity_count: int = 0
    support_breadth: float = Field(default=0.0, ge=0.0, le=1.0)
    clarification_reason: str = ""
    readiness_status: AnswerReadinessStatus = "ready_now"
    why_ready: list[str] = Field(default_factory=list)
    why_blocked: list[str] = Field(default_factory=list)
    blocker_details: list[str] = Field(default_factory=list)
    missing_inputs: list[MissingInputRecord] = Field(default_factory=list)
    readiness_nodes: list[ReadinessNode] = Field(default_factory=list)
    readiness_edges: list[ReadinessEdge] = Field(default_factory=list)
    evidence_pack: EvidenceBundle
    generation_contract: GenerationContract = Field(default_factory=GenerationContract)
    worker_review_required: bool = True
    deterministic_answer: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class WorldStateSnapshot(BaseModel):
    organization_id: str | None = None
    workspace_slug: str | None = None
    actor_user_id: str | None = None
    generated_at: datetime = Field(default_factory=_utcnow)
    workflow_count: int = 0
    obligation_count: int = 0
    risk_count: int = 0
    approval_queue_count: int = 0
    workflows: list[WorkflowRecord] = Field(default_factory=list)
    obligations: list[ObligationRecord] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class InvoiceDraft(BaseModel):
    customer_name: str
    invoice_number: str
    issue_date: datetime
    line_items: list[dict[str, Any]] = Field(default_factory=list)


class OfferDraft(BaseModel):
    customer_name: str
    offer_number: str
    valid_until: datetime | None = None
    line_items: list[dict[str, Any]] = Field(default_factory=list)


class BehoerdeDraft(BaseModel):
    subject: str
    category: str = "general"
    tone_preset: str = "formal"
    required_fields: dict[str, str] = Field(default_factory=dict)
    body_points: list[str] = Field(default_factory=list)


class ComplianceRun(BaseModel):
    id: str
    rule_id: str
    title: str
    status: Literal["scheduled", "completed"] = "scheduled"
    due_at: datetime


class TaxSupportResult(BaseModel):
    question: str
    answer: str
    source_hits: list[RetrievalHit] = Field(default_factory=list)
    disclaimer: str = "Kein Steuer- oder Rechtsrat."
    output_label: Literal["support"] = "support"


class ComplianceReminderRule(BaseModel):
    id: str
    title: str
    cadence: str
    details: str = ""


class TaxSupportQuery(BaseModel):
    question: str
    context_sources: list[str] = Field(default_factory=list)


class SyncTarget(BaseModel):
    id: str | None = None
    kind: Literal["nextcloud", "nas"]
    label: str
    path_or_url: str
    enabled: bool = True
    status: Literal["ready", "degraded", "upload_only"] = "ready"
    last_sync_at: datetime | None = None
    last_failure: str | None = None
    selected_data_classes: list[str] = Field(default_factory=list)


class SyncJob(BaseModel):
    id: str
    target_kind: Literal["nextcloud", "nas"]
    status: BackgroundJobStatus = "queued"
    detail: str = ""


class RecoveryCheckpoint(BaseModel):
    job_id: str
    stage: str
    updated_at: datetime = Field(default_factory=_utcnow)


class RAGSource(BaseModel):
    title: str
    text: str = ""
    file_path: str | None = None
    chunk_index: int = 0
    score: float = 0.0
    source_type: str = "document"


class RAGResult(BaseModel):
    answer: str
    sources: list[RAGSource] = Field(default_factory=list)
    retrieval_hits_count: int = 0
    reranked_count: int = 0
    context_tokens_used: int = 0
    had_sufficient_context: bool = True


class ModelFallbackState(BaseModel):
    enabled: bool = False
    last_error: str | None = None
    fallback_mode: Literal["deterministic", "none"] = "deterministic"
    active_mode: str = "off"


class NetworkStatusSnapshot(BaseModel):
    outbound_connections: int = 0
    last_check: str = ""
    status: Literal["isolated", "network_detected", "checking", "unmonitored"] = "checking"
    endpoints: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)


class BackupValidationResult(BaseModel):
    valid: bool = False
    profile_slug: str | None = None
    created_at: datetime | None = None
    entry_count: int = 0
    self_contained: bool = False
    errors: list[str] = Field(default_factory=list)
    entries: list[str] = Field(default_factory=list)


class RestorePlan(BaseModel):
    backup_path: str
    requested_root: str
    staged_root: str
    final_root: str
    profile_slug: str


class DomainStatus(BaseModel):
    ready: bool = False
    reason: str = ""
    lock_sensitive: bool = True
    degraded: bool = False


class RetrievalStatusSnapshot(BaseModel):
    ready: bool = False
    backend: Literal["lexical", "local_embedding", "local_tfidf"] = "lexical"
    index_health: Literal["disabled", "healthy", "stale", "building", "missing"] = "disabled"
    reason: str = ""
    last_index_build_at: datetime | None = None


class SecurityStatusSnapshot(BaseModel):
    db_encryption_enabled: bool = False
    db_encryption_mode: str = "off"
    db_key_available: bool = False
    key_derivation_version: str = "v1"
    key_version: int = 0
    last_key_rotation: datetime | None = None
    artifact_encryption_enabled: bool = False
    artifact_encryption_status: str = "not enabled"
    artifact_encryption_migration_state: str = "not enabled"
    profile_key_loaded: bool = False
    artifact_key_loaded: bool = False


class EmailReminderSuggestion(BaseModel):
    message_id: str
    title: str
    due_at: datetime
    status: ReminderSuggestionStatus = "suggested"
    rationale: str = ""


class MeetingReviewSnapshot(BaseModel):
    meeting: MeetingRecord
    transcript: TranscriptArtifact | None = None
    summary: TranscriptArtifact | None = None
    action_items: list[ExtractedActionItem] = Field(default_factory=list)


class LoopMetricsSnapshot(BaseModel):
    monitor_cycles: int = 0
    snapshots_sent: int = 0
    snapshots_skipped: int = 0
    last_broadcast_ms: float = 0.0
    last_context_refresh_ms: float = 0.0
    last_capability_refresh_ms: float = 0.0
    last_receipt_refresh_ms: float = 0.0


class RenderKeysSnapshot(BaseModel):
    conversation: str = "0"
    context: str = "0"
    capabilities: str = "0"
    receipts: str = "0"
    runtime: str = "0"


class ForegroundWindowSnapshot(BaseModel):
    title: str = ""
    process_id: int | None = None
    process_name: str = ""
    captured_at: datetime | None = None


class ClipboardSnapshot(BaseModel):
    has_text: bool = False
    excerpt: str = ""
    char_count: int = 0
    captured_at: datetime | None = None


class CurrentContextSnapshot(BaseModel):
    window: ForegroundWindowSnapshot | None = None
    clipboard: ClipboardSnapshot | None = None
    sources: dict[str, bool] = Field(default_factory=dict)


class PromptCacheSnapshot(BaseModel):
    enabled: bool = False
    entries: int = 0
    hits: int = 0
    misses: int = 0


class ModelRouteSnapshot(BaseModel):
    selected_mode: Literal["default", "fast", "deep", "unavailable"] = "default"
    requested_model: str | None = None
    strategy: str = "single"
    reason: str = ""
    used_rag: bool = False
    fallback_used: bool = False
    cache_hit: bool = False


class ModelInfoSnapshot(BaseModel):
    app_version: str = "0.0.0"
    app_name: str = "KERN"
    cognition_name: str = "Heuristic local planner"
    cognition_type: str = "hybrid"
    cognition_backend: str = "hybrid"
    hybrid_details: list[str] = Field(default_factory=list)
    cognition_model_path: str | None = None
    embed_model: str | None = None
    cloud_available: bool = False
    llm_model: str | None = None
    model_mode: str = "off"
    fast_model_path: str | None = None
    deep_model_path: str | None = None
    routing_strategy: str = "single"
    preferred_runtime: str = "Local GGUF"
    preferred_runtime_detail: str = "Recommended for pilots and internal installs."


class OnboardingSnapshot(BaseModel):
    active: bool = True
    completed: bool = False
    current_step: Literal["storage", "model", "workflow", "sample", "done"] = "storage"
    storage_confirmed: bool = False
    model_choice: str = ""
    starter_workflow: str = ""
    selected_path: Literal["real_documents", "sample_workspace", ""] = ""
    sample_workspace_active: bool = False
    sample_workspace_seeded: bool = False
    title: str = "Get to your first grounded draft"
    body: str = "Confirm local storage, confirm the recommended model path, then start with one document-grounded reply."
    primary_action: str = "Continue"
    secondary_action: str = ""
    local_data_note: str = ""
    model_note: str = ""
    workflow_note: str = ""
    storage_path: str | None = None
    model_path: str | None = None


class TrustSummarySnapshot(BaseModel):
    local_posture: str = ""
    storage_posture: str = ""
    model_posture: str = ""
    recovery_posture: str = ""
    readiness_posture: str = ""


class ReadinessCheckSnapshot(BaseModel):
    id: str
    label: str
    severity: Literal["info", "warning", "error"] = "info"
    status: Literal["pass", "warning", "fail"] = "pass"
    why_it_matters: str = ""
    operator_action: str = ""
    details: str = ""


class ReadinessSummarySnapshot(BaseModel):
    status: Literal["ready", "warning", "not_ready"] = "warning"
    headline: str = ""
    warnings: int = 0
    errors: int = 0


class FailureStateSnapshot(BaseModel):
    id: str
    error_code: str
    title: str
    message: str
    data_safe: bool = True
    blocked_scope: str = ""
    retry_available: bool = False
    retry_action: str | None = None
    next_action: str = ""
    technical_detail: str = ""
    source: str = "runtime"


class UpdateStateSnapshot(BaseModel):
    policy: str = "Manual stable-channel updates only."
    channel: str = "stable"
    app_version: str = "0.0.0"
    last_attempt_at: str = ""
    last_success_at: str = ""
    last_backup_at: str = ""
    last_restore_attempt_at: str = ""
    last_status: Literal["idle", "succeeded", "failed", "rollback_performed"] = "idle"
    last_error: str = ""
    message: str = ""


class RuntimeSnapshot(BaseModel):
    product_name: str = "KERN"
    product_posture: ProductPosture = "production"
    assistant_state: AssistantState = "idle"
    assistant_mode: AssistantMode = "manual"
    transcript: str = ""
    response_text: str = ""
    last_action: str = "Waiting for you."
    morning_brief: MorningBrief | None = None
    pending_confirmation: PendingConfirmation | None = None
    runtime_muted: bool = False
    local_mode_enabled: bool = True
    cloud_available: bool = False
    llm_available: bool = False
    cognition_backend: str = "rules"
    startup_checks: dict[str, str] = Field(default_factory=dict)
    action_in_progress: bool = False
    conversation_turns: list[ConversationTurn] = Field(default_factory=list)
    reminders_due: list[ReminderSummary] = Field(default_factory=list)
    action_history: list[ActionHistoryEntry] = Field(default_factory=list)
    active_plan: ExecutionPlan | None = None
    active_context_summary: ActiveContextSummary | None = None
    current_context: CurrentContextSnapshot | None = None
    capability_status: list[CapabilityDescriptor] = Field(default_factory=list)
    last_receipts: list[ExecutionReceipt] = Field(default_factory=list)
    verification_state: str = "No verified actions yet."
    proactive_prompt: ProactivePrompt | None = None
    model_info: ModelInfoSnapshot = Field(default_factory=ModelInfoSnapshot)
    active_profile: ProfileSummary | None = None
    profile_session: ProfileSession = Field(default_factory=ProfileSession)
    recent_audit_events: list[AuditEvent] = Field(default_factory=list)
    background_jobs: list[BackgroundJob] = Field(default_factory=list)
    backup_targets: list[BackupTarget] = Field(default_factory=list)
    audit_enabled: bool = True
    memory_scope: MemoryScope = "profile"
    policy_mode: str = "personal"
    update_channel: str = "stable"
    policy_summary: dict[str, Any] = Field(default_factory=dict)
    retention_policies: dict[str, Any] = Field(default_factory=dict)
    retention_status: dict[str, Any] = Field(default_factory=dict)
    onboarding: OnboardingSnapshot = Field(default_factory=OnboardingSnapshot)
    trust_summary: TrustSummarySnapshot = Field(default_factory=TrustSummarySnapshot)
    readiness_summary: ReadinessSummarySnapshot = Field(default_factory=ReadinessSummarySnapshot)
    readiness_checks: list[ReadinessCheckSnapshot] = Field(default_factory=list)
    update_state: UpdateStateSnapshot = Field(default_factory=UpdateStateSnapshot)
    active_failures: list[FailureStateSnapshot] = Field(default_factory=list)
    last_recoverable_failure: FailureStateSnapshot | None = None
    support_bundle_last_export_at: str = ""
    support_bundle_path: str = ""
    last_update_backup_at: str = ""
    last_restore_attempt_at: str = ""
    storage_roots: dict[str, str] = Field(default_factory=dict)
    domain_notes: dict[str, str] = Field(default_factory=dict)
    domain_statuses: dict[str, DomainStatus] = Field(default_factory=dict)
    background_job_counts: dict[str, int] = Field(default_factory=dict)
    domain_totals: dict[str, int] = Field(default_factory=dict)
    recent_documents: list[DocumentRecord] = Field(default_factory=list)
    recent_meetings: list[MeetingRecord] = Field(default_factory=list)
    recent_transcripts: list[TranscriptArtifact] = Field(default_factory=list)
    business_documents: list[GermanBusinessDocument] = Field(default_factory=list)
    sync_targets: list[SyncTarget] = Field(default_factory=list)
    recovery_checkpoints: list[RecoveryCheckpoint] = Field(default_factory=list)
    available_backups: list[str] = Field(default_factory=list)
    model_fallback_state: ModelFallbackState = Field(default_factory=ModelFallbackState)
    last_model_route: ModelRouteSnapshot = Field(default_factory=ModelRouteSnapshot)
    prompt_cache: PromptCacheSnapshot = Field(default_factory=PromptCacheSnapshot)
    retrieval_status: RetrievalStatusSnapshot = Field(default_factory=RetrievalStatusSnapshot)
    security_status: SecurityStatusSnapshot = Field(default_factory=SecurityStatusSnapshot)
    audit_chain_ok: bool = True
    audit_chain_reason: str | None = None
    last_audit_verification_at: datetime | None = None
    network_status: NetworkStatusSnapshot = Field(default_factory=NetworkStatusSnapshot)
    background_components: dict[str, str] = Field(default_factory=dict)
    runtime_degraded_reasons: list[str] = Field(default_factory=list)
    last_monitor_tick_at: datetime | None = None
    scheduled_tasks: list[dict] = Field(default_factory=list)
    proactive_alerts: list[dict] = Field(default_factory=list)
    memory_timeline: list[dict] = Field(default_factory=list)
    last_retrieval_query: str = ""
    recent_retrieval_hits: list[RetrievalHit] = Field(default_factory=list)
    recent_meeting_reviews: list[MeetingReviewSnapshot] = Field(default_factory=list)
    loop_metrics: LoopMetricsSnapshot = Field(default_factory=LoopMetricsSnapshot)
    last_snapshot_reason: str = "startup"
    dirty_flags: dict[str, bool] = Field(default_factory=dict)
    render_keys: RenderKeysSnapshot = Field(default_factory=RenderKeysSnapshot)
    model_route: ModelRouteSnapshot = Field(default_factory=ModelRouteSnapshot)


class UICommand(BaseModel):
    type: Literal[
        "submit_text",
        "confirm_action",
        "cancel_action",
        "update_settings",
        "reminder_action",
        "toggle_runtime_mute",
        "reset_conversation",
        "lock_profile",
        "unlock_profile",
        "set_profile_pin",
        "create_backup",
        "restore_backup",
        "review_action_item",
        "export_audit",
        "ingest_files",
        "create_schedule",
        "delete_schedule",
        "toggle_schedule",
        "dismiss_alert",
        "dismiss_all_alerts",
        "execute_suggested_action",
        "rerun_readiness",
        "retry_failure_action",
        "start_sample_workspace",
        "start_real_workspace",
    ]
    text: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
