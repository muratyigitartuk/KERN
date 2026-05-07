from __future__ import annotations

from datetime import datetime

from app.german_business import GermanBusinessService
from app.tools.base import Tool
from app.types import InvoiceDraft, OfferDraft, TaxSupportQuery, ToolRequest, ToolResult


class CreateAngebotTool(Tool):
    name = "create_angebot"

    def __init__(self, service: GermanBusinessService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    async def run(self, request: ToolRequest) -> ToolResult:
        draft = OfferDraft(
            customer_name=str(request.arguments.get("customer_name", "Kunde")),
            offer_number=str(request.arguments.get("offer_number", datetime.now().strftime("ANG-%Y%m%d-%H%M"))),
            line_items=list(request.arguments.get("line_items", [])),
        )
        document = self.service.create_angebot(draft)
        return ToolResult(
            status="observed",
            display_text=f"Created {document.title}.",
            side_effects=["angebot_created"],
            data={"document": document.model_dump(mode="json"), "output_label": "draft"},
        )


class CreateRechnungTool(Tool):
    name = "create_rechnung"

    def __init__(self, service: GermanBusinessService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    async def run(self, request: ToolRequest) -> ToolResult:
        draft = InvoiceDraft(
            customer_name=str(request.arguments.get("customer_name", "Kunde")),
            invoice_number=str(request.arguments.get("invoice_number", datetime.now().strftime("RE-%Y%m%d-%H%M"))),
            issue_date=datetime.now(),
            line_items=list(request.arguments.get("line_items", [])),
        )
        document = self.service.create_rechnung(draft)
        return ToolResult(
            status="observed",
            display_text=f"Created {document.title}.",
            side_effects=["invoice_created"],
            data={"document": document.model_dump(mode="json"), "output_label": "draft"},
        )


class DraftBehoerdeLetterTool(Tool):
    name = "draft_behoerde_letter"

    def __init__(self, service: GermanBusinessService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    async def run(self, request: ToolRequest) -> ToolResult:
        subject = str(request.arguments.get("subject", "Anliegen"))
        body_points = list(request.arguments.get("body_points", [])) or [str(request.arguments.get("body", "Sachverhalt ergaenzen"))]
        document = self.service.draft_behoerde_letter(subject, body_points)
        return ToolResult(
            status="observed",
            display_text=f"Created formal draft '{document.title}'.",
            side_effects=["behoerde_draft_created"],
            data={"document": document.model_dump(mode="json"), "output_label": "draft"},
        )


class CreateDsgvoReminderTool(Tool):
    name = "create_dsgvo_reminders"

    def __init__(self, service: GermanBusinessService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    async def run(self, request: ToolRequest) -> ToolResult:
        reminder_ids = self.service.create_dsgvo_reminders()
        return ToolResult(
            status="observed",
            display_text=f"Created {len(reminder_ids)} DSGVO reminders.",
            side_effects=["dsgvo_reminders_created"],
            data={"reminder_ids": reminder_ids, "output_label": "reminder"},
        )


class TaxSupportTool(Tool):
    name = "tax_support_query"

    def __init__(self, service: GermanBusinessService) -> None:
        self.service = service

    def availability(self) -> tuple[bool, str | None]:
        return self.service.availability()

    async def run(self, request: ToolRequest) -> ToolResult:
        question = str(request.arguments.get("question", "")).strip()
        if not question:
            return ToolResult(status="failed", display_text="I need a tax support question.")
        result = self.service.tax_support_result(TaxSupportQuery(question=question))
        answer = self.service.tax_support(TaxSupportQuery(question=question))
        return ToolResult(
            status="observed",
            display_text=answer,
            data={
                "answer": answer,
                "output_label": "support",
                "sources": [hit.model_dump(mode="json") for hit in result.source_hits],
                "disclaimer": result.disclaimer,
            },
        )
