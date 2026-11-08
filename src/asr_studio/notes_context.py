"""Deterministic antecedent-context links for a final notes document.

A long-source item can name a speaker, an organization, a role or a long item number that its
cited claim segments only refer to ("she", "the bill", an unlabelled speaker). When that anchor
appears just before a cited run, its segment and the bridge to the run are added as context
citations. These links are provenance, not proof that the item is entailed by the source.

The search uses no speaker labels (diarization labels can change mid-sentence), never edits
text, owner, deadline or existing citations, and stays local: it looks back from the start of
each cited run at most `MAX_BACK_SEGMENTS` segments and `MAX_BACK_SECONDS` seconds, and adds at
most `MAX_ADDED_PER_ITEM` IDs per item. An anchor that is not found in that window is left
unresolved.

Strict mode (registered Gemini only) adds two bounded anchors: a short labelled record number
("Appointment 1355") found back in the same window, and, for an item that states an outcome
while its cited motion has none, the first outcome statement at most `MAX_FORWARD_SEGMENTS`
segments and `MAX_FORWARD_SECONDS` seconds after the motion.

Strict mode also links two structured anchors in a separate, wider window of at most
`MAX_STRUCTURED_SEGMENTS` segments and `MAX_STRUCTURED_SECONDS` seconds, with no bridge:

- a literal record number absent from the cited pool links only the nearest explicit agenda
  header that states it; a different header found first stops the search;
- a person named just before "votes/voted/voting" links the next explicit vote block (vote or
  roll call opening through its stated result) when the block contains that name. The heard
  vote is never interpreted; a new agenda item, a block without the name or two different
  blocks for one item leave the item unresolved.

Strict mode also adds two short binding links, ahead of any other structured link:

- a cited run whose last segment ends mid-sentence links the continuation, at most
  `MAX_FORWARD_SEGMENTS` segments, through the first one that states item words the pool lacks;
- a named person absent from the cited run and its window links the nearest floor handoff (a
  segment ending in a short capitalised address, "Councilmember Lee.") at most
  `MAX_FORWARD_SEGMENTS` segments before the run. The handoff is provenance only: its words are
  never matched to the name.

Strict mode also adds three provenance links after all others, each one complete or none:

- an acronym ("ABC") absent from the pool links the nearest segment in the back window that
  states it or its capitalised full name (connectors such as "of" dropped), with no bridge;
  this is the only context the overview gets. A name is never read as a person;
- a duration ("6-month", "six months", "6 tháng") absent from the pool links the nearest
  segment in the back window that states the same amount and unit, plus the bridge;
- when the pool cites an explicit agenda header stating the item's record number and a later
  outcome within the structured window, with no other header between them, the first segment
  in that span stating each item word the pool lacks is linked, with no bridge.

`unverified_vote_names` is a check, not a link: it lists each person an item says is voting (or
will vote) who is not recognised for any cited first-person vote statement by an exact handoff
before that turn or an exact "Thank you, <name>" after it. A thank-you elsewhere is no proof.
"""

from __future__ import annotations

import copy
import re
from typing import Any

MAX_BACK_SEGMENTS = 16
MAX_BACK_SECONDS = 180.0
MAX_ADDED_PER_ITEM = 12
MAX_FORWARD_SEGMENTS = 4
MAX_FORWARD_SECONDS = 120.0
MAX_STRUCTURED_SEGMENTS = 32
MAX_STRUCTURED_SECONDS = 360.0

