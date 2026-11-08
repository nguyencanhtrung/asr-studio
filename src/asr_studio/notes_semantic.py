"""One bounded source-binding audit for registered-Gemini long notes, as sparse immutable refs.

The pass may split the final items into at most eight balanced, disjoint partitions; each one
sees and may patch only its own items under their global refs. A view's prompt shows only its
visible source: every segment its items cite, the read-only strict context candidates of those
items and one bounded neighbour on each side of each cited run, under their global IDs and order,
with explicit original positions and omitted ranges. Only visible IDs may be appended; the
deterministic guards still read the full source. Every
part returns an explicit Boolean decision for each of its refs (``verified``), so an omitted
item fails the part. The validated part patches are merged and applied once, so every item is
audited exactly once. The verified map proves decision coverage only, never semantic truth.

The model never writes text. Each final item has an original text ref and, at most, a few
anonymous variants that code derived: exact suffixes of the original that drop only a leading
proper-name attribution or leading proper qualifiers, and a bounded variant that replaces one
trailing secondary responsibility phrase ("developed by X") with a fixed marker. A sparse patch
selects a text ref, adds source IDs (a union onto the cited IDs) and may clear an action owner
or deadline. Unlisted items stay unchanged; no item can be dropped, duplicated, moved or
invented, and any unresolved ref is quarantined: its unverified wording is removed and its cited
source segments are published verbatim as one needs-review confirmation item.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from asr_studio import notes_document as nd
from asr_studio.notes_audit import _source_excerpt_text
from asr_studio.notes_context import (
    _HEADER,
    _TOPIC_BREAK,
    ATTRIBUTION_FORMS,
    MAX_FORWARD_SECONDS,
    link_antecedent_context,
    unverified_owner,
    unverified_speakers,
    unverified_vote_names,
)
from asr_studio.notes_outcomes import outcome_core_candidate

AUDIT_VERSION = 2
MAX_SEMANTIC_OUTPUT_BYTES = 64_000  # one part's response and the merged patch alike
MAX_AUDIT_PARTS = 8  # v38: 4 -> 8 shortens the slowest part's sequential reasoning
MAX_LEADING_NAME_TOKENS = 4
MAX_VARIANTS_PER_ITEM = 4
MAX_RESPONSIBILITY_OPTIONS = 2  # '.p' options per item, after the capped existing ones
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
_ATTRIBUTION_EN = (
    frozenset(
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
    | ATTRIBUTION_FORMS
)  # every tense: "states", "urges", "is arguing"
# Lower-case-in-source role words that may lead an attribution: "Councilmember Lee states".
_LEAD_ROLES = frozenset(
    """
    councilmember councilman councilwoman chair chairman chairwoman chairperson president mayor
    commissioner director mr mrs ms dr judge
    """.split()  # noqa: SIM905 - a readable word list
)
_SPEECH_NOUNS = frozenset(
    """
    testimony testimonies comment comments remark remarks statement statements question questions
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
    run, roles = _leading_run(tokens, common)
    if run == roles:
        return []  # no name, or only generic role words: never dropped
    starts: list[int] = []
    if tokens[run - 1].group().endswith(":") or _starts_attribution(text[tokens[run].start() :]):
        starts.append(tokens[run].start())
    cited = " ".join(_word(token) for token in _TOKEN_RE.findall(cited_text))
    if run > 1 and _phrase(tokens, 0, run) not in cited:
        starts.extend(
            tokens[k].start()
            for k in range(run - 1, roles, -1)  # shortest kept tail first; never only a role
            if f" {_phrase(tokens, k, run)} " in f" {cited} "
        )
    variants: list[str] = []
    for start in starts:
        variant = _suffix(text, start)
        if _protected(text[:start]) or not variant.strip() or variant in variants:
            continue
        variants.append(variant)
    return variants[:MAX_VARIANTS_PER_ITEM]


def _leading_run(tokens: list[re.Match[str]], common: frozenset[str]) -> tuple[int, int]:
    """Leading name tokens and how many of them are lower-case-in-source role words."""
    run = roles = 0
    while run < min(len(tokens) - 1, MAX_LEADING_NAME_TOKENS):
        token = tokens[run].group()
        if not _proper(token, common):
            if run != roles or _word(token) not in _LEAD_ROLES:
                break
            roles += 1
        run += 1
        if token.endswith((":", ",")):
            break  # "Name: claim" — the colon ends the attribution
    return run, roles


def attributed_speakers(text: str, common: frozenset[str] = frozenset()) -> list[tuple[str, bool]]:
    """(name, titled) for each person the text says spoke: a leading attribution or a name
    before a speech noun ("Lee testimony:", "Lee's comments"). The name is its last token.
    """
    tokens = list(_TOKEN_RE.finditer(text))
    found: list[tuple[str, bool]] = []
    run, roles = _leading_run(tokens, common)
    if (
        run > roles
        and _word(tokens[run - 1].group()) not in _ATTRIBUTION_EN  # "Argument: ..." names no one
        and (
            tokens[run - 1].group().endswith(":")
            or _starts_attribution(text[tokens[run].start() :])
        )
    ):
        found.append((_EDGE_RE.sub("", _POSSESSIVE_RE.sub("", tokens[run - 1].group())), roles > 0))
    for index, token in enumerate(tokens):
        if index == 0 or _word(token.group()) not in _SPEECH_NOUNS:
            continue
        start = index
        while start > max(index - MAX_LEADING_NAME_TOKENS, 0) and _proper(
            tokens[start - 1].group(), common
        ):
            start -= 1
        if start == index or tokens[index - 1].group().endswith((":", ",", ".")):
            continue
        titled = start > 0 and _word(tokens[start - 1].group()) in _LEAD_ROLES
        name = _EDGE_RE.sub("", _POSSESSIVE_RE.sub("", tokens[index - 1].group()))
        found.append((name, titled))
    return list(dict.fromkeys(found))


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
    r"\b(?:(?:bill|cb|ordinance|resolution|statute|act|section|article|clause)\s+"
    r"(?:no\.?\s*)?\d+|amend(?:s|ed|ing)?|(?:statutory|legal)\s+(?:code|references?)|"
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
    pattern = re.compile(_number(value) + r"(?=(?P<tail>(?:[\s-]+[\w'’-]+){1,4}))", re.UNICODE)
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
            spans.add(match.span())
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + marker + text[end:]
    return text, len(spans)


