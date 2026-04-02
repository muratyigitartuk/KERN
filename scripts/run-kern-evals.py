from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cognition import HybridCognitionEngine
from app.config import settings
from app.database import connect
from app.documents import DocumentService
from app.intent import RuleBasedIntentEngine
from app.knowledge_graph import KnowledgeGraphService
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.model_router import ModelRouter
from app.policy import PolicyEngine
from app.rag import RAGPipeline
from app.retrieval import RetrievalService
from app.types import CapabilityDescriptor, RetrievalHit
from app.tools.memory_tools import RecallMemoryTool
from app.types import ActiveContextSummary, TaskSummary, ToolRequest
from app.action_planner import ActionPlanner
from app.llm_client import LlamaServerClient


def _load_cases(name: str) -> list[dict[str, object]]:
    return json.loads((EVAL_ROOT / name).read_text(encoding="utf-8"))


def run_routing_eval() -> dict[str, object]:
    engine = HybridCognitionEngine(intent_fallback_mode="off")
    cases = _load_cases("routing.json")
    passed = 0
    results: list[dict[str, object]] = []
    for case in cases:
        prompt = str(case["prompt"])
        expected_tool = str(case["expected_tool"])
        result = engine.analyze(prompt, available_capabilities=[expected_tool])
        actual_tool = result.execution_plan.steps[0].capability_name if result.execution_plan.steps else None
        ok = actual_tool == expected_tool
        passed += int(ok)
        results.append(
            {
                "prompt": prompt,
                "expected_tool": expected_tool,
                "actual_tool": actual_tool,
                "ok": ok,
            }
        )
    return {
        "suite": "routing",
        "passed": passed,
        "total": len(cases),
        "score": round(passed / max(len(cases), 1), 3),
        "cases": results,
    }


def run_routing_baseline_eval() -> dict[str, object]:
    engine = RuleBasedIntentEngine()
    cases = _load_cases("routing.json")
    passed = 0
    results: list[dict[str, object]] = []
    for case in cases:
        prompt = str(case["prompt"])
        expected_tool = str(case["expected_tool"])
        result = engine.parse(prompt)
        actual_tool = result.tool_request.tool_name if result.tool_request else None
        ok = actual_tool == expected_tool
        passed += int(ok)
        results.append(
            {
                "prompt": prompt,
                "expected_tool": expected_tool,
                "actual_tool": actual_tool,
                "ok": ok,
            }
        )
    return {
        "suite": "routing_baseline",
        "passed": passed,
        "total": len(cases),
        "score": round(passed / max(len(cases), 1), 3),
        "threshold_exempt": True,
        "cases": results,
    }


def run_tool_selection_eval() -> dict[str, object]:
    engine = HybridCognitionEngine(intent_fallback_mode="off")
    cases = _load_cases("tool_selection.json")
    passed = 0
    results: list[dict[str, object]] = []
    for case in cases:
        prompt = str(case["prompt"])
        available = [str(item) for item in case.get("available_capabilities", [])]
        expected_tool = str(case.get("expected_tool", "") or "")
        expect_abstain = bool(case.get("expect_abstain", False))
        min_confidence = float(case.get("min_confidence", 0.0) or 0.0)
        result = engine.analyze(prompt, available_capabilities=available)
        actual_tool = result.execution_plan.steps[0].capability_name if result.execution_plan.steps else None
        classifier_candidate = next((candidate for candidate in result.candidates if candidate.source == "classifier"), None)
        ok = False
        if expect_abstain:
            ok = actual_tool is None and classifier_candidate is None
        else:
            ok = actual_tool == expected_tool and classifier_candidate is not None and classifier_candidate.confidence >= min_confidence
        passed += int(ok)
        results.append(
            {
                "prompt": prompt,
                "available_capabilities": available,
                "expected_tool": expected_tool or None,
                "expect_abstain": expect_abstain,
                "actual_tool": actual_tool,
                "classifier_confidence": classifier_candidate.confidence if classifier_candidate else None,
                "plan_source": result.execution_plan.source,
                "ok": ok,
            }
        )
    return {
        "suite": "tool_selection",
        "passed": passed,
        "total": len(cases),
        "score": round(passed / max(len(cases), 1), 3),
        "cases": results,
    }


