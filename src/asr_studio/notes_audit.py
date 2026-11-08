"""Bounded reference selection for the one-pass Notes source audit.

The provider still returns the public schema-version-1 document.  During the audit only,
``item.text`` is an opaque catalog reference.  This module resolves those references at the
pipeline boundary, before the document can be published.  Catalog entries retain source positions
and end offsets rather than materialising every possible text prefix.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from asr_studio import notes_document as nd

_DRAFT_KIND = "draft"
_SOURCE_KIND = "source"
_MARKERS = frozenset(
    {
        "not",
        "no",
        "never",
        "without",
        "cannot",
        "can't",
        "couldn't",
        "didn't",
        "doesn't",
        "isn't",
        "won't",
        "wouldn't",
        "unknown",
        "unconfirmed",
        "undecided",
        "uncommitted",
        "pending",
        "proposed",
        "proposal",
        "maybe",
        "perhaps",
        "may",
        "might",
        "could",
        "if",
        "unless",
        "support",
        "supported",
        "opposition",
        "oppose",
        "opposed",
        "against",
        "reject",
        "rejected",
        "defer",
        "deferred",
        "không",
        "chưa",
        "chẳng",
        "khôngđược",
        "nếu",
        "cóthể",
        "dựkiến",
        "đềxuất",
        "chờ",
        "phảnđối",
        "ủnghộ",
    }
)
_COMPOUND_MARKER_TOKENS = frozenset(
    {
        ("không", "được"),
        ("có", "thể"),
        ("dự", "kiến"),
        ("đề", "xuất"),
        ("phản", "đối"),
        ("ủng", "hộ"),
    }
)
_WORD_RE = re.compile(r"\S+", re.UNICODE)

AuditKind = Literal["draft", "source"]


def _error(
    message: str,
    *,
    reason_code: str = "text",
    field_name: str | None = None,
) -> nd.NotesDocumentError:
    return nd.NotesDocumentError(message, reason_code=reason_code, field_name=field_name)


def _marker_token(value: str) -> str:
    """Fold punctuation around a word without copying any source prefix."""
    folded = unicodedata.normalize("NFC", value).casefold()
    while folded and not folded[0].isalnum() and folded[0] != "'":
        folded = folded[1:]
    while folded and not folded[-1].isalnum() and folded[-1] != "'":
        folded = folded[:-1]
    return folded.replace(" ", "")


def _safe_prefix_end_offsets(text: str) -> tuple[int, ...]:
    """Return word ends whose suffix does not contain a certainty/polarity marker.

    The scan is linear.  It never slices ``text`` while building the catalog; text is sliced only
    for the one reference the provider actually selects.
    """
    matches = tuple(_WORD_RE.finditer(text))
    tokens = tuple(_marker_token(match.group()) for match in matches)
    marker_tokens = [token in _MARKERS for token in tokens]
    for index in range(len(tokens) - 1):
        if (tokens[index], tokens[index + 1]) in _COMPOUND_MARKER_TOKENS:
            marker_tokens[index] = True
            marker_tokens[index + 1] = True
    safe: list[int] = []
    marker_after = False
    for index in range(len(matches) - 1, -1, -1):
        match = matches[index]
        end = match.end()
        if end < len(text) and not marker_after:
            safe.append(end)
        marker_after = marker_after or marker_tokens[index]
    safe.reverse()
    return tuple(safe)


def _source_excerpt_text(text: str, output_language: str) -> str:
    """Label an unchanged source quote without translating its body."""
    if output_language == "en":
        label = "Source excerpt (original language; output language: English):"
    else:
        label = "Trích nguyên văn (ngôn ngữ gốc; ngôn ngữ đầu ra: tiếng Việt):"
    return f"{label} “{text}”"


@dataclass(frozen=True, slots=True)
class CatalogCandidate:
    """One opaque provider reference and its immutable origin."""

    ref: str
    kind: AuditKind
    section: str | None
    source_position: int
    end_offset: int | None
    source_id: str | None


@dataclass(frozen=True, slots=True)
class DraftCatalogEntry:
    """Compact prompt entry for one draft item; prefixes are end offsets only."""

    ref: str
    section: str
    source_position: int
    prefix_end_offsets: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SourceQuoteEntry:
    """Compact prompt entry for one whole source segment."""

    ref: str
    source_position: int
    source_id: str


@dataclass(frozen=True, slots=True)
class AuditCatalog:
    """Typed, bounded catalog used by one source-audit request.

    ``source`` and ``draft`` are treated as immutable inputs.  The catalog stores no copied
    prefixes: each draft candidate keeps only its section, position and optional end offset.
    """

    source: dict[str, Any]
    draft: dict[str, Any]
    draft_entries: tuple[DraftCatalogEntry, ...]
    source_quotes: tuple[SourceQuoteEntry, ...]
    _candidates: Mapping[str, CatalogCandidate]

    @classmethod
    def from_documents(
        cls,
        source: dict[str, Any],
        draft: dict[str, Any],
        *,
        include_source_quotes: bool = True,
    ) -> AuditCatalog:
        source_segments = source.get("segments") if isinstance(source, dict) else None
        if not isinstance(source_segments, list):
            raise _error("The audit source is invalid.", reason_code="source_ids")
        draft_candidates: list[DraftCatalogEntry] = []
        source_quotes: list[SourceQuoteEntry] = []
        candidates: dict[str, CatalogCandidate] = {}
        flat_position = 0
        for section, _heading in nd.SECTIONS:
            items = draft.get(section) if isinstance(draft, dict) else None
            if not isinstance(items, list):
                raise _error("The audit draft is invalid.", reason_code="items")
            for item_position, item in enumerate(items):
                text = item.get("text") if isinstance(item, dict) else None
                if not isinstance(text, str) or not text:
                    raise _error("The audit draft has invalid text.", reason_code="text")
                ref = f"d{flat_position}"
                end_offsets = _safe_prefix_end_offsets(text)
                draft_candidates.append(DraftCatalogEntry(ref, section, item_position, end_offsets))
                candidates[ref] = CatalogCandidate(
                    ref,
                    _DRAFT_KIND,
                    section,
                    item_position,
                    None,
                    None,
                )
                for end_offset in end_offsets:
                    prefix_ref = f"{ref}p{end_offset}"
                    candidates[prefix_ref] = CatalogCandidate(
                        prefix_ref,
                        _DRAFT_KIND,
                        section,
                        item_position,
                        end_offset,
                        None,
                    )
                flat_position += 1

        if include_source_quotes:
            for source_position, segment in enumerate(source_segments):
                if not isinstance(segment, dict):
                    raise _error("The audit source segment is invalid.", reason_code="source_ids")
                source_id = segment.get("id")
                text = segment.get("text")
                if not isinstance(source_id, str) or not isinstance(text, str):
                    raise _error("The audit source segment is invalid.", reason_code="source_ids")
                ref = f"s{source_position}"
                source_quotes.append(SourceQuoteEntry(ref, source_position, source_id))
                candidates[ref] = CatalogCandidate(
                    ref,
                    _SOURCE_KIND,
                    None,
                    source_position,
                    None,
                    source_id,
                )
        return cls(
            source=source,
            draft=draft,
            draft_entries=tuple(draft_candidates),
            source_quotes=tuple(source_quotes),
            _candidates=candidates,
        )

    @classmethod
    def build(
        cls,
        source: dict[str, Any],
        draft: dict[str, Any],
        *,
        include_source_quotes: bool = True,
    ) -> AuditCatalog:
        """Alias kept explicit for callers constructing the production catalog."""
        return cls.from_documents(source, draft, include_source_quotes=include_source_quotes)

    @property
    def candidate_count(self) -> int:
        return len(self._candidates)

    def prompt_payload(self) -> dict[str, Any]:
        """Return a JSON-safe catalog without duplicating draft/source text."""
        return {
            "version": 1,
            "draft_candidates": [
                {
                    "ref": entry.ref,
                    "section": entry.section,
                    "source_position": entry.source_position,
                    "prefix_end_offsets": list(entry.prefix_end_offsets),
                }
                for entry in self.draft_entries
            ],
            "source_quotes": [
                {
                    "ref": entry.ref,
                    "source_position": entry.source_position,
                    "source_id": entry.source_id,
                }
                for entry in self.source_quotes
            ],
        }

    # A descriptive alias makes the boundary clear to callers and tests.
    to_prompt_payload = prompt_payload

    def build_prompt(
        self,
        *,
        output_language: str = "en",
        invalid_output: str | None = None,
        budget: nd.NotesBudget | None = None,
        max_unit_size: int = 1,
    ) -> str:
        """Build the source-aware audit prompt without replacing its draft payload marker."""
        return nd.build_reduction_prompt(
            self.source,
            [self.draft],
            final=True,
            invalid_output=invalid_output,
            output_language=output_language,
            budget=budget,
            max_unit_size=max_unit_size,
            require_source=True,
            audit=True,
            audit_catalog=self.prompt_payload(),
        )

    def candidate(self, ref: str) -> CatalogCandidate:
        candidate = self._candidates.get(ref)
        if candidate is None:
            raise _error("The audit output contains an unknown reference.", reason_code="text")
        return candidate

    def resolve(self, ref: str) -> str:
        """Resolve one exact reference, materialising at most the selected text."""
        candidate = self.candidate(ref)
        if candidate.kind == _SOURCE_KIND:
            segment = self.source["segments"][candidate.source_position]
            text = segment.get("text") if isinstance(segment, dict) else None
            if not isinstance(text, str):
                raise _error("The audit source segment is invalid.", reason_code="source_ids")
            return text
        assert candidate.section is not None
        item = self.draft[candidate.section][candidate.source_position]
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(text, str):
            raise _error("The audit draft item is invalid.", reason_code="text")
        if candidate.end_offset is None:
            return text
        if candidate.end_offset not in self._safe_offsets(candidate):
            raise _error("The audit prefix is not an allowed reference.", reason_code="text")
        return text[: candidate.end_offset]

    def _safe_offsets(self, candidate: CatalogCandidate) -> tuple[int, ...]:
        assert candidate.section is not None
        text = self.draft[candidate.section][candidate.source_position]["text"]
        return _safe_prefix_end_offsets(text)

    def apply(
        self,
        selected: object,
        *,
        max_output_bytes: int = nd.MAX_OUTPUT_BYTES,
        max_unit_size: int = 1,
        output_language: str = "en",
    ) -> dict[str, Any]:
        """Resolve a provider selection and revalidate the resulting public document."""
        output_language = nd.validate_output_language(output_language)
        if not isinstance(selected, dict):
            raise _error("The audit output must be a Notes document.", reason_code="fields")
        # Validate the opaque schema first, but do not normalize/coalesce it: duplicate origins and
        # section changes must remain observable to this guard.
        nd.validate_document(
            selected,
            self.source,
            max_output_bytes=max_output_bytes,
            max_unit_size=max_unit_size,
        )
        result: dict[str, Any] = {
            "schema_version": nd.SCHEMA_VERSION,
            **{section: [] for section, _heading in nd.SECTIONS},
        }
        used_origins: set[tuple[str, str | int, int]] = set()
        for section, _heading in nd.SECTIONS:
            items = selected[section]
            for item in items:
                ref = item["text"]
                candidate = self.candidate(ref)
                output_item = copy.deepcopy(item)
                if candidate.kind == _DRAFT_KIND:
                    assert candidate.section is not None
                    if section != candidate.section:
                        raise _error(
                            "The audit cannot promote a draft item to another section.",
                            reason_code="items",
                        )
                    origin = ("draft", candidate.section, candidate.source_position)
                    if origin in used_origins:
                        raise _error(
                            "The audit cannot select one draft origin more than once.",
                            reason_code="items",
                        )
                    used_origins.add(origin)
                    if section == "action_items":
                        original = self.draft[section][candidate.source_position]
                        for field in sorted(nd.NOTES_ACTION_FIELD_NAMES):
                            value = item[field]
                            if value is not None and value != original.get(field):
                                raise _error(
                                    "The audit cannot invent action metadata.",
                                    reason_code="action_fields",
                                    field_name=field,
                                )
                    output_item["text"] = self.resolve(ref)
                else:
                    if section not in {"key_topics", "confirmation_items"}:
                        raise _error(
                            "A source excerpt is allowed only in context or confirmation.",
                            reason_code="items",
                        )
                    origin = ("source", candidate.source_position, candidate.source_position)
                    if origin in used_origins:
                        raise _error(
                            "The audit cannot select one source position more than once.",
                            reason_code="items",
                        )
                    used_origins.add(origin)
                    source_id = candidate.source_id
                    assert source_id is not None
                    output_item["source_ids"] = list(
                        dict.fromkeys([source_id, *item["source_ids"]])
                    )
                    output_item["text"] = _source_excerpt_text(self.resolve(ref), output_language)
                result[section].append(output_item)
        try:
            return nd.validate_document(
                result,
                self.source,
                max_output_bytes=max_output_bytes,
                max_unit_size=max_unit_size,
            )
        except nd.NotesDocumentError:
            raise
        except (TypeError, ValueError) as error:
            raise _error("The resolved audit document is invalid.", reason_code="fields") from error


def build_audit_catalog(
    source: dict[str, Any],
    draft: dict[str, Any],
    *,
    include_source_quotes: bool = True,
) -> AuditCatalog:
    """Construct the production reference catalog."""
    return AuditCatalog.from_documents(source, draft, include_source_quotes=include_source_quotes)


Catalog = AuditCatalog


def build_audit_prompt(
    source: dict[str, Any],
    draft: dict[str, Any],
    *,
    output_language: str = "en",
    invalid_output: str | None = None,
    budget: nd.NotesBudget | None = None,
    max_unit_size: int = 1,
    include_source_quotes: bool = True,
) -> str:
    """Convenience prompt builder for the bounded reference-audit contract."""
    return build_audit_catalog(
        source, draft, include_source_quotes=include_source_quotes
    ).build_prompt(
        output_language=output_language,
        invalid_output=invalid_output,
        budget=budget,
        max_unit_size=max_unit_size,
    )


def apply_audit_selection(
    source: dict[str, Any],
    draft: dict[str, Any],
    selected: object,
    *,
    include_source_quotes: bool = True,
    max_output_bytes: int = nd.MAX_OUTPUT_BYTES,
    max_unit_size: int = 1,
    output_language: str = "en",
) -> dict[str, Any]:
    """Convenience boundary for tests and provider integrations."""
    return build_audit_catalog(source, draft, include_source_quotes=include_source_quotes).apply(
        selected,
        max_output_bytes=max_output_bytes,
        max_unit_size=max_unit_size,
        output_language=output_language,
    )