# ----- approximate scale wording (quantity marker) --------------------------------------------

_SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 10**9, "dozen": 12}
_VAGUE_BEFORE = re.compile(
    r"(?i:\b(?:a\s+few|few|several|a\s+couple(?:\s+of)?|couple\s+of|some|many|"
    r"about|around|roughly|approximately|nearly|almost|over|under|up\s+to|more\s+than|"
    r"less\s+than|fewer\s+than))\s*$"
)
_COUNT_WORD = r"(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)"


def _scale_uses(text: str, scale: str) -> list[tuple[re.Match[str], bool, str]]:
    """(match, vague, next word) for each singular `scale`, a number word before it included."""
    uses = []
    pattern = rf"(?i:(?:\b{_COUNT_WORD}\s+)?\b{scale}\b)(?=\s+(?:of\s+)?([\w'’-]+))"
    for match in re.finditer(pattern, text):
        vague = bool(_VAGUE_BEFORE.search(text[: match.start()]))
        uses.append((match, vague, match.group(1).casefold()))
    return uses


def _scale_amount(match: re.Match[str], value: int) -> int:
    coefficients = {"a": 1, "an": 1, "ten": 10, **_SMALL_NUMBERS}
    words = match.group(0).casefold().split()
    coefficient = words[0] if len(words) > 1 else "one"
    return value * int(coefficients.get(coefficient, coefficient))


def _exact_scale_count(
    cited_text: str, unit: str, number: int, exact_counts: set[tuple[str, int]]
) -> bool:
    if (unit, number) in exact_counts:
        return True
    pattern = rf"{_number(number)}(?=\s+{re.escape(unit)}\b)"
    return any(
        not _VAGUE_BEFORE.search(cited_text[: m.start()])
        for m in re.finditer(pattern, cited_text, re.IGNORECASE)
    )


def mark_approximate_quantities(
    text: str, cited_text: str, output_language: str = "en"
) -> tuple[str, int]:
    """Mark a definite scale count ("hundred feet") that the cited text states only vaguely.

    A unit qualifies when the cited text says it only with an approximation ("a few hundred
    feet") and never as a plain count; the item's unqualified count of that unit (word or digits)
    becomes the fixed quantity marker.
    """
    marker = _QUANTITY_UNCLEAR[nd.validate_output_language(output_language)]
    spans: set[tuple[int, int]] = set()
    for scale, value in _SCALES.items():
        source = _scale_uses(cited_text, scale)
        vague_units = {unit for _m, vague, unit in source if vague}
        exact_counts = {(unit, _scale_amount(m, value)) for m, vague, unit in source if not vague}
        for unit in vague_units:
            for match, vague, item_unit in _scale_uses(text, scale):
                if (
                    not vague
                    and item_unit == unit
                    and not _exact_scale_count(
                        cited_text, unit, _scale_amount(match, value), exact_counts
                    )
                ):
                    spans.add(match.span())
            if not _exact_scale_count(cited_text, unit, value, exact_counts):
                plain = rf"{_number(value)}(?=\s+{re.escape(unit)}\b)"
                spans.update(
                    m.span()
                    for m in re.finditer(plain, text, re.IGNORECASE)
                    if not _VAGUE_BEFORE.search(text[: m.start()])
                )
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + marker + text[end:]
    return text, len(spans)


# ----- trailing secondary responsibility (responsibility marker) ------------------------------

_RESPONSIBILITY_UNCLEAR = {"en": "[responsibility unclear]", "vi": "[vai trò chưa rõ]"}
_RESPONSIBILITY_RE = re.compile(
    r"(?<!\w)(?:(?:developed|built|funded|operated|managed|delivered|supported|sponsored|owned|led)"
    r"(?:\s+by)?|(?:phát\s+triển|xây\s+dựng|tài\s+trợ|vận\s+hành|quản\s+lý|hỗ\s+trợ|bảo\s+trợ|"
    r"sở\s+hữu|dẫn\s+dắt)\s+bởi)(?=\s)",
    re.IGNORECASE | re.UNICODE,
)
_ACTOR_JOINS = frozenset({"and", "&", "of", "the", "for", "và", "của"})
# Right before the verb these make it the main predicate, or negate or condition it.
_PREDICATE_LEADS = frozenset(
    """
    is are was were be been being am will would shall should may might must can could has have
    had to not never no if unless whether được bị sẽ đã đang không chưa nếu
    """.split()  # noqa: SIM905 - a readable word list
)
_QUANTITY_WORDS = frozenset(
    """
    zero one two three four five six seven eight nine ten eleven twelve twenty thirty forty fifty
    sixty seventy eighty ninety hundred thousand million billion dozen
    """.split()  # noqa: SIM905 - a readable word list
)
# A vote, an approval or a record is a primary fact: its responsibility is never masked.
_PRIMARY_RECORD_RE = re.compile(
    r"(?<!\w)(?:vot(?:e|es|ed|ing)|approv\w*|adopt\w*|pass(?:es|ed)?|ordinances?|resolutions?|"
    r"bills?|motions?|biểu\s+quyết|phê\s+duyệt|thông\s+qua|nghị\s+quyết|dự\s+luật|sắc\s+lệnh)(?!\w)",
    re.IGNORECASE | re.UNICODE,
)


def _actor_end(text: str, start: int, common: frozenset[str]) -> int | None:
    """The end of a proper actor phrase starting at ``start`` (trailing punctuation kept out)."""
    end = None
    for count, token in enumerate(_TOKEN_RE.finditer(text, start)):
        raw = token.group()
        if count >= 12:
            return None  # too long to be one bounded actor phrase
        # The first actor word is a proper name; later capitalized words continue its name even
        # when the source also writes them lower-case ("Harbor Housing Services").
        if _proper(raw, common if end is None else frozenset()):
            end = token.start() + len(raw.rstrip(",.;:!?)]”’\"'"))
            if raw[-1:] in ".;:!?)":
                break
        elif (
            any(c.isdigit() for c in raw)
            and end is not None
            and not text[end : token.start()].strip()
        ):
            return None  # "District 7": a numbered actor is never partly masked
        elif _word(raw) not in _ACTOR_JOINS or raw[-1:] in ",.;:":
            break
    return end