def run_retrieval_eval() -> dict[str, object]:
    cases = _load_cases("retrieval.json")
    with tempfile.TemporaryDirectory(prefix="kern_eval_") as tmp:
        tmp_path = Path(tmp)
        documents_root = tmp_path / "documents"
        archives_root = tmp_path / "archives"
        documents_root.mkdir(parents=True, exist_ok=True)
        archives_root.mkdir(parents=True, exist_ok=True)
        memory = MemoryRepository(connect(tmp_path / "eval.db"))
        service = DocumentService(memory.connection, documents_root, archives_root)
        original_rag_enabled = settings.rag_enabled
        original_index_version = settings.rag_index_version
        settings.rag_enabled = True
        settings.rag_index_version = "eval-v1"
        try:
            retrieval = RetrievalService(memory)

            for case in cases:
                path = documents_root / str(case["path"])
                path.write_text(str(case["content"]), encoding="utf-8")
                service.ingest_document(path)

            passed = 0
            results: list[dict[str, object]] = []
            for case in cases:
                query = str(case["query"])
                expected = str(case["expected_title"]).lower()
                hits = retrieval.retrieve(query, scope="profile", limit=3)
                top_title = str(hits[0].metadata.get("title", "")).lower() if hits else ""
                ok = expected in top_title
                passed += int(ok)
                results.append(
                    {
                        "query": query,
                        "expected_title": expected,
                        "top_title": top_title,
                        "hit_count": len(hits),
                        "ok": ok,
                    }
                )
        finally:
            settings.rag_enabled = original_rag_enabled
            settings.rag_index_version = original_index_version
            memory.connection.close()
    return {
        "suite": "retrieval",
        "passed": passed,
        "total": len(cases),
        "score": round(passed / max(len(cases), 1), 3),
        "cases": results,
    }


def run_memory_recall_eval() -> dict[str, object]:
    cases = _load_cases("memory_recall.json")
    with tempfile.TemporaryDirectory(prefix="kern_memory_eval_") as tmp:
        tmp_path = Path(tmp)
        memory = MemoryRepository(connect(tmp_path / "memory.db"))
        local_data = LocalDataService(memory, "sir")
        tool = RecallMemoryTool(local_data)
        passed = 0
        results: list[dict[str, object]] = []
        for case in cases:
            for key, value in case.get("facts", []):
                local_data.remember_fact(str(key), str(value))
            query = str(case["query"])
            expected_terms = [str(term).lower() for term in case.get("expected_terms", [])]
            result = asyncio.run(
                tool.run(
                    ToolRequest(
                        tool_name="recall_memory",
                        arguments={"query": query},
                        user_utterance=query,
                        reason="eval",
                        trigger_source="manual_ui",
                    )
                )
            )
            actual = result.display_text.lower()
            ok = all(term in actual for term in expected_terms)
            passed += int(ok)
            results.append({"query": query, "expected_terms": expected_terms, "actual": actual, "ok": ok})
        memory.connection.close()
    return {
        "suite": "memory_recall",
        "passed": passed,
        "total": len(cases),
        "score": round(passed / max(len(cases), 1), 3),
        "cases": results,
    }


def run_model_routing_eval() -> dict[str, object]:
    cases = _load_cases("model_routing.json")
    router = ModelRouter(mode="auto", fast_model="fast.gguf", deep_model="deep.gguf")
    passed = 0
    results: list[dict[str, object]] = []
    for case in cases:
        prompt = str(case["prompt"])
        expected = str(case["expected_mode"])
        task_pressure = int(case.get("task_pressure", 0) or 0)
        context = ActiveContextSummary(
            preferred_title="sir",
            tasks=[TaskSummary(title=f"Task {index}") for index in range(task_pressure)],
        )
        decision = router.choose(
            prompt,
            context_summary=context,
            llm_available=True,
            rag_candidate=bool(case.get("rag_candidate", False)),
        )
        expected_rag = bool(case.get("expected_rag", False))
        ok = decision.selected_mode == expected and decision.used_rag == expected_rag
        passed += int(ok)
        results.append(
            {
                "prompt": prompt,
                "expected_mode": expected,
                "expected_rag": expected_rag,
                "actual_mode": decision.selected_mode,
                "actual_rag": decision.used_rag,
                "reason": decision.reason,
                "ok": ok,
            }
        )
    return {
        "suite": "model_routing",
        "passed": passed,
        "total": len(cases),
        "score": round(passed / max(len(cases), 1), 3),
        "cases": results,
    }


