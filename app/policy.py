from __future__ import annotations

from dataclasses import dataclass, field

from app.capabilities import CapabilityRegistry
from app.config import settings
from app.types import CapabilityDescriptor, ExecutionPlan, PlanStep, PolicyDecision, ToolRequest


DENY_TOOLS = {"send_email", "delete_file", "financial_operation", "account_modification"}
CORPORATE_CONFIRM_TOOLS = {
    "compose_email",
    "sync_mailbox",
    "bulk_ingest",
    "import_conversation_archive",
    "create_backup",
    "restore_backup",
    "read_audit_events",
}
SENSITIVE_CLASSIFICATIONS = {"confidential", "finance", "legal", "hr"}
VALID_CLASSIFICATIONS = {"public", "internal", "confidential", "restricted", "finance", "legal", "hr"}
NETWORK_TOOLS = {"open_website", "browser_search"}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass(slots=True)
class PolicyEngine:
    mode: str = "personal"
    allow_external_network: bool = False
    confirm_tools: set[str] = field(default_factory=set)
    deny_tools: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        normalized_mode = (self.mode or "personal").strip().lower()
        self.mode = normalized_mode if normalized_mode in {"personal", "corporate"} else "personal"
        self.confirm_tools = set(self.confirm_tools)
        self.deny_tools = set(self.deny_tools)
        if self.mode == "corporate":
            self.confirm_tools.update(CORPORATE_CONFIRM_TOOLS)

    def summary(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "allow_external_network": self.allow_external_network,
            "restrict_sensitive_reads": settings.policy_restrict_sensitive_reads,
            "restrict_sensitive_exports": settings.policy_restrict_sensitive_exports,
            "sensitive_classifications": sorted(SENSITIVE_CLASSIFICATIONS),
            "confirm_tools": sorted(self.confirm_tools),
            "deny_tools": sorted(self.deny_tools | DENY_TOOLS),
        }

    def is_sensitive_classification(self, classification: str | None) -> bool:
        normalized = str(classification or "").strip().lower()
        return normalized in SENSITIVE_CLASSIFICATIONS

    def restricts_sensitive_reads(self) -> bool:
        return self.mode == "corporate" and settings.policy_restrict_sensitive_reads

    def restricts_sensitive_exports(self) -> bool:
        return self.mode == "corporate" and settings.policy_restrict_sensitive_exports

    def _raise_risk(self, risk_level: str, minimum: str) -> str:
        return minimum if RISK_ORDER.get(risk_level, 0) < RISK_ORDER.get(minimum, 0) else risk_level

    def decide(self, request: ToolRequest, descriptor: CapabilityDescriptor | None = None) -> PolicyDecision:
        return self.decide_step(
            PlanStep(
                capability_name=request.tool_name,
                arguments=request.arguments,
                reason=request.reason,
            ),
            descriptor=descriptor,
        )

    def decide_step(self, step: PlanStep, descriptor: CapabilityDescriptor | None = None) -> PolicyDecision:
        tool_name = step.capability_name
        if tool_name in DENY_TOOLS or tool_name in self.deny_tools:
            return PolicyDecision(
                verdict="deny",
                risk_level="high",
                message="That action is restricted.",
                policy_scope=self.mode,
                policy_reason="tool_deny_list",
            )
        if descriptor is None:
            return PolicyDecision(
                verdict="confirm",
                risk_level="medium",
                message="Unknown actions require confirmation.",
                policy_scope=self.mode,
                policy_reason="unknown_capability",
            )
        if tool_name == "open_website" and str(step.arguments.get("url", "")).startswith("file:"):
            return PolicyDecision(
                verdict="deny",
                risk_level="high",
                message="Opening local file URLs is blocked.",
                policy_scope=self.mode,
                policy_reason="file_url_blocked",
            )
        if tool_name in NETWORK_TOOLS and self.mode == "corporate" and not self.allow_external_network:
            return PolicyDecision(
                verdict="confirm",
                risk_level="high",
                message="External network actions require confirmation in corporate mode.",
                policy_scope=self.mode,
                policy_reason="external_network_guard",
            )
        classification = str(
            step.arguments.get("classification")
            or step.arguments.get("document_classification")
            or step.metadata.get("classification")
            or ""
        ).strip().lower()
        # Reject unknown classification values — default to "internal" (most restrictive)
        if classification and classification not in VALID_CLASSIFICATIONS:
            import logging
            logging.getLogger(__name__).warning(
                "Unknown classification '%s' rejected, defaulting to 'internal'", classification
            )
            classification = "internal"
        if self.mode == "corporate" and classification in SENSITIVE_CLASSIFICATIONS:
            return PolicyDecision(
                verdict="confirm",
                risk_level="high",
                message=f"{classification.title()} data requires explicit confirmation in corporate mode.",
                policy_scope=self.mode,
                policy_reason="sensitive_classification",
            )
        if self.mode == "corporate" and tool_name in self.confirm_tools:
            return PolicyDecision(
                verdict="confirm",
                risk_level=self._raise_risk(descriptor.risk_level, "medium"),
                message="Corporate mode requires confirmation for this action.",
                policy_scope=self.mode,
                policy_reason="corporate_confirmation",
            )
        if descriptor.confirmation_rule == "always":
            return PolicyDecision(
                verdict="confirm",
                risk_level=descriptor.risk_level,
                message="Confirmation required before I do that.",
                policy_scope=self.mode,
                policy_reason="capability_rule",
            )
        if descriptor.confirmation_rule == "on_risk" and descriptor.risk_level in {"medium", "high"}:
            return PolicyDecision(
                verdict="confirm",
                risk_level=descriptor.risk_level,
                message="Confirmation required before I do that.",
                policy_scope=self.mode,
                policy_reason="risk_gated",
            )
        return PolicyDecision(
            verdict="allow",
            risk_level=descriptor.risk_level,
            message="Safe to execute immediately.",
            policy_scope=self.mode,
            policy_reason="allowed",
        )

    def decide_plan(self, plan: ExecutionPlan, capabilities: CapabilityRegistry | None = None) -> PolicyDecision:
        if not plan.steps:
            return PolicyDecision(
                verdict="allow",
                risk_level="low",
                message="No side effects planned.",
                policy_scope=self.mode,
                policy_reason="empty_plan",
            )
        highest: PolicyDecision | None = None
        for index, step in enumerate(plan.steps):
            descriptor = capabilities.get_descriptor(step.capability_name) if capabilities else None
            decision = self.decide_step(step, descriptor=descriptor)
            decision.step_index = index
            if decision.verdict == "deny":
                return decision
            if decision.verdict == "confirm":
                highest = decision
        return highest or PolicyDecision(
            verdict="allow",
            risk_level="low",
            message="Safe to execute immediately.",
            policy_scope=self.mode,
            policy_reason="plan_allowed",
        )