def responsibility_variant(
    text: str, common: frozenset[str] = frozenset(), output_language: str = "en"
) -> str | None:
    """The text with one trailing secondary responsibility phrase replaced by a fixed marker.

    Offered only for exactly one "<verb> [by] <Proper Actor>" phrase that follows a
    quantitative core (a number in the same sentence before it) and is not the main predicate,
    negated or conditioned. The phrase holds only the verb, "by" and the proper actor, so every
    digit, date, negation, modality and other word outside it stays byte-for-byte. A quote, a
    vote, an approval or a record item gets no variant.
    """
    marker = _RESPONSIBILITY_UNCLEAR[nd.validate_output_language(output_language)]
    if any(quote in text for quote in '"“”') or _PRIMARY_RECORD_RE.search(text):
        return None
    spans: list[tuple[int, int]] = []
    for match in _RESPONSIBILITY_RE.finditer(text):
        end = _actor_end(text, match.end(), common)
        if end is None:
            continue
        sentence = re.split(r"[.;!?](?:\s|$)", text[: match.start()])[-1]
        before = _TOKEN_RE.findall(sentence)
        if (
            not before
            or _word(before[-1]) in _PREDICATE_LEADS
            or before[-1].casefold().endswith("n't")
            or not any(
                any(c.isdigit() for c in token) or _word(token) in _QUANTITY_WORDS
                for token in before
            )
            or _protected(text[match.start() : end])  # defense in depth: never true today
        ):
            continue
        spans.append((match.start(), end))
    if len(spans) != 1:
        return None  # none, or ambiguous: which actor's role is unclear is not decidable here
    start, end = spans[0]
    return text[:start] + marker + text[end:]


# ----- confirmation-item evidence guards (official roles, modified record IDs) ----------------

# Canonical official roles and their literal cues; "chủ tịch" reads as president or chair.
_ROLE_CUES = {
    "president": r"president|chủ\s+tịch",
    "chair": r"chair(?:man|woman|person)?|chủ\s+tịch|chủ\s+tọa",
    "mayor": r"mayor|thị\s+trưởng",
    "councilmember": r"council\s*-?\s*(?:member|woman|man)|nghị\s+viên",
    "commissioner": r"commissioner|ủy\s+viên",
    "director": r"director|giám\s+đốc",
}
_ROLE_RE = {
    role: re.compile(rf"(?<!\w)(?:{cue})s?(?!\w)", re.IGNORECASE | re.UNICODE)
    for role, cue in _ROLE_CUES.items()
}


def unverified_roles(text: str, cited_text: str) -> set[str]:
    """The official-role words of the text whose role no cited segment literally states.

    A literal cue is necessary, not sufficient; speaker labels and other mentions of the same
    person never count, and full entailment stays with the model audit.
    """
    stated = {role for role, cue in _ROLE_RE.items() if cue.search(cited_text)}
    words = {m.group(0).lower() for cue in _ROLE_RE.values() for m in cue.finditer(text)}
    # A word shared by two roles (chủ tịch) is supported by either one.
    return {
        word
        for word in words
        if not any(cue.fullmatch(word) and role in stated for role, cue in _ROLE_RE.items())
    }


# A group actor leading the item ("Council goal:", "The committees will"); a group word later
# in the text is a common noun and out of scope. "Council" never matches "councilmember".
_GROUP_CUES = {
    "council": r"council(?!\s*-?\s*(?:members?|wom[ae]n|m[ae]n|persons?)\b)|hội\s+đồng",
    "committee": r"committees?|ủy\s+ban",
    "board": r"boards?",
    "commission": r"commissions?",
}
_GROUP_LEAD_RE = re.compile(
    rf"^\s*(?:the\s+)?(?:city\s+)?(?P<group>{'|'.join(_GROUP_CUES.values())})(?:['’]s?)?(?!\w)"
    r"(?!\s+(?:bills?|resolutions?|ordinances?|files?|members?)\b)",  # a record label, not an actor
    re.IGNORECASE | re.UNICODE,
)


def unverified_group_actor(text: str, cited_text: str) -> str | None:
    """The leading group actor of the text when no cited segment literally names that group."""
    lead = _GROUP_LEAD_RE.match(text)
    if lead is None:
        return None
    for cue in _GROUP_CUES.values():
        if re.fullmatch(cue, lead["group"], re.IGNORECASE | re.UNICODE):
            found = re.search(rf"(?<!\w)(?:{cue})(?!\w)", cited_text, re.IGNORECASE | re.UNICODE)
            return None if found else lead["group"]
    return None


# Completion of a deliverable, and a partial-progress qualifier right after the verb.
_COMPLETION_RE = re.compile(
    r"(?<!\w)(?:complet(?:e|es|ed|ing|ion)|finish(?:es|ed|ing)?|finali[sz](?:e|es|ed|ing|ation)"
    r"|hoàn\s+(?:thành|tất))(?!\w)",
    re.IGNORECASE | re.UNICODE,
)
_PARTIAL_RE = re.compile(
    r"\s+(?:\w+\s+){0,2}?(?:(?:much|most|some|part|parts|portion|portions|half|a\s+lot|a\s+bit)"
    r"\s+of|(?:the\s+)?(?:work|steps?|tasks?)\s+(?:needed|necessary|required)\s+to"
    r"|work\s+toward|progress|một\s+phần|phần\s+lớn|một\s+số|bước\s+đầu|phần\s+việc)",
    re.IGNORECASE | re.UNICODE,
)


def _completions(text: str) -> list[bool]:
    """Whether each completion statement of the text carries a partial-progress qualifier."""
    return [_PARTIAL_RE.match(text, m.end()) is not None for m in _COMPLETION_RE.finditer(text)]


def strengthened_completion(text: str, cited_text: str) -> bool:
    """The text states an unqualified completion while every cited completion is partial.

    "will complete much of the work needed to develop X" never supports "completion of X";
    a pool with no completion statement, or with one unqualified, is left to the model audit.
    """
    claims, cited = _completions(text), _completions(cited_text)
    return bool(claims) and not all(claims) and bool(cited) and all(cited)