def run_model_routing_baseline_eval() -> dict[str, object]:
    cases = _load_cases("model_routing.json")
    router = ModelRouter(mode="fast", fast_model="fast.gguf", deep_model="deep.gguf")
    passed = 0
    results: list[dict[str, object]] = []
    for case in cases:
        prompt = str(case["prompt"])
        expected = str(case["expected_mode"])
        task_pressure = int(case.get("task_pressure", 0) or 0)
        context = ActiveContextSummary(
            preferred_title="sir",
            tasks=[TaskSummary(title=f"Task {index}") for index in range(task_pressure)],
        )
        decision = router.choose(
            prompt,
            context_summary=context,
            llm_available=True,
            rag_candidate=bool(case.get("rag_candidate", False)),
        )
        expected_rag = bool(case.get("expected_rag", False))
        ok = decision.selected_mode == expected and decision.used_rag == expected_rag
        passed += int(ok)
        results.append(
            {
                "prompt": prompt,
                "expected_mode": expected,
                "expected_rag": expected_rag,
                "actual_mode": decision.selected_mode,
                "actual_rag": decision.used_rag,
                "reason": decision.reason,
                "ok": ok,
            }
        )
    return {
        "suite": "model_routing_baseline",
        "passed": passed,
        "total": len(cases),
        "score": round(passed / max(len(cases), 1), 3),
        "threshold_exempt": True,
        "cases": results,
    }


def run_prompt_cache_eval() -> dict[str, object]:
    cases = _load_cases("prompt_cache.json")
    router = ModelRouter(mode="auto", fast_model="fast.gguf", deep_model="deep.gguf")
    route = router.choose("Summarize the current workspace state.", llm_available=True, rag_candidate=True)
    passed = 0
    results: list[dict[str, object]] = []
    history = [{"role": "user", "content": "Summarize the current workspace state."}]
    for case in cases:
        first_key = router.cache.key_for(
            route=route,
            text=str(case["text"]),
            history=history,
            context_revision=str(case.get("context_revision", "")),
            knowledge_revision=str(case.get("knowledge_revision", "")),
            memory_revision=str(case.get("memory_revision", "")),
        )
        second_key = router.cache.key_for(
            route=route,
            text=str(case["text"]),
            history=history,
            context_revision=str(case.get("other_context_revision", case.get("context_revision", ""))),
            knowledge_revision=str(case.get("other_knowledge_revision", case.get("knowledge_revision", ""))),
            memory_revision=str(case.get("other_memory_revision", case.get("memory_revision", ""))),
        )
        router.cache_store(first_key, "cached")
        hit, _ = router.cache_lookup(
            route,
            str(case["text"]),
            history,
            context_revision=str(case.get("context_revision", "")),
            knowledge_revision=str(case.get("knowledge_revision", "")),
            memory_revision=str(case.get("memory_revision", "")),
        )
        different = first_key != second_key
        ok = bool(hit == "cached") and different == bool(case.get("expect_different_key", True))
        passed += int(ok)
        results.append(
            {
                "text": case["text"],
                "cache_hit": hit == "cached",
                "expect_different_key": bool(case.get("expect_different_key", True)),
                "different_key": different,
                "ok": ok,
            }
        )
    return {
        "suite": "prompt_cache",
        "passed": passed,
        "total": len(cases),
        "score": round(passed / max(len(cases), 1), 3),
        "cases": results,
    }


