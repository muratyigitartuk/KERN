from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.memory import MemoryRepository
from app.platform import PlatformStore
from app.types import (
    DataExportRecord,
    ErasureExecutionStep,
    EvidenceManifest,
    ProfileSummary,
    RegulatedDocumentRecord,
    RegulatedDocumentVersion,
)


class ComplianceService:
    def __init__(self, platform: PlatformStore, memory: MemoryRepository, profile: ProfileSummary) -> None:
        self.platform = platform
        self.memory = memory
        self.profile = profile
        self.export_root = Path(self.profile.profile_root) / "compliance"
        self.export_root.mkdir(parents=True, exist_ok=True)

    def data_inventory_map(self) -> dict[str, dict[str, Any]]:
        return {
            "users": {"exportable": True, "erasable": False, "retention_bound": True, "legal_hold_blocked": True, "pseudonymize_only": True, "organization_owned": True},
            "workspace_memberships": {"exportable": True, "erasable": True, "retention_bound": False, "legal_hold_blocked": False, "pseudonymize_only": False, "organization_owned": True},
            "sessions": {"exportable": True, "erasable": True, "retention_bound": False, "legal_hold_blocked": False, "pseudonymize_only": False, "organization_owned": True},
            "documents": {"exportable": True, "erasable": True, "retention_bound": True, "legal_hold_blocked": True, "pseudonymize_only": False, "organization_owned": True},
            "deprecated_legacy_email_data": {"exportable": True, "erasable": True, "retention_bound": True, "legal_hold_blocked": True, "pseudonymize_only": False, "organization_owned": True, "deprecated": True, "active_product_surface": False},
            "deprecated_legacy_meeting_data": {"exportable": True, "erasable": True, "retention_bound": True, "legal_hold_blocked": True, "pseudonymize_only": False, "organization_owned": True, "deprecated": True, "active_product_surface": False},
            "schedules": {"exportable": True, "erasable": True, "retention_bound": False, "legal_hold_blocked": False, "pseudonymize_only": False, "organization_owned": True},
            "knowledge_graph": {"exportable": True, "erasable": True, "retention_bound": False, "legal_hold_blocked": False, "pseudonymize_only": False, "organization_owned": True},
            "structured_memory": {"exportable": True, "erasable": True, "retention_bound": False, "legal_hold_blocked": False, "pseudonymize_only": False, "organization_owned": False},
            "audit_events": {"exportable": True, "erasable": False, "retention_bound": True, "legal_hold_blocked": True, "pseudonymize_only": True, "organization_owned": True},
            "training_examples": {"exportable": True, "erasable": True, "retention_bound": False, "legal_hold_blocked": True, "pseudonymize_only": False, "organization_owned": False},
            "feedback_signals": {"exportable": True, "erasable": True, "retention_bound": False, "legal_hold_blocked": False, "pseudonymize_only": False, "organization_owned": False},
            "deletion_tombstones": {"exportable": True, "erasable": False, "retention_bound": True, "legal_hold_blocked": False, "pseudonymize_only": False, "organization_owned": True},
        }

    def export_user_bundle(
        self,
        *,
        actor_user_id: str | None,
        target_user_id: str,
        export_record: DataExportRecord | None = None,
    ) -> tuple[DataExportRecord, dict[str, Any]]:
        user = self.platform.get_user(target_user_id)
        if user is None:
            raise RuntimeError("User not found.")
        memberships = self.platform.list_workspace_memberships(target_user_id)
        sessions = self.platform.list_sessions(user.organization_id, target_user_id)
        holds = [item for item in self.platform.list_legal_holds(user.organization_id) if item.target_user_id in {None, target_user_id}]
        erasures = [item for item in self.platform.list_erasure_requests(user.organization_id) if item.target_user_id == target_user_id]
        exports = [item for item in self.platform.list_data_exports(user.organization_id) if item.target_user_id == target_user_id]
        tombstones = self.platform.list_deletion_tombstones(user.organization_id, target_user_id=target_user_id)
        memory_items = [item for item in self.memory.list_structured_memory_items(user_id=target_user_id, limit=500)]
        feedback = [item.model_dump(mode="json") for item in self.memory.list_feedback_signals(user_id=target_user_id, limit=500)]
        training_examples = [item.model_dump(mode="json") for item in self.memory.list_training_examples(user_id=target_user_id, limit=500)]
        workspace_exports = []
        for membership in memberships:
            workspace_exports.append(
                {
                    "workspace_slug": membership.workspace_slug,
                    "documents": [
                        record.model_dump(mode="json")
                        for record in self.memory.list_document_records(limit=200, include_archived=True)
                        if record.actor_user_id == target_user_id or record.provenance.get("actor_user_id") == target_user_id
                    ],
                }
            )
        payload = {
            "user": user.model_dump(mode="json"),
            "memberships": [membership.model_dump(mode="json") for membership in memberships],
            "sessions": [session.model_dump(mode="json") for session in sessions],
            "legal_holds": [hold.model_dump(mode="json") for hold in holds],
            "erasure_requests": [item.model_dump(mode="json") for item in erasures],
            "data_exports": [item.model_dump(mode="json") for item in exports],
            "workspace_exports": workspace_exports,
            "structured_memory": memory_items,
            "feedback_signals": feedback,
            "training_examples": training_examples,
            "deletion_tombstones": tombstones,
        }
        artifact_dir = self.export_root / "user-exports"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / f"{target_user_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json"
        artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        manifest = self._build_manifest(
            actor_user_id=actor_user_id,
            organization_id=user.organization_id,
            workspace_slug=None,
            included_datasets=list(payload.keys()),
            artifact_paths=[artifact_path],
        )
        export = export_record or self.platform.create_data_export(
            organization_id=user.organization_id,
            target_user_id=target_user_id,
            requested_by_user_id=actor_user_id,
            status="requested",
        )
        updated = self.platform.update_data_export(
            export.id,
            status="completed",
            artifact_path=str(artifact_path),
            approved_by_user_id=actor_user_id,
            manifest=manifest,
            artifact_refs=[str(artifact_path)],
        )
        if updated is None:
            raise RuntimeError("Failed to persist data export state.")
        return updated, payload

    def export_workspace_bundle(
        self,
        *,
        actor_user_id: str | None,
        workspace_slug: str,
        export_record: DataExportRecord | None = None,
    ) -> tuple[DataExportRecord, dict[str, Any]]:
        profile = self.platform.get_profile(workspace_slug)
        if profile is None:
            raise RuntimeError("Workspace not found.")
        payload = {
            "workspace": profile.model_dump(mode="json"),
            "documents": [item.model_dump(mode="json") for item in self.memory.list_document_records(limit=500, include_archived=True)],
            "business_documents": [item.model_dump(mode="json") for item in self.memory.list_business_documents(limit=200)],
            "regulated_documents": [item.model_dump(mode="json") for item in self.memory.list_regulated_documents(limit=200)],
            "regulated_versions": [
                version.model_dump(mode="json")
                for regulated in self.memory.list_regulated_documents(limit=200)
                for version in self.memory.list_regulated_document_versions(regulated.id)
            ],
            "deprecated_legacy_email_data": self._legacy_rows(["email_drafts", "mailbox_messages", "email_accounts"], limit=200),
            "deprecated_legacy_meeting_data": self._legacy_rows(["meeting_records", "transcript_artifacts", "meeting_action_items"], limit=200),
            "structured_memory": self.memory.list_structured_memory_items(limit=500),
            "feedback_signals": [item.model_dump(mode="json") for item in self.memory.list_feedback_signals(limit=500)],
            "training_examples": [item.model_dump(mode="json") for item in self.memory.list_training_examples(limit=500)],
            "audit_events": [item.model_dump(mode="json") for item in self.platform.list_audit_events(workspace_slug, 200)],
            "background_jobs": [item.model_dump(mode="json") for item in self.platform.list_jobs(workspace_slug, 200)],
            "retention_policies": [item.model_dump(mode="json") for item in self.platform.list_retention_policies(profile.organization_id or self.platform.ensure_default_organization().id)],
            "legal_holds": [item.model_dump(mode="json") for item in self.platform.list_legal_holds(profile.organization_id or self.platform.ensure_default_organization().id)],
            "deletion_tombstones": self.platform.list_deletion_tombstones(profile.organization_id or self.platform.ensure_default_organization().id, workspace_slug=workspace_slug),
        }
        artifact_dir = self.export_root / "workspace-exports"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / f"{workspace_slug}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json"
        artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        manifest = self._build_manifest(
            actor_user_id=actor_user_id,
            organization_id=profile.organization_id,
            workspace_slug=workspace_slug,
            included_datasets=list(payload.keys()),
            artifact_paths=[artifact_path],
        )
        export = export_record or self.platform.create_data_export(
            organization_id=profile.organization_id or self.platform.ensure_default_organization().id,
            workspace_slug=workspace_slug,
            requested_by_user_id=actor_user_id,
            status="requested",
        )
        updated = self.platform.update_data_export(
            export.id,
            status="completed",
            artifact_path=str(artifact_path),
            approved_by_user_id=actor_user_id,
            manifest=manifest,
            artifact_refs=[str(artifact_path)],
        )
        if updated is None:
            raise RuntimeError("Failed to persist workspace export state.")
        return updated, payload

    def _legacy_rows(self, table_names: list[str], *, limit: int) -> dict[str, list[dict[str, Any]]]:
        rows: dict[str, list[dict[str, Any]]] = {}
        for table_name in table_names:
            exists = self.memory.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            if not exists:
                continue
            result = self.memory.connection.execute(
                f"SELECT * FROM {table_name} WHERE profile_slug = ? LIMIT ?",
                (self.profile.slug, limit),
            )
            rows[table_name] = [dict(row) for row in result.fetchall()]
        return rows

    def execute_erasure(self, request_id: str, *, actor_user_id: str | None) -> dict[str, Any]:
        request = self.platform.get_erasure_request(request_id)
        if request is None:
            raise RuntimeError("Erasure request not found.")
        user = self.platform.get_user(request.target_user_id)
        if user is None:
            raise RuntimeError("Target user not found.")
        active_holds = [
            hold
            for hold in self.platform.list_legal_holds(request.organization_id, active_only=True)
            if hold.target_user_id in {None, request.target_user_id} and hold.workspace_slug in {None, request.workspace_slug}
        ]
        if active_holds:
            blocked_step = ErasureExecutionStep(name="legal_hold_check", status="blocked", detail="Active legal hold prevents erasure.")
            self.platform.update_erasure_request(
                request.id,
                status="blocked",
                approved_by_user_id=actor_user_id,
                legal_hold_decision="blocked_by_active_hold",
                retention_decision="blocked_by_legal_hold",
                steps=[blocked_step],
            )
            return {"status": "blocked", "steps": [blocked_step.model_dump(mode="json")]}

        steps = list(request.steps)
        step_map = {step.name: step for step in steps}

        def _mark_step(name: str, status: str, detail: str) -> None:
            step_map[name] = ErasureExecutionStep(name=name, status=status, detail=detail, updated_at=datetime.now(timezone.utc))
            ordered = list(step_map.values())
            self.platform.update_erasure_request(
                request.id,
                approved_by_user_id=actor_user_id,
                retention_decision="pseudonymize",
                legal_hold_decision="clear",
                steps=ordered,
            )

        _mark_step("legal_hold_check", "completed", "No active legal holds.")
        self.platform.revoke_user_sessions(request.target_user_id)
        self.platform.connection.execute("DELETE FROM workspace_memberships WHERE user_id = ?", (request.target_user_id,))
        self.memory.connection.execute("DELETE FROM memory_feedback_signals WHERE profile_slug = ? AND user_id = ?", (self.profile.slug, request.target_user_id))
        self.memory.connection.execute("DELETE FROM training_examples WHERE profile_slug = ? AND user_id = ?", (self.profile.slug, request.target_user_id))
        self.memory.connection.execute("DELETE FROM structured_memory_items WHERE profile_slug = ? AND user_id = ?", (self.profile.slug, request.target_user_id))
        self.memory.connection.commit()
        _mark_step("delete_user_private_state", "completed", "Removed memberships, sessions, user-private memory, and training records.")

        pseudonym = f"deleted-{request.target_user_id[:8]}@redacted.local"
        deleted_at = datetime.now(timezone.utc).isoformat()
        self.platform.connection.execute(
            """
            UPDATE users
            SET email = ?, display_name = 'Deleted User', status = 'deleted', oidc_subject = NULL, deleted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (pseudonym, deleted_at, deleted_at, request.target_user_id),
        )
        self.platform.connection.execute(
            """
            UPDATE audit_events
            SET details_json = REPLACE(details_json, ?, ?),
                message = REPLACE(message, ?, 'Deleted User')
            WHERE details_json LIKE '%' || ? || '%' OR message LIKE '%' || ? || '%'
            """,
            (user.email, pseudonym, user.display_name, request.target_user_id, user.display_name),
        )
        self.platform.connection.commit()
        _mark_step("pseudonymize_subject", "completed", "Pseudonymized immutable user references.")

        tombstone_refs = [
            self.platform.record_deletion_tombstone(
                organization_id=request.organization_id,
                workspace_slug=request.workspace_slug,
                target_user_id=request.target_user_id,
                artifact_class="user",
                reference_id=request.target_user_id,
                metadata={"pseudonym": pseudonym},
            ),
            self.platform.record_deletion_tombstone(
                organization_id=request.organization_id,
                workspace_slug=request.workspace_slug,
                target_user_id=request.target_user_id,
                artifact_class="training_examples",
                metadata={"profile_slug": self.profile.slug},
            ),
            self.platform.record_deletion_tombstone(
                organization_id=request.organization_id,
                workspace_slug=request.workspace_slug,
                target_user_id=request.target_user_id,
                artifact_class="structured_memory",
                metadata={"profile_slug": self.profile.slug},
            ),
        ]
        _mark_step("record_tombstones", "completed", "Recorded restore-time deletion tombstones.")
        updated = self.platform.update_erasure_request(
            request.id,
            status="completed",
            approved_by_user_id=actor_user_id,
            retention_decision="pseudonymize",
            legal_hold_decision="clear",
            steps=list(step_map.values()),
            artifact_refs=tombstone_refs,
        )
        if updated is None:
            raise RuntimeError("Failed to persist erasure completion.")
        return {"status": "completed", "erasure_request": updated.model_dump(mode="json"), "artifact_refs": tombstone_refs}

    def finalize_regulated_document(
        self,
        *,
        actor_user_id: str | None,
        title: str | None = None,
        document_id: str | None = None,
        business_document_id: str | None = None,
        retention_state: str = "retention_locked",
    ) -> RegulatedDocumentRecord:
        existing = self.memory.find_regulated_document(document_id=document_id, business_document_id=business_document_id)
        current_versions = self.memory.list_regulated_document_versions(existing.id) if existing else []
        source_title = title or "Regulated document"
        content_digest = self._source_digest(document_id=document_id, business_document_id=business_document_id)
        next_version = (existing.current_version_number if existing else 0) + 1
        supersedes = current_versions[-1].id if current_versions else None
        version = RegulatedDocumentVersion(
            id=f"regver-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            regulated_document_id=existing.id if existing else f"regulated-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            version_number=next_version,
            supersedes_version_id=supersedes,
            document_id=document_id,
            business_document_id=business_document_id,
            content_digest=content_digest,
            version_chain_digest=self._version_chain_digest(supersedes, content_digest),
            metadata={"actor_user_id": actor_user_id},
        )
        record = RegulatedDocumentRecord(
            id=version.regulated_document_id,
            profile_slug=self.profile.slug,
            workspace_slug=self.profile.slug,
            organization_id=self.profile.organization_id,
            title=source_title,
            document_id=document_id,
            business_document_id=business_document_id,
            current_version_id=version.id,
            current_version_number=version.version_number,
            immutability_state="finalized",
            retention_state=retention_state,
            finalized_at=datetime.now(timezone.utc),
            finalized_by_user_id=actor_user_id,
            metadata={
                "content_digest": version.content_digest,
                "version_chain_digest": version.version_chain_digest,
                "governance_class": "regulated_business",
            },
            created_at=existing.created_at if existing else datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.memory.upsert_regulated_document(record)
        self.memory.append_regulated_document_version(version)
        return record

    def _source_digest(self, *, document_id: str | None, business_document_id: str | None) -> str:
        if document_id:
            details = self.memory.get_document_details([document_id]).get(document_id, {})
            text = json.dumps(details, sort_keys=True)
        elif business_document_id:
            match = next((item for item in self.memory.list_business_documents(limit=200) if item.id == business_document_id), None)
            text = json.dumps(match.model_dump(mode="json"), sort_keys=True) if match else business_document_id
        else:
            text = "regulated-document"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _version_chain_digest(self, supersedes_version_id: str | None, content_digest: str) -> str:
        return hashlib.sha256(f"{supersedes_version_id or 'root'}|{content_digest}".encode("utf-8")).hexdigest()

    def _build_manifest(
        self,
        *,
        actor_user_id: str | None,
        organization_id: str | None,
        workspace_slug: str | None,
        included_datasets: list[str],
        artifact_paths: list[Path],
    ) -> EvidenceManifest:
        digests: list[dict[str, str]] = []
        for artifact_path in artifact_paths:
            digests.append(
                {
                    "path": str(artifact_path),
                    "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                }
            )
        return EvidenceManifest(
            generator_actor_id=actor_user_id,
            organization_id=organization_id,
            workspace_slug=workspace_slug,
            included_datasets=included_datasets,
            excluded_datasets=[],
            digests=digests,
        )
