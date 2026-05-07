from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4


logger = logging.getLogger(__name__)

_WORD = r"[^\W\d_][^\W\d_.-]*"
_PERSON_PATTERN = re.compile(rf"\b({_WORD} {_WORD})\b", re.UNICODE)
_PERSON_3WORD_PATTERN = re.compile(rf"\b({_WORD} {_WORD} {_WORD})\b", re.UNICODE)
_COMPANY_PATTERN = re.compile(rf"\b({_WORD}(?: {_WORD})* (?:GmbH|AG|Ltd|Inc|Corp|LLC|UG|SE|KG|eV|e\.V\.|OHG|KGaA|mbH|GbR))(?:\b|\s|$|[,;.])", re.UNICODE)
_DATE_PATTERN = re.compile(r"\b(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{4}-\d{2}-\d{2})\b")
_AMOUNT_PATTERN = re.compile(r"\b(\d[\d.,]*\s*(?:EUR|USD|GBP|CHF|€|\$|£|Fr\.))(?:\b|\s|$|[,;.])")
_LOCATION_PATTERN = re.compile(rf"\b(\d{{5}})\s+({_WORD}(?:\s{_WORD}){{0,2}})\b", re.UNICODE)
_GERMAN_ADDRESS_PATTERN = re.compile(rf"\b({_WORD}(?:straße|strasse|str\.|weg|platz|allee|gasse|ring|damm|ufer)\s+\d+\w?)\b", re.IGNORECASE | re.UNICODE)

_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+|\n+")
_DUE_CUES = ("due", "deadline", "pay by", "fällig", "bis", "payment")
_INVOICE_CUES = ("invoice", "rechnung", "billing")
_CONTRACT_CUES = ("contract", "agreement", "vertrag")
_OFFER_CUES = ("angebot", "offer", "proposal")
_MEETING_CUES = ("meeting", "call", "sync", "review")

_ENTITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("company", _COMPANY_PATTERN),
    ("person", _PERSON_3WORD_PATTERN),
    ("person", _PERSON_PATTERN),
    ("date", _DATE_PATTERN),
    ("amount", _AMOUNT_PATTERN),
    ("location", _GERMAN_ADDRESS_PATTERN),
]


_UMLAUT_MAP = {
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
    "Ä": "Ae",
    "Ö": "Oe",
    "Ü": "Ue",
    "Ã¤": "ae",
    "Ã¶": "oe",
    "Ã¼": "ue",
    "ÃŸ": "ss",
    "Ã„": "Ae",
    "Ã–": "Oe",
    "Ãœ": "Ue",
}
_UMLAUT_REVERSE_MAP = {"ae": "ä", "oe": "ö", "ue": "ü", "ss": "ß"}


def _normalize_umlaut(text: str) -> str:
    """Normalize Umlauts for comparison: Ã¤â†’ae, Ã¶â†’oe, Ã¼â†’ue, ÃŸâ†’ss."""
    normalized = text
    for source, replacement in _UMLAUT_MAP.items():
        normalized = normalized.replace(source, replacement)
    return normalized.lower()