_ROLES = ("councilmember", "council member", "chair", "president", "mayor", "commissioner")
_HONORIFIC = r"(?:Mr|Mrs|Ms|Dr|Judge|Councilmember|Council member|President|Mayor|Commissioner)"
_NAME_AFTER_HONORIFIC = re.compile(rf"\b{_HONORIFIC}\.?\s+([A-Z][\w'-]+)")
# Capitalized words joined by lowercase connectors: "Friends of the Orchard".
_CONNECTED_ORG = re.compile(r"\b[A-Z][\w'-]+(?:\s+(?:of|the|for)){1,2}(?:\s+[A-Z][\w'-]+)+")
# A literal item number; a decimal or longer number does not match.
_NUMBER_EDGES = (r"(?<!\d)(?<!\d\.)", r"(?!\d)(?!\.\d)")
_LONG_NUMBER = re.compile(rf"{_NUMBER_EDGES[0]}\d{{5,}}{_NUMBER_EDGES[1]}")
# Strict mode: a 3-4 digit number labelled by a capitalised word ("Resolution 318").
_LABELED_NUMBER = re.compile(rf"\b[A-Z][a-z]+\.?\s+(\d{{3,4}}){_NUMBER_EDGES[1]}")
_ITEM_OUTCOME = re.compile(
    r"\b(?:approved|adopted|passed|carried|confirmed|amended|rejected|defeated)\b"
    r"|thông qua|phê duyệt|chấp thuận|bác bỏ",
    re.IGNORECASE,
)
_MOTION = re.compile(r"\b(?:move|moved|motion|second|vote)\b|biểu quyết|đề nghị", re.IGNORECASE)
_SOURCE_OUTCOME = re.compile(
    r"\b(?:ayes have it|carries|carried|passes|passed|adopted|approved|amended|confirmed|"
    r"fails|failed)\b|thông qua|bác bỏ",
    re.IGNORECASE,
)
# Strict mode: an explicit agenda header, a vote block opening and a person named as a voter.
_HEADER = re.compile(
    r"\bagenda\s+items?\b|\bitems?\s+(?:no\.?\s*)?\d"
    r"|\b(?:bill|resolution|ordinance|appointment)\s+(?:no\.?\s*)?\d",
    re.IGNORECASE,
)
_VOTE_START = re.compile(
    r"\b(?:roll\s*call|call\s+the\s+(?:roll|role)|passage\s+of"
    r"|vote\s+on\s+(?:the\s+)?(?:council\s+)?(?:bill|resolution|ordinance|motion|amendment|item))\b",
    re.IGNORECASE,
)
_VOTER = re.compile(r"\b([A-Z][a-z][\w'-]*)\s+(?:votes?|voted|voting)\b")
_NOT_A_VOTER = {
    *_ROLES, "council", "committee", "board", "commission", "member", "members", "final",
    "unanimous", "majority", "public", "roll", "the", "no", "yes",
}  # fmt: skip
# A vote intention attributed to a named person, and a first-person vote statement in the source.
_INTENT_VOTER = re.compile(
    r"\b([A-Z][a-z][\w'-]*)\s+(?:(?:is|was)\s+)?(?:voting|(?:will|would|plans?\s+to|intends?\s+to)\s+vote)\b"
)
_FIRST_PERSON_VOTE = re.compile(
    r"\bI(?:\s+will|\s+am|\s+plan\s+to|\s+intend\s+to|['’]ll|['’]m)?\s+(?:be\s+)?vot(?:e|ing)\b"
)
_ACK = re.compile(r"^\s*(?:thank\s+you|thanks)\b[^.!?]*", re.IGNORECASE)
_DIRECT_ACK = re.compile(
    r"^\s*(?i:thank\s+you|thanks)\s*[,!:]?\s*"
    r"(?P<address>(?:[A-Z][\w'’-]*\.?\s+){0,3}[A-Z][\w'’-]*\.?)(?:[,;.!?]|$)"
)
_NOT_AN_ADDRESSEE = {"to", "for", "all", "both", "everyone", "everybody", "the", "our"}
_SENTENCE_END = re.compile(r"[.!?…][\"'”’)]*\s*$")
_ATTRIBUTION_VERBS = (
    "said|says|stated|asked|requested|urged|noted|proposed|argued|supported|opposed|described|"
    "raised|expressed|recommended|thanked|emphasized|questioned|suggested|called|warned"
)
_ATTRIBUTED_SUBJECT = re.compile(rf"^([A-Z][a-z][\w'-]*)(?=\s*:|\s+(?:{_ATTRIBUTION_VERBS})\b)")
_ROLE_LEAD = re.compile(rf"^({'|'.join(_ROLES)})\b", re.IGNORECASE)
_TOPIC_BREAK = re.compile(
    r"(?:^|[.!?]\s*)\s*(?:and\s+)?(?:agenda\s+item\b|"
    r"(?:please\s+read\s+(?:the\s+)?)?next\s+(?:agenda\s+)?item\b)",
    re.IGNORECASE,
)


