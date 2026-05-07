from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.types import ExecutionReceipt, ToolRequest, ToolResult

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class VerificationService:
    def verify(
        self,
        request: ToolRequest,
        result: ToolResult,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ExecutionReceipt:
        receipt = ExecutionReceipt(
            capability_name=request.tool_name,
            status=result.status,
            message=result.display_text,
            original_utterance=request.user_utterance,
            trigger_source=request.trigger_source,
            verification_source="tool" if result.status == "observed" else "none",
            evidence=list(result.evidence),
            side_effects=list(result.side_effects),
            suggested_follow_up=result.suggested_follow_up,
            data=dict(result.data),
        )

        tool = request.tool_name
        if tool == "open_app" and receipt.status != "failed":
            observed = self._verify_open_app(str(request.arguments.get("app", "")))
            if observed:
                receipt.status = "observed"
                receipt.verification_source = "process"
                receipt.evidence.append(f"Observed running process matching {request.arguments.get('app')}.")
            elif receipt.status == "observed":
                receipt.status = "attempted"
                receipt.verification_source = "none"
        elif tool == "create_reminder" and receipt.status != "failed":
            self._verify_reminder(receipt, request, result, connection)
        elif tool in ("create_schedule", "create_task") and receipt.status != "failed":
            self._verify_schedule(receipt, request, result, connection)
        elif tool == "write_file" and receipt.status != "failed":
            self._verify_file(receipt, request, result)
        return receipt

    # â”€â”€ create_reminder â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _verify_reminder(
        self,
        receipt: ExecutionReceipt,
        request: ToolRequest,
        result: ToolResult,
        connection: sqlite3.Connection | None,
    ) -> None:
        reminder_id = result.data.get("reminder_id") or result.data.get("id")
        title = str(request.arguments.get("title", "") or "")
        if not connection:
            return
        try:
            if reminder_id:
                row = connection.execute(
                    "SELECT id, title FROM local_reminders WHERE id = ?", (reminder_id,)
                ).fetchone()
            elif title:
                row = connection.execute(
                    "SELECT id, title FROM local_reminders WHERE title = ? ORDER BY created_at DESC LIMIT 1",
                    (title,),
                ).fetchone()
            else:
                return
            if row:
                receipt.status = "observed"
                receipt.verification_source = "database"
                receipt.evidence.append(f"Reminder '{row['title']}' confirmed in database.")
            else:
                receipt.evidence.append("Reminder not found in database after creation.")
        except Exception as exc:
            logger.debug("Reminder verification failed: %s", exc)

    # â”€â”€ create_schedule / create_task â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _verify_schedule(
        self,
        receipt: ExecutionReceipt,
        request: ToolRequest,
        result: ToolResult,
        connection: sqlite3.Connection | None,
    ) -> None:
        task_id = result.data.get("task_id") or result.data.get("id")
        if not task_id or not connection:
            return
        try:
            row = connection.execute(
                "SELECT id, title FROM scheduled_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row:
                receipt.status = "observed"
                receipt.verification_source = "database"
                receipt.evidence.append(f"Scheduled task '{row['title']}' confirmed in database.")
            else:
                receipt.evidence.append(f"Scheduled task '{task_id}' not found in database.")
        except Exception as exc:
            logger.debug("Schedule verification failed: %s", exc)

    # â”€â”€ write_file â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _verify_file(
        self,
        receipt: ExecutionReceipt,
        request: ToolRequest,
        result: ToolResult,
    ) -> None:
        file_path = str(request.arguments.get("path", "") or result.data.get("path", "") or "")
        if not file_path:
            return
        target = Path(file_path)
        if target.exists():
            receipt.status = "observed"
            receipt.verification_source = "filesystem"
            receipt.evidence.append(f"File exists at '{target}' ({target.stat().st_size} bytes).")
        else:
            receipt.evidence.append(f"File not found at '{target}' after write.")