def run_policy_truth_eval() -> dict[str, object]:
    cases = _load_cases("policy_truth.json")
    policy = PolicyEngine(mode="corporate", allow_external_network=False)
    passed = 0
    results: list[dict[str, object]] = []
    for case in cases:
        request = ToolRequest(
            tool_name=str(case["tool_name"]),
            arguments=dict(case.get("arguments", {})),
            user_utterance=str(case.get("prompt", "")),
            reason="eval",
        )
        descriptor = CapabilityDescriptor(
            name=str(case["tool_name"]),
            title=str(case.get("title", case["tool_name"])),
            summary="eval",
            domain="core",
            risk_level=str(case.get("risk_level", "medium")),
        )
        decision = policy.decide(request, descriptor=descriptor)
        expected = str(case["expected_verdict"])
        ok = decision.verdict == expected
        passed += int(ok)
        results.append(
            {
                "tool_name": case["tool_name"],
                "expected_verdict": expected,
                "actual_verdict": decision.verdict,
                "reason": decision.policy_reason,
                "ok": ok,
            }
        )
    return {
        "suite": "policy_truth",
        "passed": passed,
        "total": len(cases),
        "score": round(passed / max(len(cases), 1), 3),
        "cases": results,
    }


def run_knowledge_graph_eval() -> dict[str, object]:
    cases = _load_cases("knowledge_graph.json")
    with tempfile.TemporaryDirectory(prefix="kern_kg_eval_") as tmp:
        tmp_path = Path(tmp)
        memory = MemoryRepository(connect(tmp_path / "kg.db"))
        graph = KnowledgeGraphService(memory.connection, "default")
        passed = 0
        results: list[dict[str, object]] = []
        for case in cases:
            graph.extract_from_document(str(case["document_id"]), str(case["text"]))
            entities = graph.search_entities(str(case["query"]), limit=5)
            relationships: set[str] = set()
            if entities:
                neighborhood = graph.get_neighborhood(str(entities[0]["id"]), depth=int(case.get("depth", 2) or 2))
                relationships = {str(edge["relationship"]) for edge in neighborhood.get("edges", [])}
                provenance_ok = any(
                    str(case["document_id"]) in item["metadata"].get("source_document_ids", [])
                    for item in entities
                )
            else:
                provenance_ok = False
            expected_rel = set(str(item) for item in case.get("expected_relationships", []))
            ok = bool(entities) and provenance_ok and expected_rel.issubset(relationships)
            passed += int(ok)
            results.append(
                {
                    "query": case["query"],
                    "entity_count": len(entities),
                    "relationships": sorted(relationships),
                    "provenance_ok": provenance_ok,
                    "ok": ok,
                }
            )
        memory.connection.close()
    return {
        "suite": "knowledge_graph",
        "passed": passed,
        "total": len(cases),
        "score": round(passed / max(len(cases), 1), 3),
        "cases": results,
    }


def run_proactive_alerts_eval() -> dict[str, object]:
    cases = _load_cases("proactive_alerts.json")
    with tempfile.TemporaryDirectory(prefix="kern_alert_eval_") as tmp:
        tmp_path = Path(tmp)
        memory = MemoryRepository(connect(tmp_path / "alerts.db"))
        local_data = LocalDataService(memory, "sir")
        planner = ActionPlanner()
        passed = 0
        results: list[dict[str, object]] = []
        for case in cases:
            local_data.set_assistant_mode(str(case.get("assistant_mode", "manual")))
            now = datetime_from_iso(str(case.get("now", "2026-03-21T09:00:00")))
            alerts = list(case.get("alerts", []))
            ranked = planner.rank_alerts(alerts, local_data, now=now)
            if ranked and case.get("feedback"):
                feedback = dict(case["feedback"])
                planner.record_feedback(
                    local_data,
                    ranked[0],
                    str(feedback.get("outcome", "dismissed")),
                    action_type=str(feedback.get("action_type")) if feedback.get("action_type") else None,
                )
                ranked = planner.rank_alerts(alerts, local_data, now=now)
            top_type = str(ranked[0]["type"]) if ranked else ""
            top_priority = str(ranked[0].get("priority", "")) if ranked else ""
            ok = top_type == str(case["expected_top_type"]) and top_priority == str(case["expected_top_priority"])
            passed += int(ok)
            results.append(
                {
                    "expected_top_type": case["expected_top_type"],
                    "actual_top_type": top_type,
                    "expected_top_priority": case["expected_top_priority"],
                    "actual_top_priority": top_priority,
                    "ok": ok,
                }
            )
        memory.connection.close()
    return {
        "suite": "proactive_alerts",
        "passed": passed,
        "total": len(cases),
        "score": round(passed / max(len(cases), 1), 3),
        "cases": results,
    }


