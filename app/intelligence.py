from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.memory import MemoryRepository
from app.platform import PlatformStore
from app.types import FeedbackSignal, ProfileSummary, TrainingExampleRecord


class IntelligenceService:
    TOKEN_PATTERN = re.compile(r"[0-9A-Za-zÄÖÜäöüß_-]+")

    def __init__(self, platform: PlatformStore, memory: MemoryRepository, profile: ProfileSummary) -> None:
        self.platform = platform
        self.memory = memory
        self.profile = profile
        self.training_root = Path(self.profile.profile_root) / "training-exports"
        self.training_root.mkdir(parents=True, exist_ok=True)

    def list_memory(
        self,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        user_id: str | None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        return self.memory.list_structured_memory_items(
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            user_id=user_id,
            limit=limit,
        )

    def capture_feedback(
        self,
        *,
        actor_user_id: str | None,
        organization_id: str | None,
        workspace_slug: str | None,
        signal_type: str,
        source_type: str,
        source_id: str,
        memory_item_id: str | None = None,
        metadata: dict[str, object] | None = None,
        approved_for_training: bool = False,
    ) -> FeedbackSignal:
        signal = FeedbackSignal(
            id=str(uuid4()),
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            user_id=actor_user_id,
            signal_type=signal_type,
            source_type=source_type,
            source_id=source_id,
            memory_item_id=memory_item_id,
            approved_for_training=approved_for_training,
            metadata=metadata or {},
        )
        stored = self.memory.record_feedback_signal(signal)
        if memory_item_id:
            approved_delta = 1 if signal_type in {"use_again", "promote_workspace", "packet_accepted", "evidence_used"} else 0
            rejected_delta = 1 if signal_type in {"reject_pattern", "packet_ignored"} else 0
            promotion_state = None
            if signal_type == "promote_workspace":
                promotion_state = "workspace_promoted"
            elif signal_type == "personal_only":
                promotion_state = "personal_only"
            elif signal_type == "reject_pattern":
                promotion_state = "rejected"
            elif signal_type in {"use_again", "packet_accepted", "evidence_used"}:
                item = self.memory.get_structured_memory_item(memory_item_id)
                current_approved = int(item.get("approved_count", 0) or 0) if item else 0
                if current_approved + approved_delta >= 1:
                    promotion_state = "candidate"
            self.memory.update_structured_memory_feedback(
                memory_item_id,
                approved_delta=approved_delta,
                rejected_delta=rejected_delta,
                promotion_state=promotion_state,
            )
        return stored

    def retrieve_memory_context(
        self,
        query: str,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        user_id: str | None,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        query_terms = self._tokenize(query)
        scored: list[tuple[float, dict[str, object]]] = []
        for item in self.list_memory(
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            user_id=user_id,
            limit=300,
        ):
            haystack = f"{item['key']} {item['value']}".lower()
            overlap = sum(1 for token in query_terms if token in haystack)
            if overlap <= 0:
                continue
            scope_bonus = 0.0
            if user_id and item.get("user_id") == user_id:
                scope_bonus += 0.3
            elif workspace_slug and item.get("workspace_slug") == workspace_slug:
                scope_bonus += 0.2
            elif organization_id and item.get("organization_id") == organization_id:
                scope_bonus += 0.1
            score = overlap + scope_bonus + (float(item.get("approved_count", 0)) * 0.05) - (float(item.get("rejected_count", 0)) * 0.08)
            scored.append(
                (
                    score,
                    {
                        **item,
                        "score": round(score, 4),
                        "provenance": {
                            "scope": "user_private" if item.get("user_id") else "workspace" if item.get("workspace_slug") else "organization_shared",
                            "workspace_slug": item.get("workspace_slug"),
                            "user_id": item.get("user_id"),
                            "confidence": item.get("confidence"),
                            "policy_safe": item.get("data_class") != "personal" or item.get("user_id") == user_id,
                            "source_type": item.get("memory_kind"),
                        },
                    },
                )
            )
        scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("key", ""))))
        return [item for _, item in scored[:limit]]

    def record_training_example(
        self,
        *,
        actor_user_id: str | None,
        organization_id: str | None,
        workspace_slug: str | None,
        source_type: str,
        source_id: str,
        input_text: str,
        output_text: str,
        status: str = "candidate",
        approved_for_training: bool = False,
        metadata: dict[str, object] | None = None,
    ) -> TrainingExampleRecord:
        now = datetime.now(timezone.utc)
        example = TrainingExampleRecord(
            id=str(uuid4()),
            profile_slug=self.profile.slug,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            user_id=actor_user_id,
            source_type=source_type,
            source_id=source_id,
            input_text=input_text,
            output_text=output_text,
            status=status,
            approved_for_training=approved_for_training,
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )
        return self.memory.upsert_training_example(example)

    def list_training_examples(
        self,
        *,
        workspace_slug: str | None,
        user_id: str | None = None,
        approved_only: bool = False,
        limit: int = 200,
    ) -> list[TrainingExampleRecord]:
        return self.memory.list_training_examples(
            workspace_slug=workspace_slug,
            user_id=user_id,
            approved_only=approved_only,
            limit=limit,
        )

    def review_training_example(
        self,
        example_id: str,
        *,
        status: str,
        actor_user_id: str | None,
        reason: str = "",
    ) -> TrainingExampleRecord | None:
        approved = status == "approved"
        return self.memory.update_training_example_status(
            example_id,
            status=status,
            approved_for_training=approved,
            metadata={
                "reviewed_by_user_id": actor_user_id,
                "review_reason": reason,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def list_promotion_candidates(
        self,
        *,
        organization_id: str | None,
        workspace_slug: str | None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        items = self.memory.list_structured_memory_items(
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            limit=limit,
        )
        return [
            item
            for item in items
            if item.get("promotion_state") in {"candidate", "none"} and item.get("memory_kind") != "episodic_turn"
        ]

    def get_promotion_candidate(self, memory_item_id: str) -> dict[str, object] | None:
        item = self.memory.get_structured_memory_item(memory_item_id)
        if item is None:
            return None
        conflicting = [
            candidate
            for candidate in self.memory.list_structured_memory_items(
                organization_id=item.get("organization_id"),
                workspace_slug=item.get("workspace_slug"),
                user_id=item.get("user_id"),
                limit=200,
            )
            if candidate.get("memory_kind") == "fact"
            and str(candidate.get("key") or "").strip().lower() == str(item.get("key") or "").strip().lower()
            and str(candidate.get("value") or "").strip() != str(item.get("value") or "").strip()
        ]
        item["provenance"] = {
            "scope": "user_private" if item.get("user_id") else "workspace" if item.get("workspace_slug") else "organization_shared",
            "workspace_slug": item.get("workspace_slug"),
            "user_id": item.get("user_id"),
            "confidence": item.get("confidence"),
            "policy_safe": item.get("data_class") != "personal",
        }
        item["ranking_explanation"] = {
            "recency": item.get("updated_at"),
            "prior_approvals": item.get("approved_count", 0),
            "scope_match": "exact" if item.get("user_id") else "workspace" if item.get("workspace_slug") else "organization",
        }
        if conflicting:
            item["blocking_reasons"] = ["Conflicting fact values require confirmation before promotion."]
            item["conflicting_values"] = [candidate.get("value") for candidate in conflicting]
        return item

    def review_promotion_candidate(
        self,
        memory_item_id: str,
        *,
        actor_user_id: str | None,
        decision: str,
        reason: str = "",
    ) -> dict[str, object] | None:
        signal_type = {
            "approved": "promote_workspace",
            "rejected": "reject_pattern",
            "personal_only": "personal_only",
        }.get(decision, "use_again")
        item = self.memory.get_structured_memory_item(memory_item_id)
        if item is None:
            return None
        self.capture_feedback(
            actor_user_id=actor_user_id,
            organization_id=item.get("organization_id"),
            workspace_slug=item.get("workspace_slug"),
            signal_type=signal_type,
            source_type="memory",
            source_id=str(item.get("key") or memory_item_id),
            memory_item_id=memory_item_id,
            metadata={"decision": decision, "reason": reason},
            approved_for_training=decision == "approved",
        )
        return self.get_promotion_candidate(memory_item_id)

    def export_training_dataset(
        self,
        *,
        actor_user_id: str | None,
        workspace_slug: str | None,
    ) -> dict[str, object]:
        examples = self.memory.list_training_examples(workspace_slug=workspace_slug, approved_only=True, limit=1000)
        filtered: list[TrainingExampleRecord] = []
        for example in examples:
            data_class = str(example.metadata.get("data_class") or "operational")
            if data_class == "personal":
                continue
            if example.metadata.get("legal_hold"):
                continue
            filtered.append(example)
        deduped: dict[str, TrainingExampleRecord] = {}
        for example in filtered:
            key = hashlib.sha256(f"{example.input_text}|{example.output_text}".encode("utf-8")).hexdigest()
            deduped[key] = example
        ordered = sorted(deduped.values(), key=lambda item: (item.updated_at, item.id))
        split_index = max(1, int(len(ordered) * 0.8)) if ordered else 0
        train = ordered[:split_index]
        validation = ordered[split_index:]
        created_at = datetime.now(timezone.utc)
        export_id = f"training-{created_at.strftime('%Y%m%d%H%M%S')}"
        dataset_dir = self.training_root / export_id
        dataset_dir.mkdir(parents=True, exist_ok=True)
        train_path = dataset_dir / "train.jsonl"
        validation_path = dataset_dir / "validation.jsonl"
        manifest_path = dataset_dir / "manifest.json"
        self._write_jsonl(train_path, train)
        self._write_jsonl(validation_path, validation)
        manifest = {
            "id": export_id,
            "created_at": created_at.isoformat(),
            "generated_by_user_id": actor_user_id,
            "workspace_slug": workspace_slug,
            "train_count": len(train),
            "validation_count": len(validation),
            "dedup_count": len(filtered) - len(ordered),
            "compliance_filter_report": {
                "excluded_personal": len([example for example in examples if str(example.metadata.get("data_class") or "operational") == "personal"]),
                "excluded_legal_hold": len([example for example in examples if example.metadata.get("legal_hold")]),
            },
            "model_card_notes": "Approved examples only. No live runtime fine-tuning.",
            "artifacts": [str(train_path), str(validation_path)],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return {
            "dataset": manifest,
            "examples": [example.model_dump(mode="json") for example in ordered],
        }

    def _write_jsonl(self, path: Path, examples: list[TrainingExampleRecord]) -> None:
        lines = [
            json.dumps(
                {
                    "input": example.input_text,
                    "output": example.output_text,
                    "metadata": example.metadata,
                    "source_id": example.source_id,
                },
                sort_keys=True,
            )
            for example in examples
        ]
        path.write_text("\n".join(lines), encoding="utf-8")

    def _tokenize(self, text: str) -> list[str]:
        return [token.lower() for token in self.TOKEN_PATTERN.findall(text or "")]
