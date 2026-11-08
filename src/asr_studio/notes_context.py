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
"""

from __future__ import annotations

import copy
import re
from typing import Any

MAX_BACK_SEGMENTS = 16
MAX_BACK_SECONDS = 180.0
MAX_ADDED_PER_ITEM = 12

_ROLES = ("councilmember", "council member", "chair", "president", "mayor", "commissioner")
_HONORIFIC = r"(?:Mr|Mrs|Ms|Dr|Judge|Councilmember|Council member|President|Mayor|Commissioner)"
_NAME_AFTER_HONORIFIC = re.compile(rf"\b{_HONORIFIC}\.?\s+([A-Z][\w'-]+)")
# Capitalized words joined by lowercase connectors: "Friends of the Orchard".
_CONNECTED_ORG = re.compile(r"\b[A-Z][\w'-]+(?:\s+(?:of|the|for)){1,2}(?:\s+[A-Z][\w'-]+)+")
# A literal item number; a decimal or longer number does not match.
_NUMBER_EDGES = (r"(?<!\d)(?<!\d\.)", r"(?!\d)(?!\.\d)")
_LONG_NUMBER = re.compile(rf"{_NUMBER_EDGES[0]}\d{{5,}}{_NUMBER_EDGES[1]}")
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


def _anchors(item: dict[str, Any]) -> list[tuple[str, str]]:
    """(kind, value) anchors stated by the item; kind selects the matcher."""
    text = item["text"]
    found: list[tuple[str, str]] = []
    found += [("word", name) for name in _NAME_AFTER_HONORIFIC.findall(text)]
    found += [("org", org) for org in _CONNECTED_ORG.findall(text)]
    found += [("number", number) for number in _LONG_NUMBER.findall(text)]
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


def _context_for_item(
    item: dict[str, Any], segments: list[dict[str, Any]], position: dict[str, int]
) -> list[int]:
    cited = sorted({position[ref] for ref in item["source_ids"] if ref in position})
    anchors = _anchors(item)
    if not cited or not anchors:
        return []
    added: set[int] = set()
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
    added -= set(cited)
    if len(added) > MAX_ADDED_PER_ITEM:
        return []  # An over-wide chain is left unresolved, not truncated.
    return sorted(added)


def link_antecedent_context(
    document: dict[str, Any], snapshot: dict[str, Any], *, max_refs_per_item: int
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
            added = _context_for_item(item, segments, position)
            if not added or len(item["source_ids"]) + len(added) > max_refs_per_item:
                continue
            ids = set(item["source_ids"]) | {segments[index]["id"] for index in added}
            item["source_ids"] = sorted(ids, key=position.__getitem__)
            counts["items"] += 1
            counts["added_ids"] += len(added)
    return result, counts