def run_cross_document_reasoning_eval() -> dict[str, object]:
    cases = _load_cases("cross_document_reasoning.json")
    passed = 0
    results: list[dict[str, object]] = []
    for case in cases:
        hits = [
            RetrievalHit(
                source_type="document",
                source_id=str(item["source_id"]),
                score=float(item.get("score", 0.8)),
                text=str(item["text"]),
                metadata={"title": str(item["title"]), "document_id": str(item["source_id"])},
            )
            for item in case.get("documents", [])
        ]
        retrieval = _EvalRetrievalService(hits)
        rag = RAGPipeline(retrieval=retrieval, reranker=None, llm_client=None)
        result = asyncio.run(
            rag.answer_multi_document(
                str(case["query"]),
                [str(item["source_id"]) for item in case.get("documents", [])],
            )
        )
        answer = result.answer.lower()
        expected_terms = [str(term).lower() for term in case.get("expected_terms", [])]
        ok = all(term in answer for term in expected_terms)
        passed += int(ok)
        results.append({"query": case["query"], "expected_terms": expected_terms, "answer": answer, "ok": ok})
    return {
        "suite": "cross_document_reasoning",
        "passed": passed,
        "total": len(cases),
        "score": round(passed / max(len(cases), 1), 3),
        "cases": results,
    }


def run_local_model_smoke_eval() -> dict[str, object]:
    if not settings.llm_enabled:
        return {
            "suite": "local_model_smoke",
            "passed": 0,
            "total": 0,
            "score": 1.0,
            "cases": [{"skipped": True, "reason": "KERN_LLM_ENABLED is false."}],
        }
    async def _run() -> bool:
        client = LlamaServerClient(settings.llama_server_url, timeout=settings.llama_server_timeout)
        await client.startup()
        try:
            return await client.health()
        finally:
            await client.shutdown()
    healthy = False
    try:
        healthy = asyncio.run(_run())
    except Exception:
        healthy = False
    return {
        "suite": "local_model_smoke",
        "passed": int(healthy),
        "total": 1,
        "score": 1.0 if healthy else 0.0,
        "cases": [{"endpoint": settings.llama_server_url, "healthy": healthy, "ok": healthy}],
    }


def datetime_from_iso(raw: str):
    from datetime import datetime
    return datetime.fromisoformat(raw)


class _EvalRetrievalService:
    def __init__(self, hits: list[RetrievalHit]) -> None:
        self._hits = hits

    def retrieve(self, query: str, scope: str = "profile", limit: int = 8) -> list[RetrievalHit]:
        return self._hits[:limit]

    def retrieve_from_documents(self, query: str, document_ids: list[str], limit: int = 8) -> list[RetrievalHit]:
        allowed = set(document_ids)
        return [hit for hit in self._hits if hit.source_id in allowed or hit.metadata.get("document_id") in allowed][:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run lightweight local KERN eval suites.")
    parser.add_argument("--fail-under", type=float, default=0.8, help="Fail if any suite score falls below this threshold.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--compare-baseline", action="store_true", help="Include baseline comparison suites.")
    parser.add_argument("--include-optional", action="store_true", help="Run optional suites such as local-model smoke checks.")
    args = parser.parse_args()

    suites = [
        run_routing_eval(),
        run_tool_selection_eval(),
        run_retrieval_eval(),
        run_memory_recall_eval(),
        run_model_routing_eval(),
        run_prompt_cache_eval(),
        run_policy_truth_eval(),
        run_knowledge_graph_eval(),
        run_proactive_alerts_eval(),
        run_cross_document_reasoning_eval(),
    ]
    if args.compare_baseline:
        suites.extend([run_routing_baseline_eval(), run_model_routing_baseline_eval()])
    if args.include_optional:
        suites.append(run_local_model_smoke_eval())
    overall = {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat(),
        "suites": suites,
        "min_score": min(suite["score"] for suite in suites if not suite.get("threshold_exempt")),
    }

    if args.json:
        print(json.dumps(overall, indent=2))
    else:
        print("KERN eval summary")
        for suite in suites:
            print(f"- {suite['suite']}: {suite['passed']}/{suite['total']} ({suite['score']:.3f})")

    return 1 if any(float(suite["score"]) < args.fail_under for suite in suites if not suite.get("threshold_exempt")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