def _anchors(item: dict[str, Any], *, strict: bool = False) -> list[tuple[str, str]]:
    """(kind, value) anchors stated by the item; kind selects the matcher."""
    text = item["text"]
    found: list[tuple[str, str]] = []
    found += [("word", name) for name in _NAME_AFTER_HONORIFIC.findall(text)]
    found += [("org", org) for org in _CONNECTED_ORG.findall(text)]
    found += [("number", number) for number in _LONG_NUMBER.findall(text)]
    if strict:
        found += [("number", number) for number in _LABELED_NUMBER.findall(text)]
    if (match := _ATTRIBUTED_SUBJECT.match(text)) and match.group(1).lower() not in _ROLES:
        found.append(("word", match.group(1)))
    if match := _ROLE_LEAD.match(text):
        found.append(("word", match.group(1)))
    owner = item.get("owner")
    # A role owner is an anchor; a person name or speaker label owner is never inferred.
    if isinstance(owner, str) and owner.strip().lower() in _ROLES:
        found.append(("word", owner.strip()))
    return list(dict.fromkeys(found))


_ACRONYM = re.compile(r"\b[A-Z]{2,6}\b")
_CONNECTORS = frozenset({"of", "the", "for", "and", "on", "at", "in", "de", "&"})
_AMOUNTS = {
    word: value
    for value, word in enumerate(
        "one two three four five six seven eight nine ten eleven twelve".split(),  # noqa: SIM905
        start=1,
    )
}
_UNITS = {"ngày": "day", "tuần": "week", "tháng": "month", "năm": "year"}
_DURATION = re.compile(
    rf"(?<!\w)(\d{{1,3}}|{'|'.join(_AMOUNTS)})[\s-]+(day|week|month|year)s?(?!\w)"
    rf"|(?<!\w)(\d{{1,3}})\s+({'|'.join(_UNITS)})(?!\w)",
    re.IGNORECASE,
)


def _durations(text: str) -> set[str]:
    """Canonical "<amount> <unit>" for each stated duration: "six-month" -> "6 month"."""
    found = set()
    for match in _DURATION.finditer(text):
        amount, unit = (match[1], match[2]) if match[1] else (match[3], match[4])
        amount = amount.lower()
        value = _AMOUNTS.get(amount) or int(amount)
        found.add(f"{value} {_UNITS.get(unit.lower(), unit.lower())}")
    return found


def _names_acronym(acronym: str, text: str) -> bool:
    """The acronym itself, or capitalised words whose initials spell it ("Bay Area Rail")."""
    if re.search(rf"\b{acronym}\b", text):
        return True
    words = re.findall(r"[\w'&-]+", text)
    for first in range(len(words)):
        initials, index = "", first
        while index < len(words) and len(initials) < len(acronym):
            word = words[index]
            index += 1
            if initials and word.lower() in _CONNECTORS:
                continue
            if not word[0].isupper():
                break
            initials += word[0]
        if initials == acronym:
            return True
    return False


def _near_spelling(heard: str, wanted: str) -> bool:
    """One internal ASR letter substitution in a long word: "Lanternes"/"Lanturnes"."""
    if len(heard) != len(wanted) or len(wanted) < 6 or heard[:2] != wanted[:2]:
        return False
    if heard[-2:] != wanted[-2:]:
        return False
    return sum(left != right for left, right in zip(heard, wanted, strict=True)) == 1


def _match(kind: str, value: str, text: str) -> str | frozenset[str] | None:
    """Return "exact", all near organization spellings, or None."""
    if kind == "acronym":
        return "exact" if _names_acronym(value, text) else None
    if kind == "duration":
        return "exact" if value in _durations(text) else None
    if kind == "number":
        found = re.search(rf"{_NUMBER_EDGES[0]}{value}{_NUMBER_EDGES[1]}", text)
        return "exact" if found else None
    if kind != "org":
        return "exact" if re.search(rf"\b{re.escape(value)}\b", text, re.IGNORECASE) else None
    # The connector phrase is exact; only its last word may carry an ASR spelling.
    words = value.split()
    head = r"\s+".join(map(re.escape, words[:-1]))
    wanted = words[-1].lower()
    near: set[str] = set()
    for found in re.finditer(rf"\b{head}\s+([\w'-]+)", text, re.IGNORECASE):
        heard = found.group(1).lower()
        if heard == wanted:
            return "exact"
        if _near_spelling(heard, wanted):
            near.add(heard)
    return frozenset(near) or None


