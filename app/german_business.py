from __future__ import annotations

import json
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import Connection
from uuid import uuid4

logger = logging.getLogger(__name__)

from app.artifacts import ArtifactStore
from app.documents import DocumentService
from app.local_data import LocalDataService
from app.memory import MemoryRepository
from app.platform import PlatformStore
from app.retrieval import RetrievalService
from app.types import BehoerdeDraft, ComplianceReminderRule, GermanBusinessDocument, InvoiceDraft, OfferDraft, ProfileSummary, TaxSupportQuery, TaxSupportResult


GERMAN_BUSINESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS german_business_documents (
    id TEXT PRIMARY KEY,
    profile_slug TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    file_path TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_german_business_documents_created_at ON german_business_documents(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_german_business_documents_profile_slug ON german_business_documents(profile_slug, created_at DESC, id DESC);
"""


class GermanBusinessService:
    def __init__(
        self,
        connection: Connection | MemoryRepository,
        platform: PlatformStore | None,
        profile: ProfileSummary,
        local_data: LocalDataService,
        documents: DocumentService,
        retrieval: RetrievalService | None = None,
    ) -> None:
        self.connection = connection.connection if isinstance(connection, MemoryRepository) else connection
        self.platform = platform
        self.profile = profile
        self.local_data = local_data
        self.documents = documents
        self.retrieval = retrieval
        self.artifacts = ArtifactStore(platform, profile)
        self.connection.executescript(GERMAN_BUSINESS_SCHEMA)
        self.connection.commit()
        self._business_root = Path(self.profile.documents_root) / "german-business"
        self._business_root.mkdir(parents=True, exist_ok=True)
        self.memory = MemoryRepository(self.connection, profile_slug=profile.slug)
        self._ensure_default_compliance_rules()

    def availability(self) -> tuple[bool, str | None]:
        if self.platform and self.platform.is_profile_locked(self.profile.slug):
            return False, "Unlock the active profile to access German business workflows."
        documents_ready, documents_note = self.documents.availability()
        if not documents_ready:
            return False, f"Document stack unavailable: {documents_note}"
        return True, "Draft/support outputs only."

    def create_angebot(self, draft: OfferDraft, *, vat_exempt: bool = False) -> GermanBusinessDocument:
        self._ensure_unlocked("create_angebot")
        title = f"Angebot {draft.offer_number}"
        valid_until = draft.valid_until or (datetime.now() + timedelta(days=14))
        totals = self._compute_totals(draft.line_items, vat_exempt=vat_exempt)
        content_lines = [
            f"# {title}",
            "",
            f"Kunde: {draft.customer_name}",
            f"Gueltig bis: {valid_until.date().isoformat()}",
            "",
            "## Positionen",
            *[self._line_item_text(item) for item in draft.line_items],
            "",
            f"Netto: {totals['total_net']:.2f} EUR",
            f"USt: {totals['vat_amount']:.2f} EUR",
            f"Brutto: {totals['total_gross']:.2f} EUR",
        ]
        if vat_exempt:
            content_lines.append("")
            content_lines.append("GemÃ¤ÃŸ Â§19 UStG wird keine Umsatzsteuer berechnet.")
        content_lines.append("")
        content_lines.append("_Entwurf. Vor Versand pruefen._")
        content = "\n".join(content_lines)
        payload = draft.model_dump(mode="json")
        payload["template"] = "angebot_v1"
        payload["output_formats"] = ["md", "json"]
        payload["output_label"] = "draft"
        payload["totals"] = totals
        return self._write_business_document("angebot", title, "draft", content, payload)

    def create_rechnung(self, draft: InvoiceDraft, *, vat_exempt: bool = False) -> GermanBusinessDocument:
        self._ensure_unlocked("create_rechnung")
        title = f"Rechnung {draft.invoice_number}"
        totals = self._compute_totals(draft.line_items, vat_exempt=vat_exempt)
        content_lines = [
            f"# {title}",
            "",
            f"Kunde: {draft.customer_name}",
            f"Ausgestellt am: {draft.issue_date.date().isoformat()}",
            "",
            "## Positionen",
            *[self._line_item_text(item) for item in draft.line_items],
            "",
            f"Netto: {totals['total_net']:.2f} EUR",
            f"USt: {totals['vat_amount']:.2f} EUR",
            f"Brutto: {totals['total_gross']:.2f} EUR",
        ]
        if vat_exempt:
            content_lines.append("")
            content_lines.append("GemÃ¤ÃŸ Â§19 UStG wird keine Umsatzsteuer berechnet.")
        content_lines.append("")
        content_lines.append("_Entwurf. Buchhalterisch und steuerlich pruefen._")
        content = "\n".join(content_lines)
        payload = draft.model_dump(mode="json")
        payload["template"] = "rechnung_v1"
        payload["output_formats"] = ["md", "json"]
        payload["output_label"] = "draft"
        payload["totals"] = totals
        return self._write_business_document("rechnung", title, "draft", content, payload)

    def draft_behoerde_letter(self, subject: str, body_points: list[str]) -> GermanBusinessDocument:
        self._ensure_unlocked("draft_behoerde_letter")
        draft = BehoerdeDraft(subject=subject, body_points=body_points, category="general", tone_preset="formal")
        title = f"Behoerde - {subject}"
        body = "\n".join(
            [
                "Sehr geehrte Damen und Herren,",
                "",
                *[f"- {point}" for point in body_points],
                "",
                "Mit freundlichen Gruessen",
                self.profile.title,
                "",
                "_Formaler Entwurf. Vor Versand pruefen._",
            ]
        )
        return self._write_business_document(
            "behoerde",
            title,
            "draft",
            body,
            {
                **draft.model_dump(mode="json"),
                "template": "behoerde_standard",
                "review_label": "formal_draft",
                "output_formats": ["md", "json"],
                "output_label": "draft",
            },
        )

    def create_dsgvo_reminders(self) -> list[int]:
        self._ensure_unlocked("dsgvo_reminders")
        created_ids: list[int] = []
        for rule in self.list_compliance_rules():
            due_at = datetime.now() + timedelta(days=self._cadence_days(rule.cadence))
            created_ids.append(self.local_data.create_reminder(rule.title, due_at))
        if self.platform:
            self.platform.record_audit("german_business", "dsgvo_reminders", "success", "Created DSGVO reminder set.", profile_slug=self.profile.slug, details={"count": len(created_ids)})
        return created_ids

    def tax_support_result(self, query: TaxSupportQuery) -> TaxSupportResult:
        self._ensure_unlocked("tax_support")
        hits = self.retrieval.retrieve(query.question, scope="profile_plus_archive", limit=5) if self.retrieval else self.documents.search_documents(query.question, limit=5)
        evidence = "\n".join(
            f"- {hit.metadata.get('title', 'Dokument')} (Score {hit.score:.2f}): {hit.text[:140]}"
            for hit in hits
        ) if hits else "- Keine passenden Dokumente gefunden."
        answer = (
            f"Frage: {query.question}\n\n"
            "Relevante Unterlagen:\n"
            f"{evidence}\n\n"
            "Naechster Schritt: Kategorien und Belege manuell mit Steuerberatung pruefen."
        )
        if self.platform:
            self.platform.record_audit("german_business", "tax_support", "success", "Generated tax support summary.", profile_slug=self.profile.slug, details={"question": query.question, "hits": len(hits)})
        return TaxSupportResult(question=query.question, answer=answer, source_hits=hits)

    def tax_support(self, query: TaxSupportQuery) -> str:
        result = self.tax_support_result(query)
        return f"Steuer-Unterstuetzung ({result.disclaimer})\n\n{result.answer}"

    def create_offer(self, customer_name: str, offer_number: str, line_items: list[dict[str, object]]) -> GermanBusinessDocument:
        return self.create_angebot(OfferDraft(customer_name=customer_name, offer_number=offer_number, line_items=line_items))

    def create_invoice(self, customer_name: str, invoice_number: str, line_items: list[dict[str, object]]) -> GermanBusinessDocument:
        return self.create_rechnung(InvoiceDraft(customer_name=customer_name, invoice_number=invoice_number, line_items=line_items))

    def create_compliance_reminder(self, title: str, days_until_due: int = 30) -> int:
        self._ensure_unlocked("compliance_reminder")
        due_at = datetime.now() + timedelta(days=days_until_due)
        reminder_id = self.local_data.create_reminder(title, due_at)
        if self.platform:
            self.platform.record_audit("german_business", "compliance_reminder", "success", f"Created compliance reminder {title}.", profile_slug=self.profile.slug)
        return reminder_id

    def tax_support_query(self, question: str) -> str:
        return self.tax_support(TaxSupportQuery(question=question))

    def list_documents(self, limit: int = 12, *, audit: bool = True) -> list[GermanBusinessDocument]:
        self._ensure_unlocked("list_german_business_documents")
        documents = self.memory.list_business_documents(limit=limit)
        if audit and self.platform:
            self.platform.record_audit(
                "german_business",
                "list_documents",
                "success",
                f"Listed {len(documents)} German business document(s).",
                profile_slug=self.profile.slug,
                details={"limit": limit, "count": len(documents)},
            )
        return documents

    def list_compliance_rules(self, limit: int = 10, *, audit: bool = True) -> list[ComplianceReminderRule]:
        self._ensure_unlocked("list_compliance_rules")
        rules = self.memory.list_compliance_rules(limit=limit)
        if audit and self.platform:
            self.platform.record_audit(
                "german_business",
                "list_compliance_rules",
                "success",
                f"Listed {len(rules)} compliance rule(s).",
                profile_slug=self.profile.slug,
                details={"limit": limit, "count": len(rules)},
            )
        return rules

    def _line_item_text(self, item: dict[str, object]) -> str:
        label = item.get("title") or item.get("label") or "Leistung"
        amount = item.get("amount", "")
        return f"- {label}: {amount}"

    def _compute_totals(self, line_items: list[dict[str, object]], *, vat_exempt: bool = False) -> dict[str, float]:
        from decimal import Decimal, ROUND_HALF_UP
        total_net = Decimal("0")
        vat_rate = Decimal("0") if vat_exempt else Decimal("0.19")
        for item in line_items:
            try:
                total_net += Decimal(str(item.get("amount", 0) or 0))
            except Exception as exc:
                logger.debug("Skipping line item with invalid amount: %s", exc)
                continue
        vat_amount = (total_net * vat_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_gross = (total_net + vat_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_net = total_net.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        result = {
            "total_net": float(total_net),
            "vat_rate": float(vat_rate),
            "vat_amount": float(vat_amount),
            "total_gross": float(total_gross),
        }
        if vat_exempt:
            result["vat_exempt_note"] = "GemÃ¤ÃŸ Â§19 UStG wird keine Umsatzsteuer berechnet."
        return result

    def _ensure_default_compliance_rules(self) -> None:
        if self.memory.list_compliance_rules(limit=1):
            return
        self.memory.upsert_compliance_rule(
            ComplianceReminderRule(id="dsgvo-consent-review", title="DSGVO consent review", cadence="30d", details="Review consent records and notices.")
        )
        self.memory.upsert_compliance_rule(
            ComplianceReminderRule(id="retention-review", title="Retention period review", cadence="90d", details="Review document retention and deletion schedules.")
        )

    def _cadence_days(self, cadence: str) -> int:
        if cadence.endswith("d") and cadence[:-1].isdigit():
            return max(1, int(cadence[:-1]))
        return 30

    def recover_jobs(self) -> None:
        if not self.platform:
            return
        for job in self.platform.list_jobs(self.profile.slug, limit=20):
            if job.job_type != "german_business_generation" or not job.recoverable:
                continue
            payload: dict[str, object] = {}
            for checkpoint in self.platform.list_checkpoints(job.id):
                payload_row = self.platform.connection.execute(
                    """
                    SELECT payload_json
                    FROM recovery_checkpoints
                    WHERE job_id = ? AND stage = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (job.id, checkpoint.stage),
                ).fetchone()
                if payload_row:
                    try:
                        payload.update(json.loads(payload_row["payload_json"] or "{}"))
                    except Exception as exc:
                        logger.warning("Failed to parse checkpoint payload JSON: %s", exc)
            document_id = str(payload.get("document_id", "") or "")
            file_path = str(payload.get("file_path", "") or "")
            with contextlib.suppress(Exception):  # cleanup â€” best-effort
                if file_path:
                    Path(file_path).unlink(missing_ok=True)
            if document_id:
                self.connection.execute(
                    "DELETE FROM german_business_documents WHERE id = ? AND profile_slug = ?",
                    (document_id, self.profile.slug),
                )
                self._delete_indexed_document_by_path(file_path)
                self.connection.commit()
            self.platform.update_job(
                job.id,
                status="rolled_back",
                recoverable=False,
                checkpoint_stage="rolled_back",
                detail="Rolled back interrupted German business generation.",
                progress=1.0,
                result={"document_id": document_id or None, "file_path": file_path or None},
            )
            self.platform.record_audit(
                "german_business",
                "generation_recovery",
                "warning",
                "Rolled back interrupted German business generation.",
                profile_slug=self.profile.slug,
                details={"job_id": job.id, "document_id": document_id or None},
            )

    def _write_business_document(self, kind: str, title: str, status: str, content: str, metadata: dict[str, object]) -> GermanBusinessDocument:
        document_id = str(uuid4())
        target_path = self._business_root / f"{document_id}.md"
        metadata = dict(metadata)
        metadata.setdefault("regulated_candidate", kind in {"angebot", "rechnung", "behoerde", "tax_support"})
        metadata.setdefault("data_class", "regulated_business" if kind in {"angebot", "rechnung", "behoerde", "tax_support"} else "operational")
        metadata.setdefault("document_kind", kind)
        job = self.platform.create_job(
            "german_business_generation",
            f"Generate {title}",
            profile_slug=self.profile.slug,
            detail="Preparing business document.",
            payload={"document_id": document_id, "kind": kind, "title": title, "status": status},
        ) if self.platform else None
        if self.platform and job:
            self.platform.update_job(job.id, status="running", progress=0.1, checkpoint_stage="planned")
        document_artifacts = getattr(self.documents, "artifacts", None)
        now = datetime.now(timezone.utc).isoformat()
        try:
            if self.artifacts.enabled and getattr(document_artifacts, "enabled", False):
                target_path = self.artifacts.write_text(target_path, content)
            else:
                target_path.write_text(content, encoding="utf-8")
            if self.platform and job:
                self.platform.update_checkpoint(
                    job.id,
                    "artifact_written",
                    {"document_id": document_id, "file_path": str(target_path)},
                )
                self.platform.update_job(
                    job.id,
                    status="running",
                    progress=0.45,
                    checkpoint_stage="artifact_written",
                    detail="Persisting business document metadata.",
                )
            document = GermanBusinessDocument(
                id=document_id,
                profile_slug=self.profile.slug,
                kind=kind,
                title=title,
                status=status,
                file_path=str(target_path),
                metadata=metadata,
            )
            self.memory.upsert_business_document(document, metadata=metadata)
            if self.platform and job:
                self.platform.update_checkpoint(
                    job.id,
                    "record_saved",
                    {"document_id": document_id, "file_path": str(target_path)},
                )
                self.platform.update_job(
                    job.id,
                    status="running",
                    progress=0.7,
                    checkpoint_stage="record_saved",
                    detail="Indexing business document.",
                )
            self.documents.ingest_document(
                target_path,
                source="german_business",
                category=kind,
                tags=[kind, "german_business"],
            )
            if self.platform and job:
                self.platform.update_checkpoint(
                    job.id,
                    "indexed",
                    {"document_id": document_id, "file_path": str(target_path)},
                )
                self.platform.update_job(
                    job.id,
                    status="completed",
                    recoverable=False,
                    progress=1.0,
                    checkpoint_stage="indexed",
                    detail=f"Created {title}.",
                    result={"document_id": document_id, "file_path": str(target_path)},
                )
                self.platform.record_audit("german_business", kind, "success", f"Created {title}.", profile_slug=self.profile.slug)
            return document
        except Exception as exc:
            with contextlib.suppress(Exception):  # cleanup â€” best-effort
                if target_path.exists():
                    target_path.unlink()
            if self.platform:
                self.platform.record_audit(
                    "german_business",
                    kind,
                    "failure",
                    f"Failed to create {title}: {exc}",
                    profile_slug=self.profile.slug,
                    details={"path": str(target_path)},
                )
            self.connection.execute(
                "DELETE FROM german_business_documents WHERE id = ? AND profile_slug = ?",
                (document_id, self.profile.slug),
            )
            self._delete_indexed_document_by_path(str(target_path))
            self.connection.commit()
            if self.platform and job:
                self.platform.update_checkpoint(
                    job.id,
                    "rolled_back",
                    {"document_id": document_id, "file_path": str(target_path)},
                )
                self.platform.update_job(
                    job.id,
                    status="failed",
                    recoverable=False,
                    progress=0.0,
                    checkpoint_stage="rolled_back",
                    detail=str(exc),
                    error_code="german_business_generation_failed",
                    error_message=str(exc),
                    result={"document_id": document_id, "file_path": str(target_path)},
                )
            raise RuntimeError(f"Failed to create {title}: {exc}") from exc

    def _delete_indexed_document_by_path(self, file_path: str) -> None:
        if not file_path:
            return
        rows = self.connection.execute(
            "SELECT id FROM document_records WHERE profile_slug = ? AND file_path = ?",
            (self.profile.slug, file_path),
        ).fetchall()
        for row in rows:
            self.connection.execute("DELETE FROM document_chunks WHERE document_id = ?", (row["id"],))
            self.connection.execute("DELETE FROM document_records WHERE id = ? AND profile_slug = ?", (row["id"], self.profile.slug))

    @staticmethod
    def validate_steuernummer(number: str) -> tuple[bool, str]:
        """Validate a German Steuernummer format (e.g., 12/345/67890)."""
        import re
        cleaned = number.strip()
        if re.fullmatch(r"\d{2,3}/\d{3}/\d{4,5}", cleaned):
            return True, "Valid Steuernummer format."
        if re.fullmatch(r"\d{10,11}", cleaned):
            return True, "Valid Steuernummer format (without separators)."
        return False, "Invalid Steuernummer. Expected format: XX/XXX/XXXXX or 10-11 digits."

    @staticmethod
    def validate_ust_id(number: str) -> tuple[bool, str]:
        """Validate a German USt-IdNr format (e.g., DE123456789)."""
        import re
        cleaned = number.strip().upper()
        if re.fullmatch(r"DE\d{9}", cleaned):
            return True, "Valid USt-IdNr format."
        return False, "Invalid USt-IdNr. Expected format: DE followed by 9 digits."

    def create_gewerbeanmeldung(self, data: dict) -> GermanBusinessDocument:
        """Create a Gewerbeanmeldung (trade registration) document from template data."""
        now = datetime.now(timezone.utc)
        doc_id = str(uuid4())
        metadata = {
            "firmenname": data.get("firmenname", ""),
            "rechtsform": data.get("rechtsform", "Einzelunternehmen"),
            "geschaeftsfuehrer": data.get("geschaeftsfuehrer", ""),
            "anschrift": data.get("anschrift", ""),
            "gegenstand": data.get("gegenstand", ""),
            "beginn_datum": data.get("beginn_datum", now.strftime("%Y-%m-%d")),
            "handelsregister": data.get("handelsregister", ""),
            "steuernummer": data.get("steuernummer", ""),
            "ust_id": data.get("ust_id", ""),
            "kleinunternehmer": data.get("kleinunternehmer", False),
        }
        # Validate tax numbers if provided
        if metadata["steuernummer"]:
            valid, msg = self.validate_steuernummer(metadata["steuernummer"])
            if not valid:
                metadata["steuernummer_warning"] = msg
        if metadata["ust_id"]:
            valid, msg = self.validate_ust_id(metadata["ust_id"])
            if not valid:
                metadata["ust_id_warning"] = msg

        title = f"Gewerbeanmeldung - {metadata['firmenname'] or 'Unbenannt'}"
        content_lines = [
            "GEWERBEANMELDUNG",
            f"Firma: {metadata['firmenname']}",
            f"Rechtsform: {metadata['rechtsform']}",
            f"GeschÃ¤ftsfÃ¼hrer: {metadata['geschaeftsfuehrer']}",
            f"Anschrift: {metadata['anschrift']}",
            f"Gegenstand des Gewerbes: {metadata['gegenstand']}",
            f"Beginn der TÃ¤tigkeit: {metadata['beginn_datum']}",
        ]
        if metadata["handelsregister"]:
            content_lines.append(f"Handelsregister: {metadata['handelsregister']}")
        if metadata["steuernummer"]:
            content_lines.append(f"Steuernummer: {metadata['steuernummer']}")
        if metadata["ust_id"]:
            content_lines.append(f"USt-IdNr.: {metadata['ust_id']}")
        if metadata["kleinunternehmer"]:
            content_lines.append("Hinweis: Kleinunternehmerregelung gemÃ¤ÃŸ Â§19 UStG angewendet.")

        file_path = self._business_root / f"gewerbeanmeldung_{doc_id[:8]}.txt"
        file_path.write_text("\n".join(content_lines), encoding="utf-8")

        self.connection.execute(
            """
            INSERT INTO german_business_documents (id, profile_slug, kind, title, status, file_path, metadata_json, created_at, updated_at)
            VALUES (?, ?, 'gewerbeanmeldung', ?, 'draft', ?, ?, ?, ?)
            """,
            (doc_id, self.profile.slug, title, str(file_path), json.dumps(metadata), now.isoformat(), now.isoformat()),
        )
        self.connection.commit()
        return GermanBusinessDocument(id=doc_id, kind="gewerbeanmeldung", title=title, status="draft", file_path=str(file_path), metadata=metadata, created_at=now.isoformat(), updated_at=now.isoformat())

    def generate_data_inventory_report(self) -> dict:
        """Generate a DSGVO data inventory report listing personal data processed by KERN."""
        from app.config import settings
        inventory = {
            "report_date": datetime.now(timezone.utc).isoformat(),
            "system": "KERN AI Workspace",
            "data_controller": getattr(settings, "dpo_contact_name", "") or "Not configured",
            "dpo_email": getattr(settings, "dpo_contact_email", "") or "Not configured",
            "categories": [
                {
                    "category": "Konversationsdaten",
                    "description": "Chat-Nachrichten zwischen Benutzer und KI-Assistent",
                    "legal_basis": "Berechtigtes Interesse (Art. 6 Abs. 1 lit. f DSGVO)",
                    "retention_days": self.memory.CONVERSATION_RETENTION,
                    "storage": "Lokale SQLite-Datenbank (verschlÃ¼sselt)",
                    "recipients": "Keine â€” alle Daten bleiben lokal",
                },
                {
                    "category": "E-Mail-Nachrichten",
                    "description": "Synchronisierte E-Mails aus dem Posteingang",
                    "legal_basis": "Einwilligung / Berechtigtes Interesse",
                    "retention_days": settings.retention_email_days,
                    "storage": "Lokale SQLite-Datenbank (verschlÃ¼sselt)",
                    "recipients": "Keine â€” alle Daten bleiben lokal",
                },
                {
                    "category": "Dokumente",
                    "description": "Hochgeladene und indexierte Dokumente",
                    "legal_basis": "Einwilligung / VertragserfÃ¼llung",
                    "retention_days": settings.retention_documents_days,
                    "storage": "Lokale Dateien + SQLite-Index",
                    "recipients": "Keine â€” alle Daten bleiben lokal",
                },
                {
                    "category": "Wissensgraph-EntitÃ¤ten",
                    "description": "Extrahierte Personen, Firmen, Daten aus Dokumenten",
                    "legal_basis": "Berechtigtes Interesse",
                    "retention_days": settings.retention_documents_days,
                    "storage": "Lokale SQLite-Datenbank",
                    "recipients": "Keine â€” alle Daten bleiben lokal",
                },
                {
                    "category": "Audit-Protokoll",
                    "description": "Systemereignisse und Benutzeraktionen",
                    "legal_basis": "Berechtigtes Interesse / Rechenschaftspflicht",
                    "retention_days": settings.retention_audit_days,
                    "storage": "Lokale SQLite-Datenbank",
                    "recipients": "Keine â€” alle Daten bleiben lokal",
                },
            ],
            "technical_measures": [
                "Fernet-VerschlÃ¼sselung fÃ¼r Profildatenbanken",
                "Keine Cloud-Verbindungen â€” vollstÃ¤ndig lokale Verarbeitung",
                "NetzwerkÃ¼berwachung zur Erkennung unbeabsichtigter Verbindungen",
                "Zugriffskontrolle Ã¼ber Profil-PIN",
                "Automatische Aufbewahrungsfristen mit konfigurierbarer LÃ¶schung",
            ],
            "data_subject_rights": "Betroffene kÃ¶nnen Auskunft, Berichtigung und LÃ¶schung Ã¼ber die KERN-OberflÃ¤che oder per Antrag anfordern.",
        }
        return inventory

    def generate_dsgvo_request_template(self, request_type: str) -> str:
        """Generate a DSGVO data subject request letter template.

        request_type: 'auskunft' (access), 'loeschung' (deletion), or 'berichtigung' (correction).
        """
        from app.config import settings
        dpo_name = getattr(settings, "dpo_contact_name", "") or "[Name des Datenschutzbeauftragten]"
        dpo_email = getattr(settings, "dpo_contact_email", "") or "[E-Mail-Adresse]"
        date_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")

        templates = {
            "auskunft": (
                f"Betreff: Auskunftsersuchen gemÃ¤ÃŸ Art. 15 DSGVO\n\n"
                f"Datum: {date_str}\n\n"
                f"Sehr geehrte(r) {dpo_name},\n\n"
                f"hiermit bitte ich gemÃ¤ÃŸ Art. 15 DSGVO um Auskunft Ã¼ber die zu meiner Person "
                f"gespeicherten personenbezogenen Daten.\n\n"
                f"Bitte teilen Sie mir insbesondere mit:\n"
                f"- Welche personenbezogenen Daten Ã¼ber mich gespeichert sind\n"
                f"- Den Zweck der Datenverarbeitung\n"
                f"- Die EmpfÃ¤nger, an die meine Daten weitergegeben wurden\n"
                f"- Die geplante Speicherdauer\n\n"
                f"Bitte Ã¼bermitteln Sie mir die Auskunft schriftlich an meine Adresse oder "
                f"per E-Mail.\n\n"
                f"Mit freundlichen GrÃ¼ÃŸen,\n[Ihr Name]\n[Ihre Adresse]"
            ),
            "loeschung": (
                f"Betreff: Antrag auf LÃ¶schung gemÃ¤ÃŸ Art. 17 DSGVO\n\n"
                f"Datum: {date_str}\n\n"
                f"Sehr geehrte(r) {dpo_name},\n\n"
                f"hiermit fordere ich gemÃ¤ÃŸ Art. 17 DSGVO die unverzÃ¼gliche LÃ¶schung "
                f"aller zu meiner Person gespeicherten personenbezogenen Daten.\n\n"
                f"Bitte bestÃ¤tigen Sie die vollstÃ¤ndige LÃ¶schung schriftlich.\n\n"
                f"Mit freundlichen GrÃ¼ÃŸen,\n[Ihr Name]\n[Ihre Adresse]"
            ),
            "berichtigung": (
                f"Betreff: Antrag auf Berichtigung gemÃ¤ÃŸ Art. 16 DSGVO\n\n"
                f"Datum: {date_str}\n\n"
                f"Sehr geehrte(r) {dpo_name},\n\n"
                f"hiermit beantrage ich gemÃ¤ÃŸ Art. 16 DSGVO die Berichtigung folgender "
                f"unrichtiger personenbezogener Daten:\n\n"
                f"Falsche Angabe: [Bitte eintragen]\n"
                f"Richtige Angabe: [Bitte eintragen]\n\n"
                f"Bitte bestÃ¤tigen Sie die Berichtigung schriftlich.\n\n"
                f"Mit freundlichen GrÃ¼ÃŸen,\n[Ihr Name]\n[Ihre Adresse]"
            ),
        }
        return templates.get(request_type.lower(), f"Unbekannter Antragstyp: {request_type}. GÃ¼ltige Typen: auskunft, loeschung, berichtigung.")

    def create_tax_calendar_reminders(self, scheduler_service=None) -> list[dict]:
        """Create scheduled reminders for German tax deadlines.

        Returns the list of created schedule entries (or dicts if no scheduler_service).
        """
        tax_schedules = [
            {
                "title": "USt-Voranmeldung (monatlich)",
                "cron": "0 9 8 * *",
                "description": "Umsatzsteuervoranmeldung fÃ¤llig am 10. des Folgemonats",
            },
            {
                "title": "Lohnsteuer-Anmeldung",
                "cron": "0 9 8 * *",
                "description": "Lohnsteuer-Anmeldung fÃ¤llig am 10. des Folgemonats",
            },
            {
                "title": "KSt/ESt Vorauszahlung Q1",
                "cron": "0 9 5 3 *",
                "description": "KÃ¶rperschaftsteuer/Einkommensteuer Vorauszahlung fÃ¤llig am 10. MÃ¤rz",
            },
            {
                "title": "KSt/ESt Vorauszahlung Q2",
                "cron": "0 9 5 6 *",
                "description": "KÃ¶rperschaftsteuer/Einkommensteuer Vorauszahlung fÃ¤llig am 10. Juni",
            },
            {
                "title": "KSt/ESt Vorauszahlung Q3",
                "cron": "0 9 5 9 *",
                "description": "KÃ¶rperschaftsteuer/Einkommensteuer Vorauszahlung fÃ¤llig am 10. September",
            },
            {
                "title": "KSt/ESt Vorauszahlung Q4",
                "cron": "0 9 5 12 *",
                "description": "KÃ¶rperschaftsteuer/Einkommensteuer Vorauszahlung fÃ¤llig am 10. Dezember",
            },
            {
                "title": "Jahresabschluss-Erinnerung",
                "cron": "0 9 1 11 *",
                "description": "Jahresabschluss fÃ¼r das vergangene GeschÃ¤ftsjahr vorbereiten (Frist: 31.12.)",
            },
        ]
        created = []
        for schedule in tax_schedules:
            entry = {
                "title": schedule["title"],
                "cron_expression": schedule["cron"],
                "action_type": "custom_prompt",
                "action_payload": {"prompt": schedule["description"]},
            }
            if scheduler_service:
                try:
                    task = scheduler_service.create_task(
                        schedule["title"],
                        schedule["cron"],
                        "custom_prompt",
                        {"prompt": schedule["description"]},
                    )
                    created.append(task)
                except Exception as exc:
                    logger.debug("Failed to create tax schedule '%s': %s", schedule["title"], exc)
                    created.append(entry)
            else:
                created.append(entry)
        return created

    def _ensure_unlocked(self, action: str) -> None:
        if self.platform:
            self.platform.assert_profile_unlocked(self.profile.slug, "german_business", action)
