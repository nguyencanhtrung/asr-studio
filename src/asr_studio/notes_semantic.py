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


@dataclass(frozen=True, slots=True)
class _Item:
    ref: str
    section: str
    position: int
    texts: tuple[tuple[str, str], ...]  # (text_ref, text); the original first


class SemanticAudit:
    """The final items of one long run, their immutable text refs and the projected source."""

    def __init__(
        self, source: dict[str, Any], document: dict[str, Any], *, max_unit_size: int = 1
    ) -> None:
        if not isinstance(source, dict) or not isinstance(source.get("segments"), list):
            raise _error("The semantic audit source is invalid.", "source_ids")
        self.source = copy.deepcopy(source)
        self.document = copy.deepcopy(document)
        self.max_unit_size = max_unit_size
        self._segment_text = {
            segment["id"]: str(segment.get("text", "")) for segment in source["segments"]
        }
        common = _common_vocabulary(source)
        items: list[_Item] = []
        for section in _SECTION_NAMES:
            entries = document.get(section)
            if not isinstance(entries, list):
                raise _error("The semantic audit input is invalid.", "fields")
            for position, item in enumerate(entries):
                ref = f"i{len(items)}"
                texts = [(f"{ref}.o", item["text"])]
                texts += [
                    (f"{ref}.a{n}", variant)
                    for n, variant in enumerate(
                        anonymous_variants(item["text"], common, self._cited(item)), start=1
                    )
                ]
                items.append(_Item(ref, section, position, tuple(texts)))
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

    def validated_patch(self, raw_output: object, *, output_language: str = "en") -> dict:
        """This view's patch after the full parse guard; the canonical decoded JSON object."""
        self.parse(raw_output, output_language=output_language)
        return json.loads(raw_output)  # type: ignore[arg-type] - parse proved it is JSON text

    def combine_patches(
        self, parts: Sequence[tuple[SemanticAudit, dict]], *, output_language: str = "en"
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
        output_language: str = "en",
        invalid_output: str | None = None,
        budget: nd.NotesBudget | None = None,
    ) -> str:
        """The bounded audit prompt; raises when it, with its schema, cannot fit the budget."""
        output_language = nd.validate_output_language(output_language)
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
        self, raw_output: object, *, output_language: str = "en"
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """Apply one sparse patch; an unknown, duplicate, cross-origin or conflicting ref fails.

        Each valid unresolved ref is quarantined as cited source excerpts.
        """
        output_language = nd.validate_output_language(output_language)
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
            ),
            0,
        )
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
            counts["anonymized"] += text_ref != f"{ref}.o"
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
        "qualifier. Check only material bindings: every named person, role, organization, "
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
        "identifier digits. When such a fact is ambiguous and no variant drops it, list the ref "
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
        "biến thể ẩn danh chỉ bỏ phần ghi người nói hoặc nhóm ở đầu, hoặc bổ ngữ ở đầu. Chỉ kiểm "
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
        "sửa hay điền thêm chữ số của số hiệu. Khi dữ kiện như vậy mơ hồ và không biến thể nào "
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
