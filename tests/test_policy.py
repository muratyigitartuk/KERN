from app.policy import PolicyEngine
from app.types import CapabilityDescriptor, ToolRequest


def test_policy_denies_high_risk_actions():
    engine = PolicyEngine()
    decision = engine.decide(
        ToolRequest(
            tool_name="delete_file",
            arguments={"path": "C:/secret.txt"},
            user_utterance="delete this",
            reason="test",
        )
    )
    assert decision.verdict == "deny"
    assert decision.risk_level == "high"


def test_policy_requires_confirmation_for_unknown_actions():
    engine = PolicyEngine()
    decision = engine.decide(
        ToolRequest(
            tool_name="mystery_tool",
            arguments={},
            user_utterance="do something odd",
            reason="test",
        )
    )
    assert decision.verdict == "confirm"


def test_policy_uses_capability_metadata_for_known_actions():
    engine = PolicyEngine()
    plan_step_request = ToolRequest(
        tool_name="open_app",
        arguments={"app": "notepad"},
        user_utterance="open notepad",
        reason="test",
    )
    descriptor = CapabilityDescriptor(
        name="open_app",
        title="Launch App",
        summary="Launch a desktop application.",
        risk_level="medium",
        confirmation_rule="on_risk",
        side_effectful=True,
    )
    decision = engine.decide(plan_step_request, descriptor=descriptor)
    assert decision.verdict == "confirm"


def test_corporate_policy_confirms_external_network_actions():
    engine = PolicyEngine(mode="corporate", allow_external_network=False)
    decision = engine.decide(
        ToolRequest(
            tool_name="open_website",
            arguments={"url": "https://example.com"},
            user_utterance="open example.com",
            reason="test",
        ),
        descriptor=CapabilityDescriptor(
            name="open_website",
            title="Open Website",
            summary="Open a website.",
            risk_level="medium",
            confirmation_rule="never",
            side_effectful=True,
        ),
    )
    assert decision.verdict == "confirm"
    assert decision.policy_scope == "corporate"
    assert decision.policy_reason == "external_network_guard"


def test_corporate_policy_confirms_sensitive_classification():
    engine = PolicyEngine(mode="corporate")
    decision = engine.decide(
        ToolRequest(
            tool_name="compose_email",
            arguments={"classification": "finance"},
            user_utterance="send the finance file",
            reason="test",
        ),
        descriptor=CapabilityDescriptor(
            name="compose_email",
            title="Send Email",
            summary="Send email.",
            risk_level="high",
            confirmation_rule="always",
            side_effectful=True,
        ),
    )
    assert decision.verdict == "confirm"
    assert decision.policy_reason in {"sensitive_classification", "corporate_confirmation"}


def test_corporate_policy_keeps_bulk_ingest_confirmed() -> None:
    engine = PolicyEngine(mode="corporate")
    decision = engine.decide(
        ToolRequest(
            tool_name="bulk_ingest",
            arguments={"folder_path": "C:/docs"},
            user_utterance="ingest this folder",
            reason="test",
        ),
        descriptor=CapabilityDescriptor(
            name="bulk_ingest",
            title="Bulk Ingest",
            summary="Import a folder or large file batch.",
            domain="documents",
            risk_level="medium",
            confirmation_rule="on_risk",
            side_effectful=True,
            verification_support="database",
        ),
    )
    assert decision.verdict == "confirm"
    assert decision.policy_reason == "corporate_confirmation"


def test_corporate_policy_allows_manual_upload_descriptor() -> None:
    engine = PolicyEngine(mode="corporate")
    decision = engine.decide(
        ToolRequest(
            tool_name="upload_documents",
            arguments={"category": "", "tags": ""},
            user_utterance="upload this pdf",
            reason="test",
        ),
        descriptor=CapabilityDescriptor(
            name="upload_documents",
            title="Upload Documents",
            summary="Upload a small set of local documents into the active profile archive.",
            domain="documents",
            risk_level="low",
            confirmation_rule="never",
            side_effectful=True,
            verification_support="database",
        ),
    )
    assert decision.verdict == "allow"
    assert decision.policy_reason == "allowed"
