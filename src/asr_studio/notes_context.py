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


def _near_spelling(heard: str, wanted: str) -> bool:
    """One internal ASR letter substitution in a long word: "Lanternes"/"Lanturnes"."""
    if len(heard) != len(wanted) or len(wanted) < 6 or heard[:2] != wanted[:2]:
        return False
    if heard[-2:] != wanted[-2:]:
        return False
    return sum(left != right for left, right in zip(heard, wanted, strict=True)) == 1


def _match(kind: str, value: str, text: str) -> str | frozenset[str] | None:
    """Return "exact", all near organization spellings, or None."""
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
    structured: list[set[int]] = []
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

    The overview is a cross-item summary, so only the detail sections get context links.
    """
    segments = snapshot["segments"]  # saved transcript order
    position = {segment["id"]: index for index, segment in enumerate(segments)}
    result = copy.deepcopy(document)
    counts = {"items": 0, "added_ids": 0}
    for section, value in result.items():
        if section == "overview" or not isinstance(value, list):
            continue
        for item in value:
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
