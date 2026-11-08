"""One bounded source-binding audit for registered-Gemini long notes, as sparse immutable refs.

The pass may split the final items into at most two balanced, disjoint partitions; each one
reads the full source but sees and may patch only its own items under their global refs. The
validated part patches are merged and applied once, so every item is audited exactly once.

The model never writes text. Each final item has an original text ref and, at most, a few
anonymous variants that code derived: exact suffixes of the original that drop only a leading
proper-name attribution or leading proper qualifiers. A sparse patch selects a text ref, adds
source IDs (a union onto the cited IDs) and may clear an action owner or deadline. Unlisted
items stay unchanged; no item can be dropped, duplicated, moved or invented, and any unresolved
ref is quarantined: its unverified wording is removed and its cited source segments are
published verbatim as one needs-review confirmation item.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from asr_studio import notes_document as nd
from asr_studio.notes_audit import _source_excerpt_text

AUDIT_VERSION = 1
MAX_SEMANTIC_OUTPUT_BYTES = 64_000  # one part's response and the merged patch alike
MAX_AUDIT_PARTS = 2
MAX_LEADING_NAME_TOKENS = 4
MAX_VARIANTS_PER_ITEM = 4
_NEEDS_REVIEW = {
    "en": "Needs review: an unverified note was removed.",
    "vi": "Cần xem lại: đã bỏ một ghi chú chưa được kiểm chứng.",
}
_SECTION_NAMES = tuple(section for section, _heading in nd.SECTIONS)
_TOKEN_RE = re.compile(r"\S+", re.UNICODE)
_EDGE_RE = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)
_POSSESSIVE_RE = re.compile(r"['’]s$")

# A dropped prefix may never carry one of these; the kept suffix is literal, so every other fact
# (numbers, dates, negation, modality) survives byte-for-byte.
_PROTECTED_WORDS = frozenset(
    """
    january february march april may june july august september october november december
    jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday
    saturday sunday today tomorrow tonight yesterday week month year quarter
    zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen
    sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety
    hundred thousand million billion half dozen first second third once twice
    no not never none neither nor without cannot
    thứ tháng ngày năm tuần quý hôm mai qua một hai ba bốn sáu bảy tám chín mười trăm nghìn
    ngàn triệu tỷ không chưa chẳng đừng chớ
    """.split()  # noqa: SIM905 - a readable word list
)
_ATTRIBUTION_EN = frozenset(
    """
    noted said says stated argued asked explained urged praised thanked acknowledged emphasized
    emphasised highlighted raised questioned expressed described reported mentioned suggested
    proposed recommended warned cautioned credited criticized criticised called added agreed
    disagreed supported opposed testified commented remarked claimed believes believed thinks
    felt feels wanted wants rationale acknowledgment acknowledgement acknowledgments explanation
    praise remarks remark comments comment concern concerns view views position question
    questions request testimony statement argument opinion thanks critique criticism support
    opposition point points recommendation proposal
    """.split()  # noqa: SIM905 - a readable word list
)
_ATTRIBUTION_VI = (
    "cho biết",
    "cho rằng",
    "nói",
    "nêu",
    "phát biểu",
    "giải thích",
    "nhấn mạnh",
    "đề nghị",
    "đề xuất",
    "kêu gọi",
    "cảm ơn",
    "khen",
    "ca ngợi",
    "hỏi",
    "ủng hộ",
    "phản đối",
    "lý do",
    "ý kiến",
    "quan điểm",
    "lo ngại",
    "nhận xét",
    "bình luận",
    "lưu ý",
    "chia sẻ",
    "báo cáo",
)


def _error(message: str, reason_code: str = "items") -> nd.NotesDocumentError:
    return nd.NotesDocumentError(message, reason_code=reason_code)


def _word(token: str) -> str:
    return _POSSESSIVE_RE.sub("", _EDGE_RE.sub("", token)).casefold()


def _common_vocabulary(source: Mapping[str, Any]) -> frozenset[str]:
    """Words the source itself writes lower-case: generic headings, never a proper name."""
    words: set[str] = set()
    for segment in source.get("segments", []):
        for token in _TOKEN_RE.findall(str(segment.get("text", ""))):
            bare = _EDGE_RE.sub("", token)
            if bare[:1].islower():
                words.add(_word(bare))
    return frozenset(words)


def _protected(text: str) -> bool:
    return any(c.isdigit() for c in text) or any(
        _word(token) in _PROTECTED_WORDS for token in _TOKEN_RE.findall(text)
    )


def _proper(token: str, common: frozenset[str]) -> bool:
    bare = _EDGE_RE.sub("", token)
    word = _word(token)
    return (
        bare[:1].isupper()
        and not any(c.isdigit() for c in bare)
        and word not in common
        and word not in _PROTECTED_WORDS
    )


def _starts_attribution(rest: str) -> bool:
    folded = rest.casefold()
    first = _word(folded.split(maxsplit=1)[0]) if folded.strip() else ""
    return first in _ATTRIBUTION_EN or any(
        folded.startswith(p) and folded[len(p) : len(p) + 1] in {"", " ", ":", ","}
        for p in _ATTRIBUTION_VI
    )


def _suffix(text: str, start: int) -> str:
    rest = text[start:]
    return rest[:1].upper() + rest[1:]  # case-only change of the first kept character


def _phrase(tokens: list[re.Match[str]], start: int, end: int) -> str:
    return " ".join(_word(token.group()) for token in tokens[start:end])


def anonymous_variants(
    text: str, common: frozenset[str] = frozenset(), cited_text: str = ""
) -> list[str]:
    """Exact suffixes dropping a leading proper-name attribution or leading proper qualifiers.

    A qualifier drop is offered only when the whole leading name is absent from the cited text
    and the kept tail is present there, so a supported compound name is never shortened.
    """
    tokens = list(_TOKEN_RE.finditer(text))
    run = 0
    while run < min(len(tokens) - 1, MAX_LEADING_NAME_TOKENS) and _proper(
        tokens[run].group(), common
    ):
        run += 1
        if tokens[run - 1].group().endswith((":", ",")):
            break  # "Name: claim" — the colon ends the attribution
    if run == 0:
        return []
    starts: list[int] = []
    if tokens[run - 1].group().endswith(":") or _starts_attribution(text[tokens[run].start() :]):
        starts.append(tokens[run].start())
    cited = " ".join(_word(token) for token in _TOKEN_RE.findall(cited_text))
    if run > 1 and _phrase(tokens, 0, run) not in cited:
        starts.extend(
            tokens[k].start()
            for k in range(run - 1, 0, -1)  # shortest kept tail first
            if f" {_phrase(tokens, k, run)} " in f" {cited} "
        )
    variants: list[str] = []
    for start in starts:
        variant = _suffix(text, start)
        if _protected(text[:start]) or not variant.strip() or variant in variants:
            continue
        variants.append(variant)
    return variants[:MAX_VARIANTS_PER_ITEM]


# ----- secondary legal references (detail-free variants) ---------------------------------------

_EXCERPT_LABELS = tuple(
    _source_excerpt_text("", language).removesuffix(" “”") for language in ("en", "vi")
)
_REFERENCE_UNCLEAR = {"en": "[reference unclear]", "vi": "[tham chiếu chưa rõ]"}
_QUANTITY_UNCLEAR = {"en": "[quantity unclear]", "vi": "[số lượng chưa rõ]"}
# A multi-component dotted identifier; a 1-3 digit lead never reads a 4-digit year as one.
_DOTTED = r"(?<![\w.])\d{1,3}[A-Za-z]?(?:\.\d+[A-Za-z]?)+(?:\([0-9A-Za-z]{1,3}\))*(?![\w.]*\d)"
_REF_LIST = rf"{_DOTTED}(?:(?:\s*,\s*(?:(?:and|or|và|hoặc)\s+)?|\s+(?:and|or|through|to|và|hoặc|đến)\s+){_DOTTED})*"  # noqa: E501
_WORD_LABEL = (
    r"(?i:(?:municipal|administrative|revised|penal|civil|criminal|building|zoning|land\s+use|"
    r"tax|health|city|county|state|government|labou?r|family|commercial|vehicle)\s+codes?|"
    r"code\s+sections?|sections?|sec\.|articles?|art\.|clauses?|chapters?|"
    r"điều|khoản|mục|chương)|§§?"
)
_LEGAL_RE = re.compile(
    rf"(?:(?<!\w)(?P<word>{_WORD_LABEL})|\b(?P<acronym>[A-Z]{{2,6}}))\s+(?P<refs>{_REF_LIST})",
    re.UNICODE,
)

_ACRONYM_LEGAL_CONTEXT = re.compile(
    r"\b(?:(?:bill|ordinance|resolution|statute|act|section|article|clause)\s+"
    r"(?:no\.?\s*)?\d+|(?:statutory|legal)\s+(?:code|references?)|"
    r"(?:municipal|administrative|penal|civil|criminal|tax)\s+code)\b",
    re.IGNORECASE,
)
_TECHNICAL_REFERENCE_CONTEXT = re.compile(
    r"\b(?:versions?|releases?|software|firmware|sdk|api|library|package|installed|"
    r"installing|installs?|upgrades?|upgrading|deployed|deploying|product|build|model)\b",
    re.IGNORECASE,
)


def _date_like_reference(reference: str) -> bool:
    parts = reference.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts) or len(parts[2]) != 4:
        return False
    first, second = int(parts[0]), int(parts[1])
    return (1 <= first <= 31 and 1 <= second <= 12) or (1 <= first <= 12 and 1 <= second <= 31)


def reference_variant(text: str, output_language: str = "en") -> str | None:
    """The text with only labelled multi-component legal reference lists marked as unclear.

    A list follows an explicit statutory label (code, section, article, clause, chapter, §) or
    an all-caps code acronym with nearby statutory context and no software-version cue.
    Every acronym identifier needs at least three components.
    Record numbers, votes, dates and every other word stay byte-for-byte.
    """
    marker = _REFERENCE_UNCLEAR[nd.validate_output_language(output_language)]
    pieces: list[str] = []
    last = 0
    for match in _LEGAL_RE.finditer(text):
        refs = re.findall(_DOTTED, match["refs"])
        if any(_date_like_reference(ref) for ref in refs):
            continue
        if match["acronym"]:
            preceding = text[max(0, match.start() - 200) : match.start()]
            local = text[max(0, match.start() - 60) : match.end() + 30]
            if (
                any(ref.count(".") < 2 for ref in refs)
                or not _ACRONYM_LEGAL_CONTEXT.search(preceding)
                or _TECHNICAL_REFERENCE_CONTEXT.search(local)
            ):
                continue  # an acronym or a product version alone is not a statutory label
        pieces += [text[last : match.start("refs")], marker]
        last = match.end("refs")
    return None if not pieces else "".join(pieces) + text[last:]


# ----- joined-number size enumerations (quantity marker) --------------------------------------

_SMALL_NUMBERS = {
    **{str(n): n for n in range(1, 10)},
    **{word: n for n, word in enumerate("one two three four five six seven eight nine".split(), 1)},  # noqa: SIM905
}
_SMALL = "|".join(sorted(_SMALL_NUMBERS, key=len, reverse=True))
_SIZE_UNIT = (
    r"(?:bed(?:room)?|bath(?:room)?|room|seat(?:er)?|stor(?:e?y|ies|eys)|door|axle|berth|"
    r"person|people|car|bay)s?"
)
# "243, four and five-seat vehicles": an ASR merge of a total and the first size can make the
# total's last digit the member just before a consecutive size pair.
_ENUMERATION_RE = re.compile(
    rf"(?<![\d.,])(?P<n>\d{{1,3}}(?:,\d{{3}})+|\d{{2,}})\s*,\s*(?P<a>{_SMALL})"
    rf"(?:\s*-?\s*(?:,|and|or|to|-|–)\s*|\s+)(?P<b>{_SMALL})[\s-]+{_SIZE_UNIT}\b"
    r"(?P<tail>(?:[\s-]+[\w'’-]+){1,3})",
    re.IGNORECASE | re.UNICODE,
)
# Counted-object families; a quantity is marked only when its noun shares the enumeration's.
_FAMILIES = {
    "dwellings": (
        "apartment unit home house townhome townhouse rowhome condo condominium dwelling "
        "residence flat household căn nhà hộ"
    ),
    "vehicles": "vehicle car bus van truck boat xe",
    "lodging": "suite cabin cottage phòng",
}
_FAMILY_OF = {word: family for family, words in _FAMILIES.items() for word in words.split()}
_LABEL_BEFORE = re.compile(
    r"(?i:(?:bill|resolution|ordinance|appointment|appointments|item|no\.?|number|case|file|"
    r"docket|route|district|ward|section|code|chapter|article|clause|số|dự\s+luật|nghị\s+quyết)"
    r"\s*#?\s*|#)$"
)
_COUNT_CUE = re.compile(r"(?i:(?:total|count|number)\s+of|tổng(?:\s+(?:cộng|số))?)\s*$")


def _family(word: str) -> str | None:
    word = _word(word)
    for stem in (word, word[:-1] if word.endswith("s") else word, word[:-2]):
        if stem in _FAMILY_OF:
            return _FAMILY_OF[stem]
    return None


# A function word or measure unit ends the modifier run: "24 hours for vehicles" is no count.
_NOT_MODIFIER = frozenset(
    """
    of for to in on at by per with from and or than the a an
    hour minute day week month year percent dollar acre foot feet mile meter metre square
    cho của và hoặc với trong giờ phút ngày tuần tháng năm đồng mét
    """.split()  # noqa: SIM905 - a readable word list
)


def _noun_family(tail: str) -> str | None:
    """The counted-object family of a noun reached through at most three modifier words."""
    for token in re.findall(r"[\w'’]+", tail)[:3]:
        if family := _family(token):
            return family
        word = _word(token)
        if word in _NOT_MODIFIER or word.removesuffix("s") in _NOT_MODIFIER:
            return None
    return None


def _number(value: int) -> str:
    return rf"(?<![\w$€£¥.,#%-])(?:{value}|{value:,})(?![\w%]|[.,]\d)"


def _count_claims(text: str, value: int, family: str) -> list[re.Match[str]]:
    """Definite counts of ``value`` objects of ``family``: the number, then <= 3 words, a noun."""
    pattern = re.compile(_number(value) + r"(?P<tail>(?:[\s-]+[\w'’-]+){1,4})", re.UNICODE)
    return [
        match
        for match in pattern.finditer(text)
        if _noun_family(match["tail"]) == family
        and not _LABEL_BEFORE.search(text[: match.start()].rstrip())
    ]


def ambiguous_quantities(cited_text: str) -> list[tuple[int, str]]:
    """(candidate value, family) pairs that only a suspicious joined-number list supports.

    For each enumeration, the literal total, its rounded-down tens and its prefix without the
    joined digit are candidates, unless the cited text, outside every such list, states that
    value as a clear count or total of the same family.
    """
    matches = []
    for match in _ENUMERATION_RE.finditer(cited_text):
        total = int(match["n"].replace(",", ""))
        first = _SMALL_NUMBERS[match["a"].casefold()]
        family = _noun_family(match["tail"])
        if (
            family is not None
            and _SMALL_NUMBERS[match["b"].casefold()] == first + 1
            and first >= 2  # the inferred first size (joined last digit) is a positive size
            and total % 10 == first - 1
        ):
            matches.append((match, total, family))
    rest = cited_text
    for match, _total, _family_name in reversed(matches):
        rest = rest[: match.start()] + " … " + rest[match.end() :]
    candidates: list[tuple[int, str]] = []
    for _match, total, family in matches:
        for value in (total, total - total % 10, total // 10):
            explicit = _count_claims(rest, value, family) or any(
                _COUNT_CUE.search(rest[: m.start()]) for m in re.finditer(_number(value), rest)
            )
            if value >= 2 and not explicit and (value, family) not in candidates:
                candidates.append((value, family))
    return candidates


def mark_unclear_quantities(
    text: str, cited_text: str, output_language: str = "en"
) -> tuple[str, int]:
    """Replace only definite counts that rest on a joined-number list by a fixed marker."""
    marker = _QUANTITY_UNCLEAR[nd.validate_output_language(output_language)]
    spans: set[tuple[int, int]] = set()
    for value, family in ambiguous_quantities(cited_text):
        for match in _count_claims(text, value, family):
            spans.add((match.start(), match.start() + len(match.group()) - len(match["tail"])))
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + marker + text[end:]
    return text, len(spans)


@dataclass(frozen=True, slots=True)
class _Item:
    ref: str
    section: str
    position: int
    texts: tuple[tuple[str, str], ...]  # (text_ref, text); the original first


class SemanticAudit:
    """The final items of one long run, their immutable text refs and the projected source."""

    def __init__(
        self,
        source: dict[str, Any],
        document: dict[str, Any],
        *,
        max_unit_size: int = 1,
        output_language: str = "en",
    ) -> None:
        if not isinstance(source, dict) or not isinstance(source.get("segments"), list):
            raise _error("The semantic audit source is invalid.", "source_ids")
        self.output_language = nd.validate_output_language(output_language)
        self.source = copy.deepcopy(source)
        self.document = copy.deepcopy(document)
        self.max_unit_size = max_unit_size
        self._segment_text = {
            segment["id"]: str(segment.get("text", "")) for segment in source["segments"]
        }
        common = _common_vocabulary(source)
        items: list[_Item] = []
        self.quantity_marks = 0
        for section in _SECTION_NAMES:
            entries = self.document.get(section)
            if not isinstance(entries, list):
                raise _error("The semantic audit input is invalid.", "fields")
            for position, item in enumerate(entries):
                # A labelled literal source quote is never edited; the detail guards skip it.
                verbatim = any(label in item["text"] for label in _EXCERPT_LABELS)
                if not verbatim:
                    # Before any ref exists, so no text ref (not even '.o') can restore it.
                    item["text"], marks = mark_unclear_quantities(
                        item["text"], self._cited(item), self.output_language
                    )
                    self.quantity_marks += marks
                ref = f"i{len(items)}"
                texts = [(f"{ref}.o", item["text"])]
                legal = None if verbatim else reference_variant(item["text"], self.output_language)
                if legal is not None:
                    texts.append((f"{ref}.r", legal))
                for n, variant in enumerate(
                    anonymous_variants(item["text"], common, self._cited(item)), start=1
                ):
                    texts.append((f"{ref}.a{n}", variant))
                    combined = (
                        None if verbatim else reference_variant(variant, self.output_language)
                    )
                    if combined is not None:
                        texts.append((f"{ref}.a{n}r", combined))
                items.append(
                    _Item(ref, section, position, tuple(texts[: 1 + MAX_VARIANTS_PER_ITEM]))
                )
        if self.quantity_marks:  # markers are nonfactual, but the bounded document must hold
            nd.validate_document(
                self.document,
                dict(self.source),
                max_output_bytes=nd.MAX_OUTPUT_BYTES,
                max_unit_size=self.max_unit_size,
            )
        self.items = tuple(items)
        self._by_ref = {item.ref: item for item in items}
        self._text_refs = {
            text_ref: (item, text) for item in items for text_ref, text in item.texts
        }
        self._source_ids = nd._unique_source_ids(
            tuple(segment["id"] for segment in self.source["segments"])
        )

    def _cited(self, item: Mapping[str, Any]) -> str:
        return " ".join(self._segment_text.get(i, "") for i in item.get("source_ids", []))

    # ----- partitions -------------------------------------------------------------------------

    def partitions(self) -> tuple[SemanticAudit, ...]:
        """At most two balanced, contiguous, disjoint item views; fewer than two items: itself.

        A view shares this audit's full source and document and keeps the global refs; its
        schema, prompt and parse admit only its own refs and text refs.
        """
        if len(self.items) < MAX_AUDIT_PARTS:
            return (self,)
        half = (len(self.items) + 1) // 2
        return (self._view(self.items[:half]), self._view(self.items[half:]))

    def _view(self, items: tuple[_Item, ...]) -> SemanticAudit:
        view = copy.copy(self)  # parse deep-copies the shared document; nothing mutates it
        view.items = items
        view._by_ref = {item.ref: item for item in items}
        view._text_refs = {r: (item, text) for item in items for r, text in item.texts}
        return view

    def _language(self, output_language: str | None) -> str:
        """The constructor's language; a different one would mix marker and label languages."""
        if (
            output_language is not None
            and nd.validate_output_language(output_language) != self.output_language
        ):
            raise _error("The semantic audit output language changed.", "fields")
        return self.output_language

    def validated_patch(self, raw_output: object, *, output_language: str | None = None) -> dict:
        """This view's patch after the full parse guard; the canonical decoded JSON object."""
        self.parse(raw_output, output_language=output_language)
        return json.loads(raw_output)  # type: ignore[arg-type] - parse proved it is JSON text

    def combine_patches(
        self, parts: Sequence[tuple[SemanticAudit, dict]], *, output_language: str | None = None
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """Merge the validated patches of disjoint views covering every item; parse it once."""
        scopes = [set(view._by_ref) for view, _patch in parts]
        if sum(map(len, scopes)) != len(self._by_ref) or set().union(*scopes) != set(self._by_ref):
            raise _error("The semantic audit parts do not cover every item exactly once.")
        changes: list[Any] = []
        unresolved: list[Any] = []
        for (_view, patch), scope in zip(parts, scopes, strict=True):
            refs = [c.get("ref") for c in patch["changes"] if isinstance(c, dict)]
            if not {*refs, *patch["unresolved_refs"]} <= scope:
                raise _error("The semantic audit names a ref outside its partition.")
            changes += patch["changes"]
            unresolved += patch["unresolved_refs"]
        merged = json.dumps(
            {"audit_version": AUDIT_VERSION, "changes": changes, "unresolved_refs": unresolved},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(merged.encode("utf-8")) > MAX_SEMANTIC_OUTPUT_BYTES:
            raise _error("The merged semantic audit patch exceeds its output limit.", "size")
        return self.parse(merged, output_language=output_language)

    # ----- provider contract ------------------------------------------------------------------

    def schema(self) -> dict[str, Any]:
        refs = [item.ref for item in self.items]

        def obj(properties: dict[str, Any]) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            }

        change = obj(
            {
                "ref": {"type": "string", "enum": refs},
                "text_ref": {"type": "string", "enum": list(self._text_refs)},
                "add_source_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": nd._source_ref_limit(self.max_unit_size),
                },
                "clear_owner": {"type": "boolean"},
                "clear_deadline": {"type": "boolean"},
            }
        )
        return obj(
            {
                "audit_version": {"type": "integer", "enum": [AUDIT_VERSION]},
                "changes": {"type": "array", "maxItems": len(refs), "items": change},
                "unresolved_refs": {
                    "type": "array",
                    "maxItems": len(refs),
                    "items": {"type": "string", "enum": refs},
                },
            }
        )

    def prompt_payload(self) -> dict[str, Any]:
        def entry(item: _Item) -> dict[str, Any]:
            original = self.document[item.section][item.position]
            payload: dict[str, Any] = {
                "ref": item.ref,
                "section": item.section,
                "text_refs": [{"text_ref": r, "text": t} for r, t in item.texts],
                "source_ids": list(original["source_ids"]),
            }
            if item.section == "action_items":
                payload["owner"] = original.get("owner")
                payload["deadline_text"] = original.get("deadline_text")
            return payload

        return {
            "audit_version": AUDIT_VERSION,
            "meeting_date": self.source.get("meeting_date"),
            "source": nd.provider_source(dict(self.source)),
            "items": [entry(item) for item in self.items],
        }

    def build_prompt(
        self,
        *,
        output_language: str | None = None,
        invalid_output: str | None = None,
        budget: nd.NotesBudget | None = None,
    ) -> str:
        """The bounded audit prompt; raises when it, with its schema, cannot fit the budget."""
        output_language = self._language(output_language)
        prompt = (
            _INSTRUCTIONS[output_language]
            + "AUDIT_INPUT_JSON:\n"
            + json.dumps(
                self.prompt_payload(), ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
        )
        return nd._check_prompt(
            nd._repair_prompt(prompt, invalid_output, output_language=output_language),
            allowed_source_ids=self._source_ids,
            output_language=output_language,
            budget=budget,
            max_unit_size=self.max_unit_size,
            response_schema=self.schema(),
        )

    # ----- patch ------------------------------------------------------------------------------

    def _quarantine(self, item: _Item, output_language: str) -> dict[str, Any]:
        """The cited segments verbatim, under a fixed label; never the unverified wording."""
        ids = list(self.document[item.section][item.position]["source_ids"])
        texts = [self._segment_text[i] for i in ids]
        if not texts or any(not text.strip() for text in texts):
            raise _error("An unresolved claim has no source excerpt to quarantine.")
        excerpt = " ".join(
            [
                _NEEDS_REVIEW[output_language],
                _source_excerpt_text(texts[0], output_language),
                *(f"“{text}”" for text in texts[1:]),
            ]
        )
        return {"text": excerpt, "source_ids": ids}

    def parse(
        self, raw_output: object, *, output_language: str | None = None
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """Apply one sparse patch; an unknown, duplicate, cross-origin or conflicting ref fails.

        Each valid unresolved ref is quarantined as cited source excerpts.
        """
        output_language = self._language(output_language)
        if not isinstance(raw_output, str):
            raise _error("The semantic audit response must be text.", "json")
        try:
            patch = json.loads(raw_output)
        except (json.JSONDecodeError, RecursionError):
            raise _error("The semantic audit response must be valid JSON.", "json") from None
        if not isinstance(patch, dict) or set(patch) != {
            "audit_version",
            "changes",
            "unresolved_refs",
        }:
            raise _error("The semantic audit patch has invalid fields.", "fields")
        if type(patch["audit_version"]) is not int or patch["audit_version"] != AUDIT_VERSION:
            raise _error("The semantic audit patch version is invalid.", "version")
        unresolved, changes = patch["unresolved_refs"], patch["changes"]
        if not isinstance(unresolved, list) or not isinstance(changes, list):
            raise _error("The semantic audit patch has invalid fields.", "fields")
        if (
            any(not isinstance(ref, str) or ref not in self._by_ref for ref in unresolved)
            or len(set(unresolved)) != len(unresolved)
            or any(isinstance(c, dict) and c.get("ref") in unresolved for c in changes)
        ):
            raise _error("The semantic audit names an unknown, repeated or conflicting ref.")

        document = copy.deepcopy(self.document)
        scope = set(self._source_ids)
        seen: set[str] = set()
        counts = dict.fromkeys(
            (
                "changes",
                "anonymized",
                "cited_items",
                "added_ids",
                "cleared_owner",
                "cleared_deadline",
                "quarantined",
                "reference_unclear",
                "quantity_unclear",
            ),
            0,
        )
        counts["quantity_unclear"] = self.quantity_marks  # applied once, before any text ref
        for change in changes:
            if not isinstance(change, dict) or set(change) != {
                "ref",
                "text_ref",
                "add_source_ids",
                "clear_owner",
                "clear_deadline",
            }:
                raise _error("The semantic audit change has invalid fields.", "fields")
            ref, text_ref, ids = change["ref"], change["text_ref"], change["add_source_ids"]
            if not isinstance(ref, str) or ref not in self._by_ref or ref in seen:
                raise _error("The semantic audit names an unknown or repeated ref.")
            seen.add(ref)
            target = self._text_refs.get(text_ref) if isinstance(text_ref, str) else None
            if target is None or target[0].ref != ref:
                raise _error("The semantic audit names an unknown or cross-origin text ref.")
            clear_owner, clear_deadline = change["clear_owner"], change["clear_deadline"]
            if type(clear_owner) is not bool or type(clear_deadline) is not bool:
                raise _error("The semantic audit change has invalid fields.", "fields")
            item = self._by_ref[ref]
            if (clear_owner or clear_deadline) and item.section != "action_items":
                raise _error("Only an action item has an owner or deadline.", "action_fields")
            if (
                not isinstance(ids, list)
                or any(not isinstance(i, str) or i not in scope for i in ids)
                or len(set(ids)) != len(ids)
            ):
                raise _error("The semantic audit cites an invalid source ID.", "source_ids")
            published = document[item.section][item.position]
            added = [i for i in ids if i not in published["source_ids"]]
            published["text"] = target[1]
            published["source_ids"] = [*published["source_ids"], *added]
            if clear_owner:
                published["owner"] = None
            if clear_deadline:
                published["deadline_text"] = None
                published["deadline_date"] = None
            counts["changes"] += 1
            suffix = text_ref.removeprefix(f"{ref}.")
            counts["anonymized"] += suffix.startswith("a")
            counts["reference_unclear"] += suffix.endswith("r")
            counts["cited_items"] += bool(added)
            counts["added_ids"] += len(added)
            counts["cleared_owner"] += clear_owner
            counts["cleared_deadline"] += clear_deadline
        # Remove by original position after every change, so no index shifts under another.
        quarantined = [item for item in self.items if item.ref in unresolved]
        removed = {(item.section, item.position) for item in quarantined}
        for section in _SECTION_NAMES:
            document[section] = [
                entry
                for position, entry in enumerate(document[section])
                if (section, position) not in removed
            ]
        document["confirmation_items"] += [
            self._quarantine(item, output_language) for item in quarantined
        ]
        counts["quarantined"] = len(quarantined)
        validated = nd.validate_document(
            document,
            dict(self.source),
            max_output_bytes=nd.MAX_OUTPUT_BYTES,
            max_unit_size=self.max_unit_size,
        )
        return validated, counts


_INSTRUCTIONS = {
    "en": (
        "Final source-binding audit of meeting notes. Return one sparse JSON patch, never a "
        "document or new wording. AUDIT_INPUT_JSON.source is the full transcript; items are the "
        "final notes assigned to this request. Each item has immutable text_refs: '.o' is its "
        "original text, '.aN' an "
        "anonymous variant that only drops a leading speaker or group attribution or a leading "
        "qualifier, '.r' only replaces labelled secondary legal reference identifiers (code, "
        "section, article or clause numbers) with a fixed reference-unclear marker, and '.aNr' "
        "does both. Bracketed unclear markers were set by code, are not claims and need no "
        "support. Check only material bindings: every named person, role, organization, "
        "number, duration, date, vote, owner, deadline and commitment must be stated by the "
        "item's cited segments. Check every listed item in every section, including overview "
        "titles, headings and confirmation_items; a section or heading never makes an "
        "unsupported fact acceptable, and only a needs-review literal source quote is exempt. "
        "A nominee, candidate or other name in the cited segments does not identify the "
        "speaker, even when the item calls the statement that person's vision, remarks or "
        "testimony. Supporting, endorsing, voting for or thanking is not sponsoring, "
        "introducing or bringing forward legislation; never move one actor's role to another. "
        "Every number, quantity, count and bill, ordinance, statute or section identifier must "
        "appear in the cited segments; never split, merge or reinterpret garbled transcribed "
        "digits or a spoken list to create a count, and never guess, correct or complete "
        "identifier digits. When only a secondary legal reference is uncertain while the main "
        "record, outcome, vote and date are supported, choose the '.r' or '.aNr' variant and "
        "keep the item; unresolved_refs is for an unsupported main fact. When such a fact is "
        "ambiguous and no variant drops it, list the ref "
        "in unresolved_refs. A procedural approval, requirement or commitment needs its own "
        "cited statement; when that statement continues in the next segment, add that "
        "segment's source ID. Never supply background legal or procedural assumptions. "
        "To fix an item, list one change for its ref: keep the original "
        "text_ref when its wording is fully supported and put the source IDs that state the "
        "missing fact in add_source_ids (code adds them to the cited IDs; already cited IDs are "
        "ignored); choose an anonymous variant only when a leading name or qualifier is not "
        "supported. When attribution is uncertain and an anonymous variant exists, choose it; "
        "an unknown actor alone is never a reason for unresolved_refs. A name mentioned "
        "elsewhere in the source, such as on a roster, in a roll "
        "call, as a nominee or as a thanked person, does not identify who made a statement. A "
        "speaker is identified only by an explicit self-introduction, recognition or handoff "
        "immediately next to the cited statement; cite that segment, otherwise choose the "
        "anonymous variant. Never infer a speaker from speaker labels. An organization modifier "
        "cannot be borrowed from an unrelated segment: two separate mentions are not one "
        "combined name. Set clear_owner or clear_deadline when an action owner or deadline is "
        "not stated in its cited or appended segments. Leave fully supported items unlisted; "
        "unlisted items are kept unchanged. When no text_ref, citation or clear can fix a "
        "material unsupported claim, list its ref in unresolved_refs: code then removes that "
        "item's wording and publishes its cited source segments verbatim as a needs-review "
        "confirmation item, so a fixable item should be fixed instead. Never list a ref in both "
        "changes and unresolved_refs. Use only refs, text_refs and source IDs from the input.\n"
    ),
    "vi": (
        "Kiểm tra cuối cùng việc gắn nguồn của biên bản họp. Trả về một bản vá JSON thưa, không "
        "trả về tài liệu hay câu chữ mới. AUDIT_INPUT_JSON.source là toàn bộ bản ghi; items là "
        "các mục biên bản cuối được giao cho yêu cầu này. Mỗi mục có text_refs bất biến: '.o' "
        "là văn bản gốc, '.aN' là "
        "biến thể ẩn danh chỉ bỏ phần ghi người nói hoặc nhóm ở đầu, hoặc bổ ngữ ở đầu; '.r' chỉ "
        "thay số hiệu tham chiếu pháp lý phụ có nhãn (số bộ luật, điều, mục, khoản) bằng một dấu "
        "tham chiếu chưa rõ cố định, và '.aNr' làm cả hai. Các dấu chưa rõ trong ngoặc vuông do "
        "mã đặt, không phải khẳng định và không cần nguồn. Chỉ kiểm "
        "tra các gắn kết trọng yếu: mọi người được nêu tên, vai trò, tổ chức, con số, thời lượng, "
        "ngày, kết quả biểu quyết, người phụ trách, hạn chót và cam kết phải được segment được "
        "trích nêu rõ. Kiểm tra mọi mục được liệt kê trong mọi phần, kể cả tiêu đề tổng quan, "
        "đề mục và confirmation_items; một phần hay đề mục không bao giờ làm cho dữ kiện không "
        "được hỗ trợ trở nên chấp nhận được, chỉ trích dẫn nguyên văn cần xem lại mới được "
        "miễn. Một người được đề cử, ứng viên hay tên khác trong các segment được trích không "
        "xác định người nói, kể cả khi mục gọi phát biểu đó là tầm nhìn, nhận xét hay lời khai "
        "của người đó. Ủng hộ, tán thành, bỏ phiếu thuận hay cảm ơn không phải là bảo trợ, giới "
        "thiệu hay đưa ra dự luật; không bao giờ chuyển vai trò của người này sang người khác. "
        "Mọi con số, số lượng, phép đếm và số hiệu dự luật, sắc lệnh, luật hay điều khoản phải "
        "xuất hiện trong segment được trích; không bao giờ tách, gộp hay diễn giải lại chữ số "
        "bị ghi sai hoặc một danh sách được đọc để tạo ra một phép đếm, và không bao giờ đoán, "
        "sửa hay điền thêm chữ số của số hiệu. Khi chỉ tham chiếu pháp lý phụ không chắc chắn "
        "còn hồ sơ chính, kết quả, biểu quyết và ngày được hỗ trợ, hãy chọn biến thể '.r' hoặc "
        "'.aNr' và giữ mục; unresolved_refs dành cho dữ kiện chính không được hỗ trợ. Khi dữ "
        "kiện như vậy mơ hồ và không biến thể nào "
        "bỏ được nó, liệt kê ref đó trong unresolved_refs. Một phê duyệt, yêu cầu hay cam kết "
        "theo thủ tục cần chính phát biểu được trích của nó; khi phát biểu đó tiếp tục ở "
        "segment kế tiếp, hãy thêm source ID của segment đó. Không bao giờ tự bổ sung giả định "
        "pháp lý hay thủ tục nền. "
        "Để sửa một mục, liệt kê một thay đổi cho ref của nó: giữ text_ref gốc khi "
        "câu chữ được hỗ trợ đầy đủ và đưa source ID nêu dữ kiện còn thiếu vào add_source_ids "
        "(mã cộng chúng vào ID đã trích; ID đã trích được bỏ qua); chỉ chọn biến thể ẩn danh khi "
        "tên hoặc bổ ngữ ở đầu không được hỗ trợ. Khi việc quy người nói không chắc chắn và có "
        "biến thể ẩn danh, hãy chọn biến thể đó; chỉ vì không rõ người thực hiện thì không bao "
        "giờ là lý do đưa vào unresolved_refs. Một cái tên xuất hiện ở chỗ "
        "khác trong nguồn, như trong danh sách, điểm danh biểu quyết, người được đề cử hoặc người "
        "được cảm ơn, không cho biết ai đã phát biểu. Người nói chỉ được xác định bằng lời tự giới "
        "thiệu, lời mời hoặc lời chuyển lượt rõ ràng ngay cạnh phát biểu được trích; hãy trích "
        "segment đó, nếu không thì chọn biến thể ẩn danh. Không bao giờ suy ra người nói từ nhãn "
        "người nói. Không được mượn bổ ngữ tổ chức từ một segment không liên quan: hai lần nhắc "
        "riêng không tạo thành một tên ghép. Đặt clear_owner hoặc clear_deadline khi người phụ "
        "trách hoặc hạn chót không được nêu trong segment được trích hoặc bổ sung. Không liệt kê "
        "các mục đã được hỗ trợ đầy đủ; mục không liệt kê được giữ nguyên. Khi không text_ref, "
        "trích dẫn hay lệnh xóa nào sửa được một khẳng định trọng yếu không được hỗ trợ, liệt kê "
        "ref đó trong unresolved_refs: mã sẽ bỏ câu chữ của mục đó và đăng nguyên văn các segment "
        "được trích thành một mục cần xác nhận lại, nên mục sửa được thì hãy sửa. Không bao giờ "
        "liệt kê một ref ở cả changes và unresolved_refs. Chỉ dùng ref, text_ref và source ID có "
        "trong đầu vào.\n"
    ),
}