# A nominee credential list, and a source clause where the speaker owns the work in first person.
_CREDENTIAL_RE = re.compile(
    r"\b(?:credentials?|qualifications?|experience|background|track\s+record|r[ée]sum[ée])\b"
    r"[^.;:]*?\b(?:include[sd]?|including|are|is)\b|\b(?:credentials?|qualifications?):",
    re.IGNORECASE,
)
_FIRST_PERSON_WORK = re.compile(
    r"\b(?:I|we)\s+(?:\w+\s+)?(?:initiated|started|launched|led|created|began|introduced|"
    r"sponsored|championed|built|drafted|worked)\b|\b(?:my|our)\s+(?:committee|office|efforts?|"
    r"work|team|initiative|legislation|bill|staff)\b",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"\b(?:not|never|no)\b|n['’]t\b", re.IGNORECASE)
# A clause about a third person ("he's worked ... similar to our work") is mixed attribution.
_THIRD_PERSON_RE = re.compile(r"\b(?:he|she|they|him|her|them|his|hers|their)\b", re.IGNORECASE)
_FIRST_PERSON_ITEM = re.compile(r"\b(?:I|we|my|our)\b", re.IGNORECASE)
_STEM_STOP = frozenset("and the of for on at in to with from by a an work".split())  # noqa: SIM905


