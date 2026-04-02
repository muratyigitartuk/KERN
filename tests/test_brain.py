from app.llm import Brain
from app.persona import KERN_SYSTEM_PROMPT, PersonaEngine


def test_brain_uses_local_rule_mode_for_music_requests():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Play some morning jazz")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "play_spotify"
    assert parsed.tool_request.arguments["mode"] == "search_and_play"


def test_brain_returns_local_help_response():
    brain = Brain(None, local_mode_enabled=True)
    reply = brain.generate_chat_reply("What can you do?", "sir")
    assert "Spotify" in reply
    assert "calendar" in reply


def test_brain_separates_pause_and_resume_intents():
    brain = Brain(None, local_mode_enabled=True)
    pause_intent = brain.parse_intent("Pause Spotify")
    resume_intent = brain.parse_intent("Resume Spotify")
    assert pause_intent.tool_request.arguments["mode"] == "pause"
    assert resume_intent.tool_request.arguments["mode"] == "resume"


def test_brain_routes_good_morning_to_local_brief_tool():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Good morning")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "generate_morning_brief"


def test_brain_parses_local_reminder_request():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Remind me to stretch in 20 minutes")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "create_reminder"
    assert parsed.tool_request.arguments["kind"] == "reminder"


def test_brain_uses_dialogue_context_for_media_follow_up():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Play something calmer", dialogue_context={"last_media_query": "morning jazz"})
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "play_spotify"
    assert "calm" in parsed.tool_request.arguments["query"]


def test_brain_parses_runtime_mute_request():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Mute Kern")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "set_preference"
    assert parsed.tool_request.arguments["key"] == "muted"
    assert parsed.tool_request.arguments["value"] == "true"


def test_brain_parses_reminder_snooze_request():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Snooze reminder 7 15")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "snooze_reminder"
    assert parsed.tool_request.arguments["reminder_id"] == 7
    assert parsed.tool_request.arguments["minutes"] == 15


def test_brain_parses_routine_request():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Run the focus routine")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "run_routine"
    assert parsed.tool_request.arguments["name"] == "focus"


def test_brain_routes_music_status_question_to_status():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Do I have music set for the morning?")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "read_status"


def test_brain_generic_memory_recall_lists_all_facts():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("What do you remember?")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "recall_memory"
    assert parsed.tool_request.arguments["query"] == ""


def test_brain_semantic_memory_request_avoids_shared_memory_key():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Keep in mind that my editor is VS Code")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "remember_fact"
    assert parsed.tool_request.arguments["key"] != "memory"


def test_brain_does_not_route_meeting_statement_to_calendar():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("My meeting was moved")
    assert parsed.tool_request is None


def test_brain_asks_to_disambiguate_open_target():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Open github")
    assert parsed.tool_request is None
    assert parsed.missing_slots == ["target_type"]


def test_brain_parses_kern_mute_request():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Mute Kern")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "set_preference"


def test_brain_resolves_contextual_reminder_reference():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Dismiss that reminder", dialogue_context={"last_announced_reminder_id": "5"})
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "dismiss_reminder"
    assert parsed.tool_request.arguments["reminder_id"] == 5


def test_brain_parses_natural_document_search_request():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Search my documents for backups, restore, or encryption")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "search_documents"


def test_brain_parses_mailbox_summary_request():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Show my recent mailbox messages")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "read_mailbox_summary"


def test_brain_parses_backup_request_with_label():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Create an encrypted backup named before-weekend-checkpoint")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "create_backup"
    assert parsed.tool_request.arguments["label"] == "before-weekend-checkpoint"


def test_brain_parses_draft_angebot_request():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Create a draft Angebot for ACME GmbH")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "create_angebot"


def test_brain_parses_start_meeting_recording_request():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Start a meeting recording for frontend review")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "start_meeting_recording"


def test_brain_parses_runtime_snapshot_request():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Give me a quick runtime snapshot summary")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "read_runtime_snapshot"


def test_brain_parses_current_context_request():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Show my current context")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "read_current_context"


def test_brain_parses_audit_read_request():
    brain = Brain(None, local_mode_enabled=True)
    parsed = brain.parse_intent("Show recent audit events related to backup, sync, or restore")
    assert parsed.tool_request is not None
    assert parsed.tool_request.tool_name == "read_audit_events"


def test_persona_uses_word_boundaries_for_greetings():
    persona = PersonaEngine()
    reply = persona.chat_reply("Which backup is newest?", "sir")
    assert "Good to hear from you" not in reply.display_text


def test_kern_system_prompt_prefers_drafting_over_sending_for_write_requests():
    assert "produce the draft directly" in KERN_SYSTEM_PROMPT
    assert "explicitly ask you to send it" in KERN_SYSTEM_PROMPT
    assert "Reply in the same language" in KERN_SYSTEM_PROMPT


def test_brain_classifier_routes_scoped_operational_prompt():
    brain = Brain(None, local_mode_enabled=True, cognition_backend="hybrid")
    analysis = brain.analyze_intent(
        "Look through files for retention policy",
        available_capabilities=["search_files"],
    )

    assert analysis.execution_plan.steps
    assert analysis.execution_plan.steps[0].capability_name == "search_files"
    assert any(candidate.source == "classifier" for candidate in analysis.candidates)
    classifier_candidate = next(candidate for candidate in analysis.candidates if candidate.source == "classifier")
    assert classifier_candidate.confidence >= 0.6
    assert "similarity" in classifier_candidate.reason.lower()


def test_brain_classifier_abstains_on_ambiguous_operational_prompt():
    brain = Brain(None, local_mode_enabled=True, cognition_backend="hybrid")
    analysis = brain.analyze_intent(
        "Handle the thing from earlier",
        available_capabilities=["search_documents", "read_mailbox_summary"],
    )

    assert analysis.execution_plan.steps == []
    assert all(candidate.source != "classifier" for candidate in analysis.candidates)
