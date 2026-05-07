from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.local_data import LocalDataService

if TYPE_CHECKING:
    from app.llm_client import LlamaServerClient
    from app.orchestrator import KernOrchestrator

logger = logging.getLogger(__name__)

_ACTION_TO_TOOL: dict[str, str] = {
    "create_reminder": "create_reminder",
    "create_task": "create_task",
    "draft_letter": "draft_behoerde_letter",
}

_URGENT_TERMS = ("urgent", "asap", "deadline", "due", "fällig", "invoice", "rechnung", "contract", "payment")


class ActionPlanner:
    """Suggest and execute follow-up actions for proactive alerts."""

    def suggest_actions(self, alert: dict) -> list[dict]:
        alert_type = str(alert.get("type", "") or "")
        if alert_type == "inbox":
            return self._inbox_actions(alert)
        if alert_type == "calendar":
            return self._calendar_actions(alert)
        if alert_type == "document":
            return self._document_actions(alert)
        if alert_type == "file_watch":
            return self._file_watch_actions(alert)
        return []

    def rank_alerts(
        self,
        alerts: list[dict],
        local_data: LocalDataService,
        now: datetime | None = None,
    ) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        ranked_by_key: dict[str, dict] = {}
        for alert in alerts:
            enriched = self._enrich_alert(alert, local_data, now)
            key = str(enriched.get("alert_key", "") or "")
            if not key:
                continue
            existing = ranked_by_key.get(key)
            if existing is None or float(enriched.get("priority_score", 0.0)) >= float(existing.get("priority_score", 0.0)):
                ranked_by_key[key] = enriched
        ranked = sorted(
            ranked_by_key.values(),
            key=lambda item: (
                float(item.get("priority_score", 0.0)),
                str(item.get("generated_at", "")),
            ),
            reverse=True,
        )
        interruption_budget = 1 if local_data.assistant_mode() == "manual" else 2
        quiet_hours = local_data.quiet_hours_active(now)
        for alert in ranked:
            interruption_class = str(alert.get("interruption_class", "ambient") or "ambient")
            if quiet_hours:
                alert["interrupt_now"] = False
                continue
            if interruption_class == "interrupt_now" and interruption_budget > 0:
                alert["interrupt_now"] = True
                interruption_budget -= 1
            else:
                alert["interrupt_now"] = False
        return ranked[:20]

    def record_feedback(
        self,
        local_data: LocalDataService,
        alert: dict | None,
        outcome: str,
        action_type: str | None = None,
    ) -> None:
        if not alert:
            return
        alert_type = str(alert.get("type", "") or "")
        if not alert_type:
            return
        local_data.record_proactive_feedback(alert_type, outcome, action_type=action_type)

    async def execute_action(
        self,
        action_type: str,
        action_payload: dict[str, Any],
        orchestrator: "KernOrchestrator",
    ) -> dict[str, Any]:
        tool_name = _ACTION_TO_TOOL.get(action_type)
        if not tool_name:
            return {"success": False, "message": f"Unknown action type: {action_type}"}

        tool = orchestrator.tools.get(tool_name)
        if tool is None:
            return {"success": False, "message": f"Tool '{tool_name}' not available."}

        from app.types import ToolRequest

        normalized_payload = dict(action_payload or {})
        normalized_payload = self._validate_payload(action_type, normalized_payload)
        if action_type == "create_reminder" and "due_at" not in normalized_payload:
            minutes = int(normalized_payload.pop("minutes", 60) or 60)
            normalized_payload["due_at"] = (datetime.now(timezone.utc) + timedelta(minutes=max(1, minutes))).isoformat()
            normalized_payload.setdefault("title", "Follow up")
        request = ToolRequest(
            tool_name=tool_name,
            arguments=normalized_payload,
            user_utterance="",
            reason=f"Suggested action: {action_type}",
        )
        try:
            result = await tool.run(request)
            return {
                "success": result.success,
                "message": result.display_text or ("Action completed." if result.success else "Action failed."),
                "data": result.data,
            }
        except Exception as exc:
            logger.warning("ActionPlanner execute_action error: %s", exc)
            return {"success": False, "message": str(exc)}

    def _inbox_actions(self, alert: dict[str, Any]) -> list[dict]:
        sample = next(iter(alert.get("samples") or []), {})
        subject = str(sample.get("subject", "") or "your message")
        due_at = (datetime.now(timezone.utc) + timedelta(minutes=60)).replace(second=0, microsecond=0).isoformat()
        return [
            {
                "action_type": "create_reminder",
                "label": "Remind me later",
                "payload": self._with_source_context(alert, {
                    "title": f"Reply to {subject}",
                    "due_at": due_at,
                    "kind": "reminder",
                }),
            },
        ]

    def _calendar_actions(self, alert: dict[str, Any]) -> list[dict]:
        event_title = str(alert.get("event_title", "") or "upcoming event")
        starts_at_raw = str(alert.get("starts_at", "") or "").strip()
        starts_at = self._parse_datetime(starts_at_raw) or (datetime.now(timezone.utc) + timedelta(hours=1))
        reminder_due = max(datetime.now(timezone.utc) + timedelta(minutes=5), starts_at - timedelta(minutes=30))
        return [
            {
                "action_type": "create_reminder",
                "label": "Set reminder",
                "payload": self._with_source_context(alert, {
                    "title": f"Prepare for {event_title}",
                    "due_at": reminder_due.replace(second=0, microsecond=0).isoformat(),
                    "kind": "reminder",
                }),
            },
        ]

    def _document_actions(self, alert: dict[str, Any]) -> list[dict]:
        document = next(iter(alert.get("documents") or []), {})
        title = str(document.get("title", "") or "document")
        due_date = self._parse_datetime(str(document.get("due_date", "") or "").strip())
        due_hint = f" before {due_date.strftime('%Y-%m-%d')}" if due_date else ""
        due_at = (
            max(datetime.now(timezone.utc) + timedelta(hours=1), due_date - timedelta(days=2))
            if due_date is not None
            else datetime.now(timezone.utc) + timedelta(days=2)
        )
        return [
            {
                "action_type": "create_reminder",
                "label": "Track deadline",
                "payload": self._with_source_context(alert, {
                    "title": f"Review {title}",
                    "due_at": due_at.replace(second=0, microsecond=0).isoformat(),
                    "kind": "reminder",
                }),
            },
        ]

    def _file_watch_actions(self, alert: dict[str, Any]) -> list[dict]:
        title = str(alert.get("document_title", "") or alert.get("path", "") or "indexed file")
        due_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(second=0, microsecond=0).isoformat()
        return [
            {
                "action_type": "create_task",
                "label": "Review file",
                "payload": self._with_source_context(alert, {"title": f"Review indexed file: {title}"}),
            },
            {
                "action_type": "create_reminder",
                "label": "Remind me",
                "payload": self._with_source_context(alert, {"title": f"Check {title}", "due_at": due_at, "kind": "reminder"}),
            },
        ]

    def _validate_payload(self, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize and validate payload fields for the given action type."""
        normalized = dict(payload)
        if action_type == "create_reminder":
            normalized.setdefault("title", "Follow up")
            normalized.setdefault("kind", "reminder")
        elif action_type == "create_task":
            normalized.setdefault("title", "New task")
        elif action_type == "draft_letter":
            normalized.setdefault("recipient_name", "")
            normalized.setdefault("subject", "")
        return normalized

    def _with_source_context(self, alert: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(payload)
        if not enriched.get("source_alert_type"):
            enriched["source_alert_type"] = str(alert.get("type", "") or "")
        if not enriched.get("source_alert_key"):
            enriched["source_alert_key"] = str(alert.get("alert_key", "") or "")
        if not enriched.get("source_alert_title"):
            enriched["source_alert_title"] = str(alert.get("title", "") or "")
        if not enriched.get("source_alert_message"):
            enriched["source_alert_message"] = str(alert.get("message", "") or "")
        if not enriched.get("source_alert_evidence"):
            enriched["source_alert_evidence"] = list(alert.get("evidence") or [])
        return enriched

    def _parse_datetime(self, raw: str) -> datetime | None:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None

    def _enrich_alert(self, alert: dict[str, Any], local_data: LocalDataService, now: datetime) -> dict[str, Any]:
        enriched = dict(alert)
        enriched.setdefault("generated_at", now.isoformat())
        enriched.setdefault("suggested_actions", self.suggest_actions(enriched))
        score, confidence, reasons = self._score_alert(enriched, now)
        feedback = local_data.get_proactive_feedback(str(enriched.get("type", "") or ""))
        score += min(0.18, feedback["accepted"] * 0.05)
        score += min(0.08, feedback["executed_later"] * 0.03)
        score -= min(0.22, feedback["dismissed"] * 0.05)
        score -= min(0.08, feedback["ignored"] * 0.02)
        score = max(0.05, min(0.99, score))
        priority = "high" if score >= 0.78 else "normal" if score >= 0.52 else "low"
        interruption_class = "interrupt_now" if score >= 0.82 else "active" if score >= 0.6 else "ambient"
        enriched["alert_key"] = self._alert_key(enriched)
        enriched["priority_score"] = round(score, 3)
        enriched["confidence"] = round(max(0.05, min(0.99, confidence)), 3)
        enriched["priority"] = priority
        enriched["interruption_class"] = interruption_class
        enriched["reason"] = "; ".join(reasons[:3]) if reasons else "Local signal detected."
        enriched["suggested_actions"] = self._enrich_suggested_actions(enriched)
        return enriched

    def _score_alert(self, alert: dict[str, Any], now: datetime) -> tuple[float, float, list[str]]:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        alert_type = str(alert.get("type", "") or "")
        reasons: list[str] = []
        score = 0.2
        confidence = 0.55
        if alert_type == "inbox":
            count = int(alert.get("count", 0) or 0)
            subjects = " ".join(str(item.get("subject", "") or "") for item in (alert.get("samples") or []))
            urgent = any(term in subjects.lower() for term in _URGENT_TERMS)
            score = 0.46 + min(0.2, count * 0.07) + (0.24 if urgent else 0.0)
            confidence = 0.72 + (0.12 if urgent else 0.0)
            reasons.append(f"{count} unread message{'s' if count != 1 else ''}")
            if urgent:
                reasons.append("urgent language detected in subjects")
        elif alert_type == "calendar":
            starts_at = self._parse_datetime(str(alert.get("starts_at", "") or ""))
            importance = int(alert.get("importance", 0) or 0)
            hours = ((starts_at - now).total_seconds() / 3600) if starts_at else 8
            score = 0.42 + min(0.24, max(0.0, (6 - max(hours, 0)) * 0.05)) + min(0.16, importance * 0.05)
            confidence = 0.78
            reasons.append("upcoming calendar commitment")
            if starts_at:
                reasons.append(f"starts within {max(0, round(hours, 1))}h")
        elif alert_type == "document":
            documents = list(alert.get("documents") or [])
            soonest_days = 7.0
            for document in documents:
                due_at = self._parse_datetime(str(document.get("due_date", "") or ""))
                if due_at:
                    soonest_days = min(soonest_days, max(0.0, (due_at - now).total_seconds() / 86400))
            score = 0.5 + min(0.18, len(documents) * 0.05) + min(0.18, max(0.0, (7 - soonest_days) * 0.025))
            confidence = 0.81
            reasons.append("document deadline approaching")
            if documents:
                reasons.append(f"{len(documents)} document{'s' if len(documents) != 1 else ''} affected")
        elif alert_type == "file_watch":
            category = str(alert.get("category", "") or "").lower()
            score = 0.24 + (0.12 if category in {"finance", "legal", "contract", "invoice"} else 0.0)
            confidence = 0.61
            reasons.append("new local file was indexed")
            if category:
                reasons.append(f"category: {category}")
        else:
            reasons.append("local proactive signal")
        return score, confidence, reasons

    def _alert_key(self, alert: dict[str, Any]) -> str:
        alert_type = str(alert.get("type", "") or "alert")
        if alert_type == "inbox":
            sample_ids = ",".join(sorted(str(item.get("id", "") or "") for item in (alert.get("samples") or []) if item.get("id")))
            return f"inbox:{sample_ids or alert.get('message', '')}"
        if alert_type == "calendar":
            return f"calendar:{alert.get('event_title', '')}:{alert.get('starts_at', '')}"
        if alert_type == "document":
            doc_ids = ",".join(sorted(str(item.get('id', '') or '') for item in (alert.get("documents") or []) if item.get("id")))
            return f"document:{doc_ids or alert.get('message', '')}"
        if alert_type == "file_watch":
            return f"file_watch:{alert.get('document_id', '') or alert.get('path', '')}"
        return f"{alert_type}:{alert.get('title', '')}:{alert.get('message', '')}"

    def _enrich_suggested_actions(self, alert: dict[str, Any]) -> list[dict]:
        actions: list[dict] = []
        for action in list(alert.get("suggested_actions") or []):
            payload = self._with_source_context(alert, dict(action.get("payload") or {}))
            action_type = str(action.get("action_type", "") or "")
            enriched = dict(action)
            enriched["payload"] = payload
            enriched["confidence"] = round(min(0.97, max(0.4, float(alert.get("confidence", 0.6)) - 0.06)), 3)
            enriched["reason"] = str(alert.get("reason", "") or "Suggested from local alert.")
            enriched["evidence"] = list(alert.get("evidence") or [])[:2]
            actions.append(enriched)
        return actions

    # â”€â”€ Contextual payload generation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def build_contextual_payload(
        self,
        action_type: str,
        alert: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a rich payload from alert data using template extraction.

        Extracts names, dates, amounts, and document references from the
        alert's samples, documents, and evidence to produce a payload that
        goes beyond the generic template.
        """
        context = self._extract_context(alert)
        if action_type == "create_reminder":
            return self._contextual_reminder(alert, context)
        if action_type == "create_task":
            return self._contextual_task(alert, context)
        if action_type == "draft_letter":
            return self._contextual_letter(alert, context)
        return {}

    async def build_contextual_payload_llm(
        self,
        action_type: str,
        alert: dict[str, Any],
        llm: "LlamaServerClient",
    ) -> dict[str, Any]:
        """Use LLM to generate a contextual payload. Falls back to template."""
        if not llm.available:
            return self.build_contextual_payload(action_type, alert)

        context = self._extract_context(alert)
        prompt = self._build_llm_prompt(action_type, alert, context)
        try:
            response = await llm.chat([
                {"role": "system", "content": "You are a professional German business assistant. Generate concise, contextual content for the requested action. Respond in the same language as the alert content. Use formal address (Sie) in German."},
                {"role": "user", "content": prompt},
            ])
            content = str((response.get("choices") or [{}])[0].get("message", {}).get("content", ""))
            if content.strip():
                base = self.build_contextual_payload(action_type, alert)
                base["body"] = content.strip()
                base["generated_by"] = "llm"
                return base
        except Exception as exc:
            logger.warning("LLM payload generation failed, using template: %s", exc)

        return self.build_contextual_payload(action_type, alert)

    def _extract_context(self, alert: dict[str, Any]) -> dict[str, Any]:
        """Extract structured context from alert data."""
        ctx: dict[str, Any] = {
            "names": [],
            "dates": [],
            "amounts": [],
            "references": [],
            "subject": "",
            "sender": "",
        }

        # From legacy alert samples
        for sample in list(alert.get("samples") or []):
            sender = str(sample.get("sender", "") or "")
            if sender:
                name = sender.split("<", 1)[0].strip()
                if name and name not in ctx["names"]:
                    ctx["names"].append(name)
                ctx["sender"] = sender
            subject = str(sample.get("subject", "") or "")
            if subject:
                ctx["subject"] = subject
            body = str(sample.get("body_preview", "") or sample.get("body_text", "") or "")
            ctx["dates"].extend(self._extract_dates(body))
            ctx["amounts"].extend(self._extract_amounts(body))

        # From documents
        for doc in list(alert.get("documents") or []):
            title = str(doc.get("title", "") or "")
            if title:
                ctx["references"].append(title)
            due = str(doc.get("due_date", "") or "")
            if due:
                ctx["dates"].append(due)

        # From evidence
        for ev in list(alert.get("evidence") or []):
            text = str(ev) if isinstance(ev, str) else str(ev.get("text", "") or "")
            ctx["dates"].extend(self._extract_dates(text))
            ctx["amounts"].extend(self._extract_amounts(text))

        # From event title / starts_at
        event_title = str(alert.get("event_title", "") or "")
        if event_title:
            ctx["references"].append(event_title)
        starts_at = str(alert.get("starts_at", "") or "")
        if starts_at:
            ctx["dates"].append(starts_at)

        # Deduplicate
        ctx["dates"] = list(dict.fromkeys(ctx["dates"]))[:5]
        ctx["amounts"] = list(dict.fromkeys(ctx["amounts"]))[:5]
        ctx["names"] = list(dict.fromkeys(ctx["names"]))[:5]
        ctx["references"] = list(dict.fromkeys(ctx["references"]))[:5]
        return ctx

    def _extract_dates(self, text: str) -> list[str]:
        """Extract date strings from text."""
        patterns = [
            r"\d{4}-\d{2}-\d{2}",           # ISO
            r"\d{1,2}\.\d{1,2}\.\d{2,4}",   # German DD.MM.YYYY
            r"\d{1,2}/\d{1,2}/\d{2,4}",     # Slash
        ]
        dates: list[str] = []
        for pattern in patterns:
            dates.extend(re.findall(pattern, text))
        return dates

    def _extract_amounts(self, text: str) -> list[str]:
        """Extract monetary amounts from text."""
        patterns = [
            r"\d[\d.,]*\s*(?:EUR|â‚¬|CHF|USD|\$)",
            r"(?:EUR|â‚¬|CHF|USD|\$)\s*\d[\d.,]*",
        ]
        amounts: list[str] = []
        for pattern in patterns:
            amounts.extend(re.findall(pattern, text))
        return amounts

    def _contextual_reminder(self, alert: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        subject = ctx["subject"] or ctx["references"][0] if ctx["references"] else "Nachfassen"
        title_parts = [f"Nachfassen: {subject}"]
        if ctx["names"]:
            title_parts.append(f"({', '.join(ctx['names'][:2])})")
        if ctx["amounts"]:
            title_parts.append(f"[{ctx['amounts'][0]}]")

        due_at = None
        for d in ctx["dates"]:
            parsed = self._parse_datetime(d)
            if parsed and parsed > datetime.now(timezone.utc):
                due_at = parsed
                break
        if not due_at:
            due_at = datetime.now(timezone.utc) + timedelta(hours=2)

        return self._with_source_context(alert, {
            "title": " ".join(title_parts),
            "due_at": due_at.replace(second=0, microsecond=0).isoformat(),
            "kind": "reminder",
            "generated_by": "template",
        })

    def _contextual_task(self, alert: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        ref = ctx["references"][0] if ctx["references"] else str(alert.get("document_title", "") or "Datei")
        title = f"Prüfen: {ref}"
        if ctx["dates"]:
            title += f" (Frist: {ctx['dates'][0]})"
        return self._with_source_context(alert, {
            "title": title,
            "generated_by": "template",
        })

    def _contextual_letter(self, alert: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        name = ctx["names"][0] if ctx["names"] else ""
        subject = ctx["subject"] or (ctx["references"][0] if ctx["references"] else "")
        return self._with_source_context(alert, {
            "recipient_name": name,
            "subject": subject,
            "generated_by": "template",
        })

    def _build_llm_prompt(self, action_type: str, alert: dict[str, Any], ctx: dict[str, Any]) -> str:
        alert_type = str(alert.get("type", "") or "unknown")
        parts = [f"Alert type: {alert_type}"]
        if ctx["names"]:
            parts.append(f"People: {', '.join(ctx['names'])}")
        if ctx["references"]:
            parts.append(f"References: {', '.join(ctx['references'])}")
        if ctx["dates"]:
            parts.append(f"Dates: {', '.join(ctx['dates'])}")
        if ctx["amounts"]:
            parts.append(f"Amounts: {', '.join(ctx['amounts'])}")
        if ctx["subject"]:
            parts.append(f"Subject: {ctx['subject']}")
        message = str(alert.get("message", "") or "")
        if message:
            parts.append(f"Alert message: {message}")

        context_block = "\n".join(parts)
        if action_type == "create_reminder":
            return f"Write a concise reminder title (max 80 chars) in German based on this context:\n{context_block}"
        if action_type == "draft_letter":
            return f"Write a formal German business letter body based on this context:\n{context_block}\n\nUse Behörde style. Do not include address header or sign-off."
        return f"Summarize this alert context in one sentence for a task title:\n{context_block}"
