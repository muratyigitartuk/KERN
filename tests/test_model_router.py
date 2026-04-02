from app.model_router import ModelRouter
from app.types import ActiveContextSummary, TaskSummary


def test_model_router_prefers_deep_route_for_complex_retrieval_prompt():
    router = ModelRouter(mode="auto", fast_model="fast.gguf", deep_model="deep.gguf")
    context = ActiveContextSummary(
        preferred_title="sir",
        tasks=[TaskSummary(title=f"Task {index}") for index in range(3)],
    )

    route = router.choose(
        "Compare these documents and explain the most important contract differences.",
        context_summary=context,
        rag_candidate=True,
        llm_available=True,
    )

    assert route.selected_mode == "deep"
    assert route.used_rag is True
    assert route.requested_model == "deep"


def test_model_router_prompt_cache_tracks_hit_and_miss():
    router = ModelRouter(mode="auto", fast_model="fast.gguf", deep_model="deep.gguf")
    route = router.choose(
        "Say good morning.",
        context_summary=ActiveContextSummary(preferred_title="sir"),
        rag_candidate=False,
        llm_available=True,
    )
    history = [{"role": "user", "content": "Say good morning."}]

    miss, cache_key = router.cache_lookup(route, "Say good morning.", history)
    assert miss is None

    router.cache_store(cache_key, "Good morning.")
    hit, _ = router.cache_lookup(route, "Say good morning.", history)
    snapshot = router.cache_snapshot()

    assert hit == "Good morning."
    assert snapshot.hits == 1
    assert snapshot.misses == 1


def test_model_router_avoids_rag_for_drafting_request_without_explicit_document_grounding():
    router = ModelRouter(mode="auto", fast_model="fast.gguf", deep_model="deep.gguf")

    route = router.choose(
        "Schreibe eine kurze professionelle E-Mail an den Lieferanten und bitte um Bestaetigung der USt-IdNr.",
        context_summary=ActiveContextSummary(preferred_title="sir"),
        rag_candidate=True,
        llm_available=True,
    )

    assert route.used_rag is False


def test_model_router_avoids_rag_for_capability_question():
    router = ModelRouter(mode="auto", fast_model="fast.gguf", deep_model="deep.gguf")

    route = router.choose(
        "What can you actually help me with in this workspace?",
        context_summary=ActiveContextSummary(preferred_title="sir"),
        rag_candidate=True,
        llm_available=True,
    )

    assert route.used_rag is False


def test_model_router_keeps_rag_for_explicit_uploaded_document_question():
    router = ModelRouter(mode="auto", fast_model="fast.gguf", deep_model="deep.gguf")

    route = router.choose(
        "Aus den hochgeladenen ACME-Dokumenten: Worum geht es im Angebot und wann ist die Rechnung faellig?",
        context_summary=ActiveContextSummary(preferred_title="sir"),
        rag_candidate=True,
        llm_available=True,
    )

    assert route.used_rag is True