def _antecedent(kind: str, value: str, segments: list[dict[str, Any]], start: int) -> int | None:
    """Nearest exact anchor in the local window; else one unambiguous near spelling."""
    start_time = float(segments[start]["start_seconds"])
    near: list[tuple[int, str]] = []
    for back in range(start - 1, max(start - MAX_BACK_SEGMENTS, 0) - 1, -1):
        # A header can identify its own item, but never a claim in a later agenda item.
        if back < start - 1 and _TOPIC_BREAK.search(segments[back + 1]["text"]):
            break
        if start_time - float(segments[back]["start_seconds"]) > MAX_BACK_SECONDS:
            break
        found = _match(kind, value, segments[back]["text"])
        if found == "exact":
            return back
        if found is not None:
            near.extend((back, spelling) for spelling in found)
    if len({spelling for _back, spelling in near}) == 1:
        return near[0][0]
    return None  # missing, or two different near spellings: left unresolved


def _runs(indices: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for index in indices:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


def _outcome_after(run: list[int], segments: list[dict[str, Any]]) -> int | None:
    """First outcome statement shortly after a cited motion run; a new agenda item stops it."""
    end = run[-1]
    end_time = float(segments[end]["end_seconds"])
    for ahead in range(end + 1, min(end + MAX_FORWARD_SEGMENTS, len(segments) - 1) + 1):
        text = segments[ahead]["text"]
        if _TOPIC_BREAK.search(text) or float(segments[ahead]["start_seconds"]) - end_time > (
            MAX_FORWARD_SECONDS
        ):
            return None
        if _SOURCE_OUTCOME.search(text):
            return ahead
    return None


def _header_before(value: str, segments: list[dict[str, Any]], start: int) -> int | None:
    """Nearest explicit agenda header stating `value`; any other header first stops the search."""
    start_time = float(segments[start]["start_seconds"])
    for back in range(start - 1, max(start - MAX_STRUCTURED_SEGMENTS, 0) - 1, -1):
        if start_time - float(segments[back]["start_seconds"]) > MAX_STRUCTURED_SECONDS:
            return None
        text = segments[back]["text"]
        if _HEADER.search(text) or _TOPIC_BREAK.search(text):
            numbers = set(_LONG_NUMBER.findall(text)) | set(_LABELED_NUMBER.findall(text))
            # A header naming a different or a second record number is never this item's.
            return back if _match("number", value, text) and numbers <= {value} else None
    return None


def _vote_block_after(name: str, cited: list[int], segments: list[dict[str, Any]]) -> range | None:
    """Next explicit vote block after the last cited mention of `name`, if it contains the name."""
    mentions = [index for index in cited if _match("word", name, segments[index]["text"])]
    if not mentions:
        return None
    anchor = mentions[-1]
    anchor_end = float(segments[anchor]["end_seconds"])
    opening = None
    last = min(anchor + MAX_STRUCTURED_SEGMENTS, len(segments) - 1)
    for ahead in range(anchor + 1, last + 1):
        text = segments[ahead]["text"]
        if float(segments[ahead]["start_seconds"]) - anchor_end > MAX_STRUCTURED_SECONDS:
            return None
        topic_break = _TOPIC_BREAK.search(text)
        header = _HEADER.search(text)
        # A vote opening may itself name its bill. Later headers (including another vote
        # opening) end this block; compare positions so an outcome after a header cannot win.
        if (
            header is not None
            and (opening is not None or not _VOTE_START.search(text))
            and (topic_break is None or header.start() < topic_break.start())
        ):
            topic_break = header
        if opening is None:
            if topic_break:
                return None  # a new agenda item before any vote
            if not _VOTE_START.search(text):
                continue
            opening = ahead
        outcome = _SOURCE_OUTCOME.search(text)
        if outcome and (topic_break is None or outcome.start() < topic_break.start()):
            block = range(opening, ahead + 1)
            named = any(_match("word", name, segments[index]["text"]) for index in block)
            return block if named else None
        if topic_break:
            return None  # the block ran into a new agenda item without a result
    return None


def _vocative(text: str) -> str | None:
    """The last sentence when it is only a short capitalised address: "Councilmember Lee."."""
    last = re.split(r"(?<=[.!?])\s+", text.strip())[-1].rstrip(".!?")
    words = last.split()
    return last if 2 <= len(words) <= 4 and all(word[0].isupper() for word in words) else None


def _handoff_before(start: int, segments: list[dict[str, Any]], limit: int) -> int | None:
    """Nearest floor handoff before `start`; an acknowledgment or a new agenda item stops it."""
    start_time = float(segments[start]["start_seconds"])
    for back in range(start - 1, max(start - limit, 0) - 1, -1):
        text = segments[back]["text"]
        if start_time - float(segments[back]["start_seconds"]) > MAX_BACK_SECONDS:
            return None
        if _vocative(text) is not None:
            return back
        if _ACK.match(text) or _TOPIC_BREAK.search(text):
            return None
    return None


def _ack_after(end: int, segments: list[dict[str, Any]]) -> int | None:
    """The first "Thank you, ..." acknowledgment shortly after `end`."""
    end_time = float(segments[end]["end_seconds"])
    for ahead in range(end + 1, min(end + MAX_FORWARD_SEGMENTS, len(segments) - 1) + 1):
        text = segments[ahead]["text"]
        if _TOPIC_BREAK.search(text) or float(segments[ahead]["start_seconds"]) - end_time > (
            MAX_FORWARD_SECONDS
        ):
            return None
        if _ACK.match(text):
            return ahead
    return None


def _recognized(name: str, index: int, segments: list[dict[str, Any]], cited: set[int]) -> bool:
    """A cited exact handoff to `name` before statement `index`, or a cited acknowledgment after.

    The turn bounds are found in the full source, but the witness itself must be in the item's
    own pool; an uncited recognition leaves the name unverified.
    """
    back = _handoff_before(index, segments, MAX_BACK_SEGMENTS)
    if back in cited and _match("word", name, _vocative(segments[back]["text"]) or ""):
        return True
    ahead = _ack_after(index, segments)
    ack = _DIRECT_ACK.match(segments[ahead]["text"]) if ahead in cited else None
    return (
        ack is not None
        and ack["address"].split()[0].casefold().rstrip(".") not in _NOT_AN_ADDRESSEE
        and _match("word", name, ack["address"]) is not None
    )


def _reports_vote(name: str, text: str) -> bool:
    """`name` is the grammatical voter: "Lee votes no", "Lee will be voting yes"."""
    aux = r"(?:(?:is|was|will|would|has|had)\s+)?(?:be\s+)?"
    voter = rf"\b{re.escape(name)}\s+{aux}vot(?:e|es|ed|ing)\b"
    return re.search(voter, text) is not None


def unverified_vote_names(
    item: dict[str, Any], segments: list[dict[str, Any]], position: dict[str, int]
) -> list[str]:
    """Named vote intentions with first-person cited statements but no cited recognition."""
    names = [
        name
        for name in dict.fromkeys(_INTENT_VOTER.findall(item["text"]))
        if name.lower() not in _NOT_A_VOTER
    ]
    cited = sorted({position[ref] for ref in item["source_ids"] if ref in position})
    statements = [index for index in cited if _FIRST_PERSON_VOTE.search(segments[index]["text"])]
    if not names or not statements:
        return []  # no first-person statement to bind: out of scope
    return [
        name
        for name in names
        if not any(_reports_vote(name, segments[index]["text"]) for index in cited)
        and not any(_recognized(name, index, segments, set(cited)) for index in statements)
    ]


_FUNCTION_WORDS = frozenset(
    """
    about after again against because before being below between could during every other
    their there these those through under until where which while would should
    """.split()  # noqa: SIM905 - a readable word list
)


def _words(text: str) -> set[str]:
    """Content words (five or more letters, no function words), plural "s" dropped."""
    words = {word.lower() for word in re.findall(r"[A-Za-z]{5,}", text)}
    return {word.removesuffix("s") for word in words - _FUNCTION_WORDS}


def _binding_links(
    item: dict[str, Any], cited: list[int], segments: list[dict[str, Any]]
) -> list[set[int]]:
    """Strict binding links: the continuation of a cut sentence, and a named turn's handoff."""
    links: list[set[int]] = []
    missing = _words(item["text"]) - _words(" ".join(segments[i]["text"] for i in cited))
    names = [*_NAME_AFTER_HONORIFIC.findall(item["text"])]
    if (match := _ATTRIBUTED_SUBJECT.match(item["text"])) and match.group(1).lower() not in _ROLES:
        names.append(match.group(1))
    for run in _runs(cited):
        end, last = run[-1], run[-1]
        while (
            missing
            and not _SENTENCE_END.search(segments[last]["text"])
            and last + 1 < len(segments)
            and last + 1 - end <= MAX_FORWARD_SEGMENTS
            and float(segments[last + 1]["start_seconds"]) - float(segments[end]["end_seconds"])
            <= MAX_FORWARD_SECONDS
            and not _TOPIC_BREAK.search(segments[last + 1]["text"])
        ):
            last += 1
            if _words(segments[last]["text"]) & missing:
                links.append(set(range(end + 1, last + 1)))
                break
        run_text = " ".join(segments[i]["text"] for i in run)
        for name in dict.fromkeys(names):
            if (
                _match("word", name, run_text)
                or _antecedent("word", name, segments, run[0]) is not None
            ):
                continue
            back = _handoff_before(run[0], segments, MAX_FORWARD_SEGMENTS)
            if back is not None:
                links.append(set(range(back, run[0])))
    return links


def _provenance_links(
    item: dict[str, Any], cited: list[int], segments: list[dict[str, Any]], *, overview: bool
) -> list[set[int]]:
    """Strict acronym, duration and same-agenda word links (only acronyms for the overview)."""
    text = item["text"]
    anchors = [("acronym", value) for value in dict.fromkeys(_ACRONYM.findall(text))]
    if not overview:
        anchors += [("duration", value) for value in sorted(_durations(text))]
    links: list[set[int]] = []
    for kind, value in anchors:
        if any(_match(kind, value, segments[index]["text"]) for index in cited):
            continue
        # The nearest statement before any cited run; a full name identifies itself (no bridge).
        found = [
            (run[0] - back, back, run[0])
            for run in _runs(cited)
            if (back := _antecedent(kind, value, segments, run[0])) is not None
        ]
        if found:
            _gap, back, start = min(found)
            links.append({back} if kind == "acronym" else set(range(back, start)))
    if not overview and (span := _agenda_span(text, cited, segments)) is not None:
        missing = _words(text) - _words(" ".join(segments[i]["text"] for i in cited))
        found = {
            next((i for i in span if word in _words(segments[i]["text"])), None)
            for word in sorted(missing)
        }
        if found - {None}:
            links.append(found - {None})
    return links


def _agenda_span(text: str, cited: list[int], segments: list[dict[str, Any]]) -> range | None:
    """Segments between a cited header of the item's record and its cited outcome."""
    numbers = set(_LONG_NUMBER.findall(text)) | set(_LABELED_NUMBER.findall(text))
    for header in cited:
        head_text = segments[header]["text"]
        if not _HEADER.search(head_text) or not any(
            _match("number", value, head_text) for value in numbers
        ):
            continue
        start_time = float(segments[header]["start_seconds"])
        for ahead in range(
            header + 1, min(header + MAX_STRUCTURED_SEGMENTS, len(segments) - 1) + 1
        ):
            if float(segments[ahead]["start_seconds"]) - start_time > MAX_STRUCTURED_SECONDS:
                return None
            ahead_text = segments[ahead]["text"]
            if _HEADER.search(ahead_text) or _TOPIC_BREAK.search(ahead_text):
                stated = set(_LONG_NUMBER.findall(ahead_text)) | set(
                    _LABELED_NUMBER.findall(ahead_text)
                )
                # Another agenda item ends the span, even with an outcome word in that segment;
                # only a header restating this item's record alone may carry its outcome.
                if not stated or not stated <= numbers:
                    return None
            if ahead in cited and _SOURCE_OUTCOME.search(ahead_text):
                return range(header + 1, ahead)
        return None
    return None


def _context_for_item(
    item: dict[str, Any],
    segments: list[dict[str, Any]],
    position: dict[str, int],
    *,
    strict: bool = False,
) -> tuple[list[int], list[set[int]]]:
    """Generic context IDs (unchanged rules) and the complete strict structured links."""
    cited = sorted({position[ref] for ref in item["source_ids"] if ref in position})
    anchors = _anchors(item, strict=strict)
    added: set[int] = set()
    # Binding evidence first: complete links are admitted in order while they fit.
    structured: list[set[int]] = _binding_links(item, cited, segments) if strict else []
    if (
        strict
        and cited
        and _ITEM_OUTCOME.search(item["text"])
        and not any(_SOURCE_OUTCOME.search(segments[index]["text"]) for index in cited)
    ):
        for run in _runs(cited):
            if (
                _MOTION.search(" ".join(segments[index]["text"] for index in run))
                and (ahead := _outcome_after(run, segments)) is not None
            ):
                added.update(range(run[-1] + 1, ahead + 1))
                break  # one outcome per item
    for run in _runs(cited):
        run_text = " ".join(segments[index]["text"] for index in run)
        start = run[0]
        for kind, value in anchors:
            if _match(kind, value, run_text) is not None:
                continue
            back = _antecedent(kind, value, segments, start)
            if back is not None:
                # The anchor and the bridge that carries its reference to the cited run.
                added.update(range(back, start))
            elif (
                strict
                and kind == "number"
                and not any(_match(kind, value, segments[index]["text"]) for index in cited)
            ):
                header = _header_before(value, segments, start)
                if header is not None:
                    structured.append({header})  # the header states the relation: no bridge
    if strict and cited:
        voters = [name for name in _VOTER.findall(item["text"]) if name.lower() not in _NOT_A_VOTER]
        blocks = {
            tuple(block)
            for name in dict.fromkeys(voters)
            if (block := _vote_block_after(name, cited, segments)) is not None
        }
        if len(blocks) == 1:  # two different blocks for one item conflict: unresolved
            structured.append(set(blocks.pop()))
    if strict:
        structured += _provenance_links(item, cited, segments, overview=False)
    added -= set(cited)
    if len(added) > MAX_ADDED_PER_ITEM:
        added = set()  # An over-wide chain is left unresolved, not truncated.
    return sorted(added), [link - set(cited) for link in structured]


def link_antecedent_context(
    document: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    max_refs_per_item: int,
    strict: bool = False,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Return a copy with local antecedent IDs added, and bounded counts.

    The overview is a cross-item summary, so only the detail sections get context links, except
    strict acronym links.
    """
    segments = snapshot["segments"]  # saved transcript order
    position = {segment["id"]: index for index, segment in enumerate(segments)}
    result = copy.deepcopy(document)
    counts = {"items": 0, "added_ids": 0}
    for section, value in result.items():
        if not isinstance(value, list) or (section == "overview" and not strict):
            continue
        for item in value:
            if section == "overview":
                cited = sorted({position[ref] for ref in item["source_ids"] if ref in position})
                generic = []
                links = _provenance_links(item, cited, segments, overview=True)
                structured = [link - set(cited) for link in links]
            else:
                generic, structured = _context_for_item(item, segments, position, strict=strict)
            fits = len(item["source_ids"]) + len(generic) <= max_refs_per_item
            added = set(generic) if fits else set()
            # Generic links are kept as before; each complete structured link joins only if the
            # union still fits both caps. A link is never truncated.
            for link in structured:
                union = added | link
                if (
                    len(union) <= MAX_ADDED_PER_ITEM
                    and len(item["source_ids"]) + len(union) <= max_refs_per_item
                ):
                    added = union
            if not added:
                continue
            ids = set(item["source_ids"]) | {segments[index]["id"] for index in added}
            item["source_ids"] = sorted(ids, key=position.__getitem__)
            counts["items"] += 1
            counts["added_ids"] += len(added)
    return result, counts