def _levenshtein_ratio(a: str, b: str) -> float:
    """Compute normalized Levenshtein similarity ratio (1.0 = identical)."""
    if a == b:
        return 1.0
    len_a, len_b = len(a), len(b)
    if not len_a or not len_b:
        return 0.0
    max_len = max(len_a, len_b)
    # Optimize: if length difference alone exceeds threshold, skip
    if abs(len_a - len_b) / max_len > 0.3:
        return 0.0
    # Wagner-Fischer algorithm
    prev = list(range(len_b + 1))
    for i in range(1, len_a + 1):
        curr = [i] + [0] * len_b
        for j in range(1, len_b + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    distance = prev[len_b]
    return 1.0 - distance / max_len


def _fuzzy_match(a: str, b: str, threshold: float = 0.85) -> bool:
    """Check if two strings are fuzzy-similar above the given threshold.

    Considers both raw comparison and Umlaut-normalized comparison.
    """
    if a == b:
        return True
    # Direct ratio
    if _levenshtein_ratio(a, b) >= threshold:
        return True
    # Umlaut-normalized ratio
    norm_a = _normalize_umlaut(a)
    norm_b = _normalize_umlaut(b)
    if norm_a == norm_b:
        return True
    return _levenshtein_ratio(norm_a, norm_b) >= threshold


class KnowledgeGraphService:
    """Local knowledge graph: extract entities and edges from document chunks."""

    def __init__(self, connection: sqlite3.Connection, profile_slug: str, *, llm_client=None) -> None:
        self.connection = connection
        self.profile_slug = profile_slug
        self.llm_client = llm_client

    def upsert_entity(self, entity_type: str, name: str, metadata: dict | None = None) -> str:
        display_name = name.strip()
        canonical_name = self._canonical_entity_name(entity_type, display_name)
        rows = self.connection.execute(
            """
            SELECT id, name, metadata_json
            FROM knowledge_entities
            WHERE profile_slug = ? AND entity_type = ?
            ORDER BY created_at DESC
            """,
            (self.profile_slug, entity_type),
        ).fetchall()
        for row in rows:
            existing_metadata = json.loads(row["metadata_json"] or "{}")
            existing_canonical = str(
                existing_metadata.get("canonical_name", "") or self._canonical_entity_name(entity_type, str(row["name"] or ""))
            )
            if existing_canonical == canonical_name:
                aliases = list(existing_metadata.get("aliases") or [])
                if display_name and display_name != row["name"] and display_name not in aliases:
                    aliases.append(display_name)
                existing_metadata = self._merge_entity_metadata(
                    existing_metadata,
                    metadata,
                    canonical_name=canonical_name,
                    aliases=aliases,
                )
                self.connection.execute(
                    "UPDATE knowledge_entities SET metadata_json = ? WHERE id = ?",
                    (json.dumps(existing_metadata), row["id"]),
                )
                return str(row["id"])

        # Fuzzy match pass: check if any existing entity is close enough
        for row in rows:
            existing_metadata = json.loads(row["metadata_json"] or "{}")
            existing_canonical = str(
                existing_metadata.get("canonical_name", "") or self._canonical_entity_name(entity_type, str(row["name"] or ""))
            )
            if _fuzzy_match(existing_canonical, canonical_name):
                aliases = list(existing_metadata.get("aliases") or [])
                if display_name and display_name != row["name"] and display_name not in aliases:
                    aliases.append(display_name)
                existing_metadata = self._merge_entity_metadata(
                    existing_metadata,
                    metadata,
                    canonical_name=existing_canonical,  # keep the original canonical
                    aliases=aliases,
                )
                self.connection.execute(
                    "UPDATE knowledge_entities SET metadata_json = ? WHERE id = ?",
                    (json.dumps(existing_metadata), row["id"]),
                )
                return str(row["id"])

        entity_id = str(uuid4())
        payload = self._merge_entity_metadata({}, metadata, canonical_name=canonical_name, aliases=[])
        self.connection.execute(
            """
            INSERT INTO knowledge_entities (id, entity_type, name, metadata_json, created_at, profile_slug)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (entity_id, entity_type, display_name, json.dumps(payload), datetime.now(timezone.utc).isoformat(), self.profile_slug),
        )
        return entity_id

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        relationship: str,
        source_document_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        exists = self.connection.execute(
            "SELECT id, metadata_json FROM knowledge_edges WHERE source_entity_id = ? AND target_entity_id = ? AND relationship = ?",
            (source_id, target_id, relationship),
        ).fetchone()
        if exists:
            merged_metadata = self._merge_edge_metadata(json.loads(exists["metadata_json"] or "{}"), metadata, source_document_id)
            self.connection.execute(
                "UPDATE knowledge_edges SET metadata_json = ?, source_document_id = COALESCE(source_document_id, ?) WHERE id = ?",
                (json.dumps(merged_metadata), source_document_id, exists["id"]),
            )
            return
        self.connection.execute(
            """
            INSERT INTO knowledge_edges (source_entity_id, target_entity_id, relationship, source_document_id, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                target_id,
                relationship,
                source_document_id,
                json.dumps(self._merge_edge_metadata({}, metadata, source_document_id)),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def extract_from_text(self, text: str, document_id: str | None = None) -> dict[str, list[str]]:
        entity_ids_by_type: dict[str, set[str]] = defaultdict(set)
        sentences = self._split_sentences(text)
        for sentence in sentences:
            sentence_lower = sentence.lower()
            sentence_entities: list[tuple[str, str, str]] = []
            for entity_type, pattern in _ENTITY_PATTERNS:
                matches = list({match.group(1).strip() for match in pattern.finditer(sentence)})
                for name in matches[:20]:
                    metadata = {
                        "source_document_ids": [document_id] if document_id else [],
                        "evidence_samples": [sentence[:220]],
                        "last_seen_sentence": sentence[:220],
                    }
                    entity_id = self.upsert_entity(entity_type, name, metadata)
                    entity_ids_by_type[entity_type].add(entity_id)
                    sentence_entities.append((entity_id, entity_type, name))
            # German postal code + city extraction (PLZ Stadt)
            for loc_match in _LOCATION_PATTERN.finditer(sentence):
                plz = loc_match.group(1)
                city = loc_match.group(2).strip()
                location_name = f"{plz} {city}"
                meta = {
                    "source_document_ids": [document_id] if document_id else [],
                    "evidence_samples": [sentence[:220]],
                    "plz": plz,
                    "city": city,
                }
                loc_id = self.upsert_entity("location", location_name, meta)
                entity_ids_by_type["location"].add(loc_id)
                sentence_entities.append((loc_id, "location", location_name))
            event_name = self._infer_event_name(sentence)
            if event_name:
                event_id = self.upsert_entity(
                    "event",
                    event_name,
                    {
                        "source_document_ids": [document_id] if document_id else [],
                        "evidence_samples": [sentence[:220]],
                        "event_kind": event_name,
                    },
                )
                entity_ids_by_type["event"].add(event_id)
                sentence_entities.append((event_id, "event", event_name))
            for index, source_entity in enumerate(sentence_entities):
                source_id, source_type, source_name = source_entity
                for target_id, target_type, target_name in sentence_entities[index + 1 :]:
                    relationship = self._relationship_for_pair(
                        source_type,
                        source_name,
                        target_type,
                        target_name,
                        sentence_lower,
                    )
                    edge_metadata = {
                        "source_document_ids": [document_id] if document_id else [],
                        "evidence_samples": [sentence[:220]],
                        "sentence": sentence[:220],
                    }
                    self.add_edge(source_id, target_id, relationship, source_document_id=document_id, metadata=edge_metadata)
        self.connection.commit()
        return {entity_type: sorted(ids) for entity_type, ids in entity_ids_by_type.items()}

    def extract_from_document(self, document_id: str, text: str) -> int:
        entity_ids = self.extract_from_text(text, document_id=document_id)
        return sum(len(value) for value in entity_ids.values())

    async def extract_from_text_llm(
        self, text: str, document_id: str | None = None, llm_client=None
    ) -> dict[str, list[str]]:
        """Extract entities using the local LLM, falling back to regex on failure."""
        if llm_client is None or not getattr(llm_client, "available", False):
            return self.extract_from_text(text, document_id=document_id)

        prompt_text = text[:4000] if len(text) > 4000 else text
        messages = [
            {
                "role": "system",
                "content": (
                    "Extract named entities from the text. Return a JSON object with keys: "
                    '"persons" (list of person names), "companies" (list of company names), '
                    '"dates" (list of dates in YYYY-MM-DD format), "amounts" (list of monetary amounts). '
                    "Include German names with Umlauts (Ã¤, Ã¶, Ã¼, ÃŸ). Only return the JSON, nothing else."
                ),
            },
            {"role": "user", "content": prompt_text},
        ]
        try:
            response = await llm_client.chat(messages, max_tokens=512, temperature=0.1)
            choices = response.get("choices", [])
            if not choices:
                return self.extract_from_text(text, document_id=document_id)
            content = choices[0].get("message", {}).get("content", "")
            # Try to parse JSON from the response
            import re as _re
            json_match = _re.search(r"\{.*\}", content, _re.DOTALL)
            if not json_match:
                return self.extract_from_text(text, document_id=document_id)
            extracted = json.loads(json_match.group())
        except Exception:
            logger.debug("LLM entity extraction failed, falling back to regex.")
            return self.extract_from_text(text, document_id=document_id)

        # Process LLM-extracted entities
        entity_ids_by_type: dict[str, set[str]] = defaultdict(set)
        type_mapping = {
            "persons": "person",
            "companies": "company",
            "dates": "date",
            "amounts": "amount",
        }
        for llm_key, entity_type in type_mapping.items():
            for name in (extracted.get(llm_key) or [])[:30]:
                if not name or not str(name).strip():
                    continue
                meta = {
                    "source_document_ids": [document_id] if document_id else [],
                    "extraction_method": "llm",
                }
                entity_id = self.upsert_entity(entity_type, str(name).strip(), meta)
                entity_ids_by_type[entity_type].add(entity_id)

        # Also run regex extraction to catch patterns LLM might miss (dates, amounts)
        regex_result = self.extract_from_text(text, document_id=document_id)
        for entity_type, ids in regex_result.items():
            entity_ids_by_type[entity_type].update(ids)

        return {entity_type: sorted(ids) for entity_type, ids in entity_ids_by_type.items()}

    def search_entities(self, query: str, limit: int = 20) -> list[dict]:
        normalized_query = query.strip().lower()
        rows = self.connection.execute(
            """
            SELECT id, entity_type, name, metadata_json, created_at
            FROM knowledge_entities
            WHERE profile_slug = ?
            ORDER BY created_at DESC
            """,
            (self.profile_slug,),
        ).fetchall()
        scored: list[tuple[int, dict]] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            haystacks = [str(row["name"] or "").lower(), str(metadata.get("canonical_name", "") or "").lower()]
            haystacks.extend(str(alias).lower() for alias in (metadata.get("aliases") or []))
            best_score = 0
            for item in haystacks:
                if not item:
                    continue
                if item == normalized_query:
                    best_score = max(best_score, 4)
                elif item.startswith(normalized_query):
                    best_score = max(best_score, 3)
                elif normalized_query and normalized_query in item:
                    best_score = max(best_score, 2)
            if best_score:
                scored.append(
                    (
                        -best_score,
                        {
                            "id": row["id"],
                            "type": row["entity_type"],
                            "name": row["name"],
                            "metadata": metadata,
                            "created_at": row["created_at"],
                        },
                    )
                )
        return [
            item
            for _, item in sorted(scored, key=lambda pair: (pair[0], pair[1]["created_at"]), reverse=False)[:limit]
        ]

    def get_neighborhood(self, entity_id: str, depth: int = 1) -> dict:
        entity = self.connection.execute(
            "SELECT id, entity_type, name, metadata_json FROM knowledge_entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if not entity:
            return {"entity": None, "edges": []}
        frontier = {entity_id}
        visited = {entity_id}
        collected_edges: dict[str, dict] = {}
        for current_depth in range(max(1, depth)):
            if not frontier:
                break
            placeholders = ",".join("?" * len(frontier))
            edges_rows = self.connection.execute(
                f"""
                SELECT e.id AS edge_id, e.relationship, e.metadata_json,
                       src.id AS src_id, src.entity_type AS src_type, src.name AS src_name,
                       tgt.id AS tgt_id, tgt.entity_type AS tgt_type, tgt.name AS tgt_name
                FROM knowledge_edges e
                JOIN knowledge_entities src ON src.id = e.source_entity_id
                JOIN knowledge_entities tgt ON tgt.id = e.target_entity_id
                WHERE e.source_entity_id IN ({placeholders}) OR e.target_entity_id IN ({placeholders})
                LIMIT 120
                """,
                list(frontier) + list(frontier),
            ).fetchall()
            next_frontier: set[str] = set()
            for row in edges_rows:
                edge_id = str(row["edge_id"])
                if edge_id not in collected_edges:
                    collected_edges[edge_id] = {
                        "relationship": row["relationship"],
                        "depth": current_depth + 1,
                        "metadata": json.loads(row["metadata_json"] or "{}"),
                        "source": {"id": row["src_id"], "type": row["src_type"], "name": row["src_name"]},
                        "target": {"id": row["tgt_id"], "type": row["tgt_type"], "name": row["tgt_name"]},
                    }
                for candidate in (str(row["src_id"]), str(row["tgt_id"])):
                    if candidate not in visited:
                        next_frontier.add(candidate)
                        visited.add(candidate)
            frontier = next_frontier
        return {
            "entity": {
                "id": entity["id"],
                "type": entity["entity_type"],
                "name": entity["name"],
                "metadata": json.loads(entity["metadata_json"] or "{}"),
            },
            "edges": list(collected_edges.values()),
        }

    def graph_snapshot(self, limit_nodes: int = 80, limit_edges: int = 200) -> dict:
        nodes_rows = self.connection.execute(
            """
            SELECT id, entity_type, name
            FROM knowledge_entities
            WHERE profile_slug = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (self.profile_slug, limit_nodes),
        ).fetchall()
        node_ids = {row["id"] for row in nodes_rows}
        edges_rows = (
            self.connection.execute(
                """
                SELECT source_entity_id, target_entity_id, relationship
                FROM knowledge_edges
                WHERE source_entity_id IN ({0}) AND target_entity_id IN ({0})
                LIMIT ?
                """.format(",".join("?" * len(node_ids))),
                list(node_ids) + list(node_ids) + [limit_edges],
            ).fetchall()
            if node_ids
            else []
        )
        return {
            "nodes": [{"id": row["id"], "type": row["entity_type"], "name": row["name"]} for row in nodes_rows],
            "links": [
                {"source": row["source_entity_id"], "target": row["target_entity_id"], "relationship": row["relationship"]}
                for row in edges_rows
            ],
        }

    def build_from_documents(self, document_service, limit: int = 50) -> int:
        docs = document_service.list_documents(limit=limit, audit=False)
        all_chunks = document_service.memory.list_all_document_chunks()
        chunks_by_source: dict[str, list[str]] = {}
        for chunk in all_chunks:
            source_id = str(chunk.get("source_id", "") or "")
            if source_id:
                chunks_by_source.setdefault(source_id, []).append(str(chunk.get("text", "") or ""))
        total = 0
        for doc in docs:
            try:
                text = " ".join(chunks_by_source.get(doc.id, []))
                if text:
                    total += self.extract_from_document(doc.id, text)
            except Exception as exc:
                logger.debug("KG extraction error for doc %s: %s", doc.id, exc)
        return total

    def _canonical_entity_name(self, entity_type: str, name: str) -> str:
        cleaned = " ".join(name.strip().split())
        if entity_type == "company":
            cleaned = re.sub(r"[.,]", "", cleaned)
            cleaned = re.sub(r"\b(gmbh|ag|ltd|inc|corp|llc|ug|se|kg)\b", lambda match: match.group(1).upper(), cleaned, flags=re.IGNORECASE)
            return cleaned.lower()
        if entity_type == "amount":
            normalized = cleaned.replace("â‚¬", "EUR").replace("Â£", "GBP").replace("$", "USD")
            normalized = normalized.replace("Ã¢â€šÂ¬", "EUR").replace("Ã‚Â£", "GBP")
            return re.sub(r"\s+", " ", normalized).lower()
        if entity_type == "date":
            return self._normalize_date(cleaned)
        return cleaned.lower()

    def _normalize_date(self, value: str) -> str:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
        match = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", value)
        if match:
            day, month, year = match.groups()
            if len(year) == 2:
                year_int = int(year)
                year = f"20{year}" if year_int <= 49 else f"19{year}"
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        return value.lower()

    def _merge_entity_metadata(
        self,
        existing_metadata: dict,
        incoming_metadata: dict | None,
        *,
        canonical_name: str,
        aliases: list[str],
    ) -> dict:
        payload = dict(existing_metadata or {})
        incoming = dict(incoming_metadata or {})
        payload["aliases"] = aliases[:10]
        payload["canonical_name"] = canonical_name
        payload["occurrences"] = int(payload.get("occurrences", 0) or 0) + 1
        source_ids = set(str(value) for value in payload.get("source_document_ids", []) if value)
        incoming_source_id = incoming.pop("source_document_id", None)
        if incoming_source_id:
            source_ids.add(str(incoming_source_id))
        source_ids.update(str(value) for value in incoming.pop("source_document_ids", []) if value)
        if source_ids:
            payload["source_document_ids"] = sorted(source_ids)[:20]
        evidence_samples = [str(value) for value in payload.get("evidence_samples", []) if value]
        for sample in incoming.pop("evidence_samples", []):
            text = str(sample).strip()
            if text and text not in evidence_samples:
                evidence_samples.append(text)
        if evidence_samples:
            payload["evidence_samples"] = evidence_samples[:5]
        payload.update(incoming)
        return payload

    def _merge_edge_metadata(self, existing_metadata: dict, incoming_metadata: dict | None, source_document_id: str | None) -> dict:
        payload = dict(existing_metadata or {})
        incoming = dict(incoming_metadata or {})
        payload["occurrences"] = int(payload.get("occurrences", 0) or 0) + 1
        source_ids = set(str(value) for value in payload.get("source_document_ids", []) if value)
        if source_document_id:
            source_ids.add(str(source_document_id))
        source_ids.update(str(value) for value in incoming.pop("source_document_ids", []) if value)
        if source_ids:
            payload["source_document_ids"] = sorted(source_ids)[:20]
        evidence_samples = [str(value) for value in payload.get("evidence_samples", []) if value]
        for sample in incoming.pop("evidence_samples", []):
            text = str(sample).strip()
            if text and text not in evidence_samples:
                evidence_samples.append(text)
        if evidence_samples:
            payload["evidence_samples"] = evidence_samples[:5]
        payload.update(incoming)
        return payload

    def _split_sentences(self, text: str) -> list[str]:
        parts = [segment.strip() for segment in _SENTENCE_SPLIT_PATTERN.split(text) if segment.strip()]
        return parts[:80]

    def _infer_event_name(self, sentence: str) -> str | None:
        lowered = sentence.lower()
        if any(token in lowered for token in _INVOICE_CUES):
            return "invoice"
        if any(token in lowered for token in _CONTRACT_CUES):
            return "contract"
        if any(token in lowered for token in _OFFER_CUES):
            return "offer"
        if any(token in lowered for token in _MEETING_CUES):
            return "meeting"
        return None

    def _relationship_for_pair(
        self,
        source_type: str,
        source_name: str,
        target_type: str,
        target_name: str,
        sentence_lower: str,
    ) -> str:
        pair = {source_type, target_type}
        if "event" in pair and "date" in pair and any(token in sentence_lower for token in _DUE_CUES):
            return "due_on"
        if "event" in pair and "amount" in pair:
            return "amount_associated_with"
        if pair == {"company", "amount"}:
            return "amount_associated_with"
        if pair == {"company", "date"}:
            return "date_associated_with"
        if "event" in pair and "company" in pair:
            return "involves"
        if "event" in pair and "person" in pair:
            return "mentions_participant"
        if "date" in pair and "person" in pair and any(token in sentence_lower for token in _MEETING_CUES):
            return "mentioned_in_event"
        if source_name.lower() == target_name.lower():
            return "alias_of"
        return "co_occurs_with"