def _stems(text: str) -> set[str]:
    """Lower-case content-word stems: "rates"/"rate", "sizing"/"size" share one."""
    stems = set()
    for word in re.findall(r"[^\W_]+", text.lower()):
        if word in _STEM_STOP or len(word) < 3:
            continue
        for suffix in ("ing", "ed", "es", "s", "e"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[: -len(suffix)]
                break
        stems.add(word)
    return stems


def speaker_work_as_credential(text: str, cited_text: str) -> bool:
    """A credential list entry that the cited pool states only as the speaker's own work.

    The item must assert a credential or qualification list and hold no first person itself.
    An entry counts when at least two of its content stems, and at least half, co-occur only in
    a non-negated cited clause with explicit first-person ownership ("we initiated", "my
    committee") and no third-person word, and in no other cited clause. Third-person or other
    cited credentials, negated or unrelated first-person talk never count; full entailment
    stays with the model audit.
    """
    lead = _CREDENTIAL_RE.search(text)
    if lead is None or _FIRST_PERSON_ITEM.search(text):
        return False
    clauses = [c for c in re.split(r"[.!?;]+", cited_text) if c.strip()]
    owned = [
        _stems(c)
        for c in clauses
        if _FIRST_PERSON_WORK.search(c)
        and not _NEGATION_RE.search(c)
        and not _THIRD_PERSON_RE.search(c)
    ]
    other = [_stems(c) for c in clauses if not _FIRST_PERSON_WORK.search(c)]
    for entry in re.split(r",\s*(?:and\s+)?|\s+and\s+", text[lead.end() :]):
        stems = _stems(entry)
        need = max(2, (len(stems) + 1) // 2)
        if any(len(stems & c) >= need for c in owned) and not any(
            len(stems & c) >= 2 for c in other
        ):
            return True
    return False


_RECORD_KINDS = {
    "ordinance": r"ordinance|sắc\s+lệnh",
    "resolution": r"resolution|nghị\s+quyết",
    "bill": r"(?:council\s+)?bill|dự\s+luật",
}
_MODIFY = r"amend\w*|repeal\w*|replac\w*|supersed\w*|sửa\s+đổi|bãi\s+bỏ|thay\s+thế"
# A plain or 3-digit-grouped integer ("480210", "480 216", "480,216"); never a plural label.
_RECORD_INTEGER = r"(?:\d{1,3}(?:[ ,]\d{3})+|\d{4,})"
_RECORD_NUMBER = rf"(?<![\w.,])(?P<n>{_RECORD_INTEGER})(?![\w]|[.,]\d)"
_MODIFIED_RECORD_RE = {
    kind: re.compile(
        rf"(?<!\w)(?:{_MODIFY})\s+(?:(?:to|of|the|existing)\s+){{0,2}}(?:{label})\s+"
        rf"(?:(?:no\.?|number|số)\s*|#\s*)?{_RECORD_NUMBER}",
        re.IGNORECASE | re.UNICODE,
    )
    for kind, label in _RECORD_KINDS.items()
}
# "Ordinance 480210 and 480216": one label, further IDs of the same list.
_LIST_CONTINUES_RE = re.compile(
    r"\s*(?:,\s*(?:(?:and|or|và|hoặc)\s*)?|\s+(?:and|or|through|to|và|hoặc|đến)\s*|&\s*)"
    rf"(?:(?:no\.?|number|số)\s*|#\s*)?{_RECORD_INTEGER}(?![\w]|[.,]\d)",
    re.IGNORECASE | re.UNICODE,
)
_LABELLED_RECORD_RE = {
    kind: re.compile(
        rf"(?<!\w)(?:{label})s?\s+(?:(?:no\.?|number|số)\s*|#\s*)?{_RECORD_NUMBER}",
        re.IGNORECASE | re.UNICODE,
    )
    for kind, label in _RECORD_KINDS.items()
}


def _digits(number: str) -> str:
    return re.sub(r"[ ,]", "", number)


def _close(a: str, b: str) -> bool:
    """One ID family: same length and leading half, at most two differing trailing digits."""
    half = len(a) // 2
    return (
        len(a) == len(b)
        and a[:half] == b[:half]
        and sum(x != y for x, y in zip(a, b, strict=True)) <= 2
    )


def mark_conflicting_modified_records(
    text: str, cited_text: str, output_language: str = "en"
) -> tuple[str, int]:
    """Mark a modified (secondary) record's digits when its pool states close variants of it.

    Only an ID right after amend/repeal/replace and a record label counts; the modifying
    record, every other word and a pool with one variant stay byte-for-byte. An item that
    itself lists several modified records of one kind is an explicit list and is untouched.
    """
    marker = _REFERENCE_UNCLEAR[nd.validate_output_language(output_language)]
    spans: list[tuple[int, int]] = []
    for kind, pattern in _MODIFIED_RECORD_RE.items():
        claims = list(pattern.finditer(text))
        if len(claims) != 1:
            continue
        claim = claims[0]
        if len(list(_LABELLED_RECORD_RE[kind].finditer(text, claim.start()))) > 1:
            continue  # "amending Ordinance A and Ordinance B": an explicit list of targets
        if _LIST_CONTINUES_RE.match(text, claim.end("n")):
            continue  # "amending Ordinance A and B": the same list under one label
        number = _digits(claim["n"])
        variants = {
            _digits(m["n"]) for m in pattern.finditer(cited_text) if _close(_digits(m["n"]), number)
        }
        if len(variants) > 1:
            spans.append(claim.span("n"))
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
        common = self._common = _common_vocabulary(source)
        self._position = {s["id"]: index for index, s in enumerate(self.source["segments"])}
        self._cores: dict[str, tuple[str, tuple[str, ...]]] = {}
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
                texts = texts[: 1 + MAX_VARIANTS_PER_ITEM]
                texts += self._responsibility_texts(texts)  # a quoted excerpt gets none
                core = None if verbatim or section != "decisions" else self._outcome_core(item)
                if core is not None:
                    self._cores[ref] = core
                    if core[0] not in {text for _r, text in texts}:
                        texts.append((f"{ref}.c", core[0]))
                items.append(_Item(ref, section, position, tuple(texts)))
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
        self._visible = self._visible_ids(self.items)

    def _outcome_core(self, entry: Mapping[str, Any]) -> tuple[str, tuple[str, ...]] | None:
        """The record-outcome core candidate of one decision and its candidate source IDs.

        A candidate is an immutable shorter option the model may choose; it is not semantic
        truth, and code never publishes it over a model's unresolved decision.

        The pool is the item's cited IDs and its candidates from a read-only strict
        ``complete_bindings`` run over a copy holding only this item: the same candidates
        that make up the item's visible source, so a core never cites an unshown segment.
        """
        part: dict[str, Any] = {section: [] for section in _SECTION_NAMES}
        part["decisions"] = [copy.deepcopy(dict(entry))]
        limit = nd._source_ref_limit(self.max_unit_size)
        linked, _counts = link_antecedent_context(
            part, self.source, max_refs_per_item=limit, strict=True, complete_bindings=True
        )
        cited = [self._position[i] for i in entry["source_ids"] if i in self._position]
        pool = set(cited) | {
            self._position[i]
            for linked_entry in linked["decisions"]
            for i in linked_entry["source_ids"]
            if i in self._position
        }
        core = outcome_core_candidate(
            entry["text"], self.source["segments"], pool, cited=cited, max_refs=limit
        )
        if core is None:
            return None
        segments = self.source["segments"]
        return core[0], tuple(segments[index]["id"] for index in core[1])

    def _responsibility_texts(self, texts: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """'.p' over the original, '.pr' over '.r', '.aNp'/'.aNpr' over anonymous ones; capped."""
        extra: list[tuple[str, str]] = []
        for text_ref, text in texts:
            base, suffix = text_ref.split(".")
            variant = responsibility_variant(text, self._common, self.output_language)
            if variant is None or variant in {t for _r, t in [*texts, *extra]}:
                continue
            name = (
                "p"
                if suffix == "o"
                else suffix[:-1] + "pr"
                if suffix.endswith("r")
                else suffix + "p"
            )
            extra.append((f"{base}.{name}", variant))
        return extra[:MAX_RESPONSIBILITY_OPTIONS]

    def _cited(self, item: Mapping[str, Any]) -> str:
        return " ".join(self._segment_text.get(i, "") for i in item.get("source_ids", []))

    # ----- partitions -------------------------------------------------------------------------

    def partitions(self) -> tuple[SemanticAudit, ...]:
        """min(8, N) balanced, contiguous, disjoint item views; fewer than two items: itself.

        A view shares this audit's full source and document and keeps the global refs; its
        schema, prompt and parse admit only its own refs and text refs.
        """
        count = min(MAX_AUDIT_PARTS, len(self.items))
        if count < 2:
            return (self,)
        size, extra = divmod(len(self.items), count)
        bounds = [k * size + min(k, extra) for k in range(count + 1)]  # larger parts first
        return tuple(self._view(self.items[a:b]) for a, b in itertools.pairwise(bounds))

    def _view(self, items: tuple[_Item, ...]) -> SemanticAudit:
        view = copy.copy(self)  # parse deep-copies the shared document; nothing mutates it
        view.items = items
        view._by_ref = {item.ref: item for item in items}
        view._text_refs = {r: (item, text) for item in items for r, text in item.texts}
        view._visible = view._visible_ids(items)
        return view

    # ----- visible source ---------------------------------------------------------------------

    def _visible_ids(self, items: tuple[_Item, ...]) -> tuple[str, ...]:
        """Own cited IDs, strict context candidates and bounded +-1 neighbours, in source order.

        Every own cited ID is kept, never truncated. Candidates come from a read-only run of the
        strict context linker over a deep copy holding only these items; nothing is published.
        A neighbour joins only within the same agenda (no header or topic break between) and
        the existing 120 s gap. No items: the whole source, never an empty snapshot.
        """
        if not items:
            return self._source_ids
        segments = self.source["segments"]
        part: dict[str, Any] = {section: [] for section in _SECTION_NAMES}
        for item in items:
            part[item.section].append(self.document[item.section][item.position])
        cited: set[int] = set()
        for entries in part.values():
            for entry in entries:
                if any(i not in self._position for i in entry["source_ids"]):
                    raise _error(
                        "The semantic audit item cites an unknown source ID.", "source_ids"
                    )
                cited.update(self._position[i] for i in entry["source_ids"])
        linked, _counts = link_antecedent_context(
            copy.deepcopy(part),
            self.source,
            max_refs_per_item=nd._source_ref_limit(self.max_unit_size),
            strict=True,
            complete_bindings=True,
        )
        shown = set(cited)
        for entries in linked.values():
            if isinstance(entries, list):
                shown.update(
                    self._position[i]
                    for entry in entries
                    for i in entry["source_ids"]
                    if i in self._position
                )

        def boundary(index: int) -> bool:
            text = str(segments[index].get("text", ""))
            return bool(_HEADER.search(text) or _TOPIC_BREAK.search(text))

        def close(earlier: int, later: int) -> bool:
            gap = float(segments[later]["start_seconds"]) - float(segments[earlier]["end_seconds"])
            return gap <= MAX_FORWARD_SECONDS

        for index in cited:
            # Before: the cited segment must not open a new agenda; after: nor the neighbour.
            if index > 0 and not boundary(index) and close(index - 1, index):
                shown.add(index - 1)
            if index + 1 < len(segments) and not boundary(index + 1) and close(index, index + 1):
                shown.add(index + 1)
        return tuple(segments[index]["id"] for index in sorted(shown))

    def visible_snapshot(self) -> dict[str, Any]:
        """The source restricted to this view's visible segments: the prompt and citation scope."""
        visible = set(self._visible)
        return {
            **self.source,
            "segments": [s for s in self.source["segments"] if s["id"] in visible],
        }

    def source_scope(self) -> dict[str, Any]:
        """Original 0-based positions of the shown segments and the omitted position ranges."""
        total = len(self.source["segments"])
        shown = [self._position[i] for i in self._visible]
        omitted = [[a + 1, b - 1] for a, b in itertools.pairwise([-1, *shown, total]) if b - a > 1]
        return {
            "full_segment_count": total,
            "shown_positions": shown,
            "omitted_position_ranges": omitted,
            "note": _SCOPE_NOTE[self.output_language],
        }

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
        verified: dict[str, Any] = {}
        for (view, patch), scope in zip(parts, scopes, strict=True):
            visible = set(view._visible)
            if any(
                not isinstance(i, str) or i not in visible
                for c in patch["changes"]
                if isinstance(c, dict) and isinstance(c.get("add_source_ids"), list)
                for i in c["add_source_ids"]
            ):
                raise _error(
                    "The semantic audit part cites a source ID it was not shown.", "source_ids"
                )
            refs = [c.get("ref") for c in patch["changes"] if isinstance(c, dict)]
            decided = patch.get("verified")
            if (
                not {*refs, *patch["unresolved_refs"]} <= scope
                or not isinstance(decided, dict)
                or set(decided) != scope
            ):
                raise _error("The semantic audit names a ref outside its partition.")
            changes += patch["changes"]
            unresolved += patch["unresolved_refs"]
            verified.update(decided)
        merged = json.dumps(
            {
                "audit_version": AUDIT_VERSION,
                "verified": verified,
                "changes": changes,
                "unresolved_refs": unresolved,
            },
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
                "verified": obj({ref: {"type": "boolean"} for ref in refs}),
                "changes": {"type": "array", "maxItems": len(refs), "items": change},
                "unresolved_refs": {
                    "type": "array",
                    "maxItems": len(refs),
                    "items": {"type": "string", "enum": refs},
                },
            }
        )

    def _text_ref_entry(self, item: _Item, text_ref: str, text: str) -> dict[str, Any]:
        """A '.c' option also lists the source IDs choosing it would append (shown, not proof)."""
        entry: dict[str, Any] = {"text_ref": text_ref, "text": text}
        if text_ref == f"{item.ref}.c":
            entry["source_ids"] = list(self._cores[item.ref][1])
        return entry

    def prompt_payload(self) -> dict[str, Any]:
        def entry(item: _Item) -> dict[str, Any]:
            original = self.document[item.section][item.position]
            payload: dict[str, Any] = {
                "ref": item.ref,
                "section": item.section,
                "text_refs": [self._text_ref_entry(item, r, t) for r, t in item.texts],
                "source_ids": list(original["source_ids"]),
            }
            if item.section == "action_items":
                payload["owner"] = original.get("owner")
                payload["deadline_text"] = original.get("deadline_text")
            return payload

        return {
            "audit_version": AUDIT_VERSION,
            "meeting_date": self.source.get("meeting_date"),
            "source": nd.provider_source(self.visible_snapshot()),
            "source_scope": self.source_scope(),
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
            allowed_source_ids=self._visible,
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
            "verified",
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
        verified = patch["verified"]
        if (
            not isinstance(verified, dict)
            or set(verified) != set(self._by_ref)
            or any(type(value) is not bool for value in verified.values())
        ):
            raise _error("The semantic audit must decide every item exactly once.")

        document = copy.deepcopy(self.document)
        scope = set(self._visible)  # an unshown ID fails even when it exists in the full source
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
                "responsibility_unclear",
                "core_selected",
                "core_preserved",  # retired: always 0, kept for manifest compatibility
                "reviewed",
                "model_verified",
                "owner_unverified",  # deterministic guard, never part of model_verified
            ),
            0,
        )
        # Decision coverage, not a quality grade: how many items the model explicitly kept.
        counts["reviewed"] = len(self.items)
        counts["model_verified"] = sum(verified.values())
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
            suffix = text_ref.removeprefix(f"{ref}.")
            if suffix == "c":  # a chosen candidate carries its own candidate IDs
                core_ids = self._cores[ref][1]
                if any(i not in scope for i in core_ids):
                    raise _error("The semantic audit core cites an unshown ID.", "source_ids")
                ids = [*ids, *(i for i in core_ids if i not in ids)]
                counts["core_selected"] += 1
            added = [i for i in ids if i not in published["source_ids"]]
            published["text"] = target[1]
            published["source_ids"] = [*published["source_ids"], *added]
            if clear_owner:
                published["owner"] = None
            if clear_deadline:
                published["deadline_text"] = None
                published["deadline_date"] = None
            counts["changes"] += 1
            counts["anonymized"] += suffix.startswith("a")
            counts["reference_unclear"] += suffix.endswith("r")
            counts["responsibility_unclear"] += "p" in suffix
            counts["cited_items"] += bool(added)
            counts["added_ids"] += len(added)
            counts["cleared_owner"] += clear_owner
            counts["cleared_deadline"] += clear_deadline
        listed = seen | set(unresolved)
        if any(verified[ref] == (ref in listed) for ref in self._by_ref):
            # true means kept unchanged and verified; false means exactly one change or unresolved
            raise _error("The semantic audit decision contradicts its patch.")
        # Deterministic guards on the patched text and its own pooled citations: approximate
        # scale wording keeps a fixed marker; a named vote intention with no local recognition
        # of its first-person statement is quarantined like an unresolved ref; an attributed
        # speaker the pool does not locally identify is dropped as a literal suffix, or the
        # item is quarantined; a confirmation or action item with an uncited leading group
        # actor or a completion the pool states only as partial progress is quarantined; an
        # action owner the pool does not tie to its own cited turn is cleared, nothing else.
        segments = self.source["segments"]
        position = {segment["id"]: index for index, segment in enumerate(segments)}
        forced: set[str] = set()
        for item in self.items:
            published = document[item.section][item.position]
            if item.ref in unresolved or any(x in published["text"] for x in _EXCERPT_LABELS):
                continue
            published["text"], marks = mark_approximate_quantities(
                published["text"], self._cited(published), output_language
            )
            counts["quantity_unclear"] += marks
            if unverified_vote_names(published, segments, position) or speaker_work_as_credential(
                published["text"], self._cited(published)
            ):
                forced.add(item.ref)
                continue
            names = attributed_speakers(published["text"], self._common)
            if unverified_speakers(published, names, segments, position):
                # Drop the unproven attribution as a literal suffix, or quarantine the item.
                variant = next(
                    (
                        v
                        for v in anonymous_variants(published["text"], self._common)
                        if not unverified_speakers(
                            published, attributed_speakers(v, self._common), segments, position
                        )
                    ),
                    None,
                )
                if variant is None:
                    forced.add(item.ref)
                    continue
                published["text"] = variant
                counts["anonymized"] += 1
            if item.section == "action_items" and unverified_owner(published, segments, position):
                published["owner"] = None  # never renamed; text, deadline and IDs are kept
                counts["owner_unverified"] += 1
            if item.section in {"confirmation_items", "action_items"}:
                cited = self._cited(published)
                if unverified_group_actor(published["text"], cited) or strengthened_completion(
                    published["text"], cited
                ):
                    forced.add(item.ref)
                    continue
            if item.section == "confirmation_items":
                # Uncertainty guards only: an uncited official role is quarantined, and close
                # conflicting variants of a modified record keep a marker on its digits.
                cited = self._cited(published)
                if unverified_roles(published["text"], cited):
                    forced.add(item.ref)
                    continue
                published["text"], marks = mark_conflicting_modified_records(
                    published["text"], cited, output_language
                )
                counts["reference_unclear"] += marks
        # Remove by original position after every change, so no index shifts under another.
        quarantined = [item for item in self.items if item.ref in unresolved or item.ref in forced]
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


_SCOPE_NOTE = {
    "en": (
        "source shows only the segments at shown_positions of the full transcript; segments in "
        "omitted_position_ranges are not shown. Segments on either side of an omission are not "
        "adjacent: never infer continuity, a handoff, a turn or the same speaker across one."
    ),
    "vi": (
        "source chỉ gồm các segment ở shown_positions của toàn bộ bản ghi; các segment trong "
        "omitted_position_ranges không được hiển thị. Hai segment ở hai bên một khoảng bị lược "
        "không liền kề: không bao giờ suy ra sự tiếp nối, lời chuyển lượt, lượt nói hay cùng "
        "người nói qua khoảng đó."
    ),
}

_INSTRUCTIONS = {
    "en": (
        "Final source-binding audit of meeting notes. Return one sparse JSON patch, never a "
        "document or new wording. AUDIT_INPUT_JSON.source contains the shown transcript segments; "
        "source_scope identifies omitted ranges. Never infer continuity across omissions. Items "
        "are the final notes assigned to this request. Each item has immutable text_refs: '.o' "
        "is its original text, '.aN' an "
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
        "anonymous variant; this holds in every tense (states, urges, said). Never infer a speaker "
        "from speaker labels. A group owner such as a council, committee, board or commission "
        "needs its own cited mention. Partial progress (much of the work needed to develop a "
        "recommendation) is not completion of the deliverable; keep the qualifier or list the "
        "ref. An organization modifier "
        "cannot be borrowed from an unrelated segment: two separate mentions are not one "
        "combined name. Set clear_owner or clear_deadline when an action owner or deadline is "
        "not stated in its cited or appended segments. Leave fully supported items unlisted; "
        "unlisted items are kept unchanged. When no text_ref, citation or clear can fix a "
        "material unsupported claim, list its ref in unresolved_refs: code then removes that "
        "item's wording and publishes its cited source segments verbatim as a needs-review "
        "confirmation item, so a fixable item should be fixed instead. Never list a ref in both "
        "changes and unresolved_refs. Use only refs, text_refs and source IDs from the input. "
        "'.p' (and '.pr', '.aNp', '.aNpr') replaces only one trailing secondary responsibility "
        "phrase such as 'developed by' an organization with a fixed responsibility-unclear marker; "
        "when the main quantity or fact is supported but that actor's role is not, choose it "
        "instead of unresolved_refs. '.c' is only a shorter candidate, literal words of the "
        "original: its record, its explicit outcome and a directly stated tally. Code does not "
        "prove, guarantee or vouch for it. Check it independently against your own cited and "
        "candidate source exactly like '.o', including retractions, history, committee or "
        "other-body votes and qualifiers. Choose '.c' only when its record and outcome are "
        "supported and only a name, role, purpose or secondary code number is unclear; "
        "otherwise unresolved_refs remains valid. A '.c' text_ref lists the source_ids that "
        "choosing it adds: proposed citation additions, not proof. Review the item's existing "
        "source_ids, those proposed IDs and the shown context; choosing '.c' approves those IDs, "
        "so add any other needed IDs explicitly or list the ref in unresolved_refs. "
        "'N in favor, unopposed' or 'not opposed' states N-0. "
        "Work the speaker describes as their own (we initiated, my committee) is never the "
        "nominee's credential or qualification. "
        "Check each atomic actor-role-object binding separately: a "
        "list of partners does not give each partner the same function, and roles stated for "
        "different projects, categories or examples cannot be merged. A distant introduction, "
        "role continuity, a roster or co-occurrence never identifies a speaker; a "
        "self-introduction counts only within the stated turn, otherwise only explicit local "
        "recognition or a direct report does. Prefer a safe variant or a citation fix over "
        "unresolved_refs for a supported main fact. Modality, completion and current status are "
        "material claims too: a wish, effort, urging or opportunity is not an achieved outcome "
        "or a formal mandate, and a claim that a record is pending, awaiting a vote or still "
        "open needs cited support that it is still current; a committee recommendation alone "
        "does not establish that a final decision is pending. "
        "verified must hold every item ref with a "
        "Boolean: true only when that item is kept unchanged and fully supported, false when it "
        "has exactly one change or is in unresolved_refs; this records your decision for every "
        "item, it is not proof of truth.\n"
    ),
    "vi": (
        "Kiểm tra cuối cùng việc gắn nguồn của biên bản họp. Trả về một bản vá JSON thưa, không "
        "trả về tài liệu hay câu chữ mới. AUDIT_INPUT_JSON.source gồm các segment được hiển thị; "
        "source_scope chỉ rõ khoảng bị lược. Không suy ra sự tiếp nối qua khoảng bị lược. items là "
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
        "segment đó, nếu không thì chọn biến thể ẩn danh; điều này áp dụng ở mọi thì (nêu, kêu "
        "gọi, đã nói). Không bao giờ suy ra người nói từ nhãn người nói. Chủ thể là nhóm như "
        "hội đồng, ủy ban hay ban cần được chính segment được trích nêu. Tiến độ một phần (phần "
        "lớn công việc cần để xây dựng một kiến nghị) không phải là hoàn thành sản phẩm; giữ "
        "bổ ngữ đó hoặc liệt kê ref. Không được mượn bổ ngữ tổ chức từ một segment không liên "
        "quan: hai lần nhắc "
        "riêng không tạo thành một tên ghép. Đặt clear_owner hoặc clear_deadline khi người phụ "
        "trách hoặc hạn chót không được nêu trong segment được trích hoặc bổ sung. Không liệt kê "
        "các mục đã được hỗ trợ đầy đủ; mục không liệt kê được giữ nguyên. Khi không text_ref, "
        "trích dẫn hay lệnh xóa nào sửa được một khẳng định trọng yếu không được hỗ trợ, liệt kê "
        "ref đó trong unresolved_refs: mã sẽ bỏ câu chữ của mục đó và đăng nguyên văn các segment "
        "được trích thành một mục cần xác nhận lại, nên mục sửa được thì hãy sửa. Không bao giờ "
        "liệt kê một ref ở cả changes và unresolved_refs. Chỉ dùng ref, text_ref và source ID có "
        "trong đầu vào. '.p' (và '.pr', '.aNp', '.aNpr') chỉ thay một cụm vai trò phụ ở cuối, "
        "như 'phát triển bởi' một tổ chức, bằng một dấu vai trò chưa rõ cố định; khi con số hay "
        "dữ kiện chính được hỗ trợ nhưng vai trò của chủ thể đó không được hỗ trợ, hãy chọn nó "
        "thay vì unresolved_refs. '.c' chỉ là lõi ứng viên ngắn hơn, đúng chữ của văn bản gốc: "
        "hồ sơ, kết quả rõ ràng và kết quả biểu quyết nêu trực tiếp. Mã không chứng minh hay bảo "
        "đảm nó. Hãy kiểm nó độc lập với nguồn đã trích và ứng viên của chính bạn đúng như '.o', "
        "gồm rút lại, lịch sử, biểu quyết ở ủy ban hay cơ quan khác và điều kiện. Chỉ chọn '.c' "
        "khi hồ sơ và kết quả được hỗ trợ và chỉ tên, vai trò, mục đích hay số hiệu điều khoản "
        "phụ chưa rõ; nếu không, unresolved_refs vẫn hợp lệ. text_ref '.c' liệt kê source_ids mà "
        "việc chọn nó sẽ thêm: đó là đề xuất trích dẫn thêm, không phải bằng chứng. "
        "Hãy xem source_ids hiện có của mục, các ID đề xuất đó và ngữ cảnh được hiển thị; "
        "chọn '.c' là chấp thuận các ID đó, nên hãy thêm tường minh mọi ID cần khác "
        "hoặc liệt kê ref trong unresolved_refs. "
        "'N in favor, unopposed' hoặc 'not opposed' là "
        "N-0. Việc người nói mô tả là của chính mình (we initiated, my committee) không bao giờ "
        "là thành tích hay năng lực của người được đề cử. "
        "Kiểm tra riêng từng gắn kết nguyên tử chủ thể-vai trò-đối "
        "tượng: một danh sách đối tác không cho mỗi đối tác cùng một chức năng, và vai trò được "
        "nêu cho các dự án, hạng mục hay ví dụ khác nhau không được gộp. Lời giới thiệu ở xa, "
        "sự liên tục của vai trò, danh sách hay việc cùng xuất hiện không bao giờ xác định "
        "người nói; lời tự giới thiệu chỉ được tính trong chính lượt nói đó, ngoài ra chỉ lời "
        "mời rõ ràng tại chỗ hoặc lời thuật trực tiếp mới được tính. Với dữ kiện chính được hỗ "
        "trợ, ưu tiên biến thể an toàn hoặc sửa trích dẫn hơn unresolved_refs. Mức độ, sự hoàn "
        "thành và trạng thái hiện tại cũng là khẳng định trọng yếu: mong muốn, nỗ lực, lời kêu "
        "gọi hay cơ hội không phải là kết quả đã đạt hay chỉ thị chính thức, và khẳng định một "
        "hồ sơ đang chờ, đang chờ biểu quyết hay vẫn còn mở cần segment được trích hỗ trợ rằng "
        "trạng thái đó vẫn còn hiện hành; riêng kiến nghị của ủy ban không xác lập rằng quyết "
        "định cuối cùng còn đang chờ. verified phải "
        "chứa mọi ref của mục với giá trị Boolean: true chỉ khi mục được giữ nguyên và được hỗ "
        "trợ đầy đủ, false khi mục có đúng một thay đổi hoặc nằm trong unresolved_refs; đây là "
        "ghi nhận quyết định của bạn cho từng mục, không phải bằng chứng về tính đúng.\n"
    ),
}
