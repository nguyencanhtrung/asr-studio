"""Record-outcome core candidate of one decision item, checked against its strict candidate pool.

A candidate holds only literal substrings of the original item: its one typed primary record,
its explicit outcome verb and, when the outcome's own vote directly states them, its tally and
an "as amended" qualifier. Names, roles, dates, purposes and secondary code numbers are never
part of it. It is an immutable shorter option for the model, not proof that the decision is
true: the conservative recognizer only decides whether to offer it, and code never publishes
it over a model's unresolved decision. The check reads the full snapshot for agenda boundaries
between the record and its outcome; support comes only from the caller's strictly closed pool
(the item's cited IDs and its strict context candidates).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from asr_studio.notes_context import (
    _GENERIC_HEADER,
    _HEADER,
    _TOPIC_BREAK,
    MAX_AGENDA_SECONDS,
    MAX_AGENDA_SEGMENTS,
    _header_states_record,
    _record_refs,
)

# The outcome verbs a record family can take, and the nouns a source outcome sentence uses.
_FAMILY_VERBS = {
    "bill": ("passed", "adopted"),
    "ordinance": ("passed", "adopted"),
    "resolution": ("adopted", "passed"),
    "appointment": ("confirmed", "approved"),
}
_FAMILY_NOUNS = {
    "bill": r"bill|ordinance|legislation",
    "ordinance": r"ordinance|bill|legislation",
    "resolution": r"resolution",
    "appointment": r"appointment",
}
_ITEM_OUTCOME = re.compile(r"\b(passed|adopted|confirmed|approved)\b", re.IGNORECASE)
# Negation, condition, recommendation or a future vote anywhere in the item: no core.
_ITEM_BLOCKERS = re.compile(
    r"\b(?:not|never|no|fail\w*|reject\w*|defeat\w*|den(?:y|ied)|tabled|withdrawn|if|unless"
    r"|would|will|shall|should|may|might|could|expected|pending|recommend\w*|propos\w*"
    r"|scheduled|later)\b|n['’]t\b|\b(?-i:plan(?:s|ned|ning)?\s+to|planned)\b",
    re.IGNORECASE,
)
# Harmless label and date words the blockers would misread: "Bill No. 12", "May 3, 2026".
_ITEM_LABELS = re.compile(
    r"\b(?-i:No)\.?\s*#?\d|\b(?-i:May)\s+\d|\b(?:in|of|since|until|by|from|on)\s+(?-i:May)\b",
    re.IGNORECASE,
)


def _item_blocked(text: str, refs: Sequence[tuple[str, str, str, tuple[int, int]]]) -> bool:
    """A blocker word outside the item's record labels and calendar dates."""
    for _family, _number, _label, (start, end) in refs:
        text = text[:start] + " " * (end - start) + text[end:]
    return bool(_ITEM_BLOCKERS.search(_ITEM_LABELS.sub(lambda m: " " * len(m[0]), text)))


# Right before a source outcome in its sentence these make it a recommendation or not an outcome.
_SOURCE_BLOCKERS = re.compile(
    r"\b(?:not|never|recommend\w*|if|unless|would|will|shall|should|may|might|could|expect\w*"
    r"|hope\w*|want\w*|propos\w*|move|moved|motion\s+to|already|previously|originally"
    r"|as\s+you\s+(?:may\s+)?(?:know|recall)|recall\w*|rememb\w*)\b|n['’]t\b",
    re.IGNORECASE,
)
_SOURCE_FAILURE = re.compile(
    r"\b(?:fail(?:s|ed)?|defeated|rejected|not\s+(?:pass|passed|adopted|confirmed|approved"
    r"|carr(?:y|ied)))\b",
    re.IGNORECASE,
)
_WORDS = "zero one two three four five six seven eight nine ten eleven twelve".split()  # noqa: SIM905
_COUNT = rf"(?:\d{{1,2}}|{'|'.join(_WORDS)})"
# "Appointments 12 through 15", "Bill 1 and 2": a list of records is never one core.
_LIST_AFTER = re.compile(
    r"\s*(?:,|&|/|-|–|\band\b|\bor\b|\bplus\b|\bas\s+well\s+as\b|\bthrough\b|\bto\b)\s*\d"
)
_UNOPPOSED = r"(?:unopposed|not\s+opposed|none\s+opposed)"
_ITEM_TALLIES = (
    re.compile(r"(?<![\w.\-–])(\d{1,2})\s*[-–]\s*(\d{1,2})(?![\w.\-–])"),
    re.compile(rf"\b({_COUNT})\s+in\s+favou?r\s*,?\s*(?:and\s+)?({_COUNT})\s+opposed\b", re.I),
    re.compile(rf"\b({_COUNT})\s+in\s+favou?r\s*,?\s*(?:and\s+)?{_UNOPPOSED}\b", re.I),
)
_SOURCE_TALLY = re.compile(
    rf"\b(?P<n>{_COUNT})\s+in\s+favou?r\b\s*,?\s*(?:and\s+)?"
    rf"(?:(?P<un>{_UNOPPOSED})|(?P<m>{_COUNT})\s+opposed)\b",
    re.IGNORECASE,
)


def _count(token: str) -> int:
    token = token.lower()
    return int(token) if token.isdigit() else _WORDS.index(token)


def _item_tally(text: str) -> tuple[tuple[int, int], tuple[int, int]] | None | bool:
    """((for, against), span) of the item's one tally; None without one, False when several."""
    found: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for pattern in _ITEM_TALLIES:
        for match in pattern.finditer(text):
            if any(a < match.end() and match.start() < b for _v, (a, b) in found):
                continue
            against = _count(match[2]) if match.lastindex and match.lastindex >= 2 else 0
            found.append(((_count(match[1]), against), match.span()))
    if len(found) > 1:
        return False
    return found[0] if found else None


# A source outcome is a plain clause whose subject is the record noun: "The bill passed", "Bill
# passed", "..., an appointment is confirmed". A reported, asked or doubted outcome never is.
_SUBJECT_LEAD = re.compile(
    r"(?:^|[,:;]|\b(?:and|so|okay|ok|then|now|well|yes)\b)\s*(?:(?:the|this|that|an?)\s+)?"
    r"(?:council\s+)?$",
    re.IGNORECASE,
)
_DOUBT = re.compile(
    r"\b(?:whether|unclear|unknown|uncertain|unsure|unverified|nobody|no\s+one|claim\w*"
    r"|alleged\w*|reported\w*|rumou?r\w*|suppos\w*|apparently|maybe|perhaps|correction"
    r"|actually|sorry|mistak\w*|retract\w*|wait|i\s+mean)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:not|never|no)\b|n['’]t\b", re.IGNORECASE)
_CLAUSE_END = re.compile(r",?\s+(?:and|but|then|so|while|although|though)\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?;](?=\s|$)")
_AS_AMENDED = re.compile(r"\bas\s+amended\b", re.IGNORECASE)
# A sentence of another vote: an amendment, or an outcome of anything but the motion itself.
_OTHER_VOTE = re.compile(
    r"\b(?:amendments?|substitute)\b|\b(?!motion\b)\w+\s+(?:is\s+|was\s+|has\s+been\s+)?"
    r"(?:pass(?:ed|es)|adopted|carr(?:ies|ied)|confirmed|approved|fail(?:s|ed))\b",
    re.IGNORECASE,
)


# In the outcome's own clause these make it another body's or a preliminary passage: "passed
# committee", "passed on first reading", "passes to full council", "passed, pending ...".
_NOT_FINAL = re.compile(
    r"\b(?:(?:sub)?committees?|readings?|to\s+(?:the\s+)?(?:full\s+)?council|pending"
    r"|preliminar\w*|tentative\w*|initial\w*|introduc\w*)\b",
    re.IGNORECASE,
)
# Anywhere in the outcome sentence: an earlier passage, not this meeting's outcome.
_HISTORY = re.compile(
    r"\b(?:last|previous|prior)\s+(?:year|month|week|session|meeting|term|time)\b"
    r"|\b(?:years?|months?|weeks?|days?)\s+ago\b|\byesterday\b|\bback\s+in\b"
    r"|\bin\s+(?:19|20)\d\d\b|\bearlier\s+(?:this|that|in)\b",
    re.IGNORECASE,
)
# After the outcome, up to the next agenda: a correction, a deferral or a vote still to come.
_CORRECTION = re.compile(
    r"\b(?:misspoke|mis-spoke|correct(?:ion|ing)?|scratch\s+that|strike\s+that|i\s+mean"
    r"|stand\s+corrected|sorry|actually|my\s+(?:mistake|bad|apologies)|mistak\w*|retract\w*"
    r"|wrong|wait|rather|apologi[sz]e\w*)\b",
    re.IGNORECASE,
)
_STATUS = (
    r"(?:held|hold|holding|tabled?|tabling|continued|deferr?(?:ed|al)?|postpon\w*"
    r"|(?:re-?)?referr?(?:ed)?|sent\s+back|returned|laid\s+over|withdrawn|withdrew"
    r"|fail(?:s|ed)?|defeated|rejected|not\s+(?:pass\w*|adopted|confirmed|approved|carr\w*)"
    r"|did(?:n['’]t|\s+not)\s+(?:pass|carry))\b"
)
_BE = r"\s+(?:is|was|has\s+been|will\s+be|to\s+be|be|been|being|got|gets)"
_LATER_MOTION = re.compile(
    r"\b(?:move[ds]?|motion)\s+to\s+(?:hold|table|continue|defer|postpone|refer|reconsider"
    r"|rescind|send|return)\b|\breconsider\w*",
    re.IGNORECASE,
)
_FUTURE = re.compile(
    r"\b(?:will|shall|going\s+to|next\s+(?:week|month|meeting|session|time)|later|tomorrow"
    r"|upcoming|future|scheduled)\b|['’]ll\b",
    re.IGNORECASE,
)
_VOTE_AHEAD = re.compile(
    r"\bvot(?:e|es|ing)\b|\bfinal\s+(?:vote|passage|action|reading)\b"
    r"|\bgo(?:es|ing)?\s+(?:on\s+)?to\s+(?:the\s+)?(?:full\s+)?council\b",
    re.IGNORECASE,
)
_TAKE_UP = re.compile(
    r"\b(?:consider\w*|take\s+(?:it\s+|this\s+)?up|taken\s+up|come\s+back|return\w*"
    r"|hear(?:d|ing)?)\b",
    re.IGNORECASE,
)


def _retracts(family: str, verb: str, sentence: str) -> bool:
    """A later sentence corrects, defers or reopens the record's outcome."""
    noun = rf"(?:{_FAMILY_NOUNS[family]})s?(?:\s+(?:no\.?\s*|number\s+|#\s*)?\d+)?"
    # "The bill was held", "it was tabled": the record, or a passive "it", as the subject.
    subject = rf"\b(?:{noun}(?:{_BE})?|it{_BE})\s+{_STATUS}"
    refers = re.search(rf"\b(?:{noun}|it|this)\b", sentence, re.IGNORECASE)
    return bool(
        re.search(subject, sentence, re.IGNORECASE)
        or (
            _CORRECTION.search(sentence)
            and (
                re.search(rf"\b(?:{verb}|pass\w*|vote\w*|tally)\b", sentence, re.I)
                or _SOURCE_TALLY.search(sentence)
            )
        )
        or _LATER_MOTION.search(sentence)
        or (
            _FUTURE.search(sentence)
            and (_VOTE_AHEAD.search(sentence) or (refers and _TAKE_UP.search(sentence)))
        )
    )


def _retracted_later(
    family: str,
    number: str,
    verb: str,
    segments: Sequence[Mapping[str, Any]],
    outcome: int,
    clause_end: int,
) -> bool:
    """A correction, deferral or future vote follows the outcome before the next agenda.

    The full snapshot is read from the end of the outcome clause, sentence by sentence, until
    a header, topic break or labelled record that does not state this record alone, within the
    long agenda window. A sentence that states this record still counts.
    """
    start_time = float(segments[outcome].get("start_seconds", 0.0))
    last = min(outcome + MAX_AGENDA_SEGMENTS, len(segments))
    for index in range(outcome, last):
        if float(segments[index].get("start_seconds", 0.0)) - start_time > MAX_AGENDA_SECONDS:
            return False
        text = str(segments[index].get("text", ""))
        if index == outcome:
            text = text[clause_end:]
        for start, end in _sentences(text):
            sentence = text[start:end]
            refs = {(kind, canonical) for kind, canonical, _label, _span in _record_refs(sentence)}
            if (
                refs - {(family, number)}
                or _HEADER.search(sentence)
                or _TOPIC_BREAK.search(sentence)
            ) and not _header_states_record(family, number, sentence):
                return False
            if _retracts(family, verb, sentence):
                return True
    return False


def _ambiguous_agenda(
    family: str,
    number: str,
    segments: Sequence[Mapping[str, Any]],
    record: int,
    outcome: int,
    sentence_end: int,
) -> bool:
    """The nearest agenda header at or before the record lists it with others.

    The outcome segment is read up to its outcome sentence, so a later agenda may follow.
    """
    start_time = float(segments[record].get("start_seconds", 0.0))
    for back in range(record, max(record - MAX_AGENDA_SEGMENTS, -1), -1):
        if start_time - float(segments[back].get("start_seconds", 0.0)) > MAX_AGENDA_SECONDS:
            return False
        text = str(segments[back].get("text", ""))
        if back == outcome:
            text = text[:sentence_end]
        if _GENERIC_HEADER.search(text) or _TOPIC_BREAK.search(text):
            named = (family, number) in {(f, n) for f, n, _label, _span in _record_refs(text)}
            return named and not _header_states_record(family, number, text)
    return False


def _sentences(text: str) -> list[tuple[int, int]]:
    """Spans of the sentences of a text, each with its closing mark."""
    spans, start = [], 0
    for mark in _SENTENCE_END.finditer(text):
        spans.append((start, mark.end()))
        start = mark.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _outcome_clause(family: str, verb: str, text: str) -> tuple[int, int, int] | None:
    """(clause start, sentence end, clause end) of the first asserted outcome of the segment.

    The record noun is the clause subject; the sentence is no question, has no doubt marker,
    and nothing before the outcome or in its own clause negates, conditions or defers it.
    """
    pattern = re.compile(
        rf"\b(?:{_FAMILY_NOUNS[family]})s?"
        rf"(?:\s+(?:no\.?\s*|number\s+|#\s*)?\d+(?:[ \u00a0]\d{{3}})*)?"
        rf"\s+(?:(?:is|are|was|were|has\s+been|have\s+been|be|been)\s+)?"
        rf"{'pass(?:ed|es)' if verb == 'passed' else verb}\b",  # "the bill passes" is direct
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        start, end = next(span for span in _sentences(text) if span[0] <= match.start() < span[1])
        sentence = re.sub(_UNOPPOSED, " ", text[start:end], flags=re.IGNORECASE)
        lead = _SUBJECT_LEAD.search(text[start : match.start()])
        rest = text[match.end() : end]
        own = rest[: m.start()] if (m := _CLAUSE_END.search(rest)) else rest
        contrasts = re.findall(r"\b(?:but|although|though|while)\b(.*)", rest, re.IGNORECASE)
        if (
            lead is None
            or text[start:end].rstrip().endswith("?")
            or _DOUBT.search(sentence)
            or _SOURCE_BLOCKERS.search(text[start : match.start()])
            or _SOURCE_BLOCKERS.search(re.sub(_UNOPPOSED, " ", own, flags=re.IGNORECASE))
            or _NOT_FINAL.search(own)
            or _HISTORY.search(text[start:end])
            or any(_NEGATION.search(re.sub(_UNOPPOSED, " ", c, flags=re.I)) for c in contrasts)
        ):
            continue
        return start + lead.start(), end, match.end() + len(own)
    return None


def _crosses_boundary(
    family: str,
    number: str,
    segments: Sequence[Mapping[str, Any]],
    record: int,
    outcome: int,
    sentence_end: int,
) -> bool:
    """A different labelled record, agenda header or topic break up to the outcome sentence end."""
    for index in range(record + 1, outcome + 1):
        text = str(segments[index].get("text", ""))
        if index == outcome:
            text = text[:sentence_end]
        refs = {(kind, canonical) for kind, canonical, _label, _span in _record_refs(text)}
        if refs - {(family, number)}:
            return True
        if (_HEADER.search(text) or _TOPIC_BREAK.search(text)) and not _header_states_record(
            family, number, text
        ):
            return True
    return False


def _states_record(family: str, number: str, text: str) -> bool:
    """The text names the record, with no other primary record or unlabelled list of them."""
    named = (family, number) in {(f, n) for f, n, _label, _span in _record_refs(text)}
    return named and _header_states_record(family, number, text)


def _vote_phase(
    pool: set[int], texts: Mapping[int, str], record: int, outcome: int, clause: tuple[int, int]
) -> list[tuple[int, str]]:
    """The outcome clause and the sentences of the same vote right before it.

    They come from the outcome segment and the one before it, stopping at another vote's
    sentence (an amendment or another outcome) or a labelled record.
    """
    units = [(outcome, texts[outcome][slice(*clause)])]
    for index in (outcome, outcome - 1):
        if index < record or index not in pool:
            break
        text = texts[index][: clause[0]] if index == outcome else texts[index]
        for start, end in reversed(_sentences(text)):
            sentence = text[start:end]
            if (
                _OTHER_VOTE.search(sentence)
                or _record_refs(sentence)
                or _NOT_FINAL.search(sentence)
                or _HISTORY.search(sentence)
            ):
                return units
            units.append((index, sentence))
    return units


def outcome_core_candidate(
    text: str,
    segments: Sequence[Mapping[str, Any]],
    pool: Iterable[int],
    *,
    cited: Iterable[int] = (),
    max_refs: int | None = None,
) -> tuple[str, tuple[int, ...]] | None:
    """(core text, candidate segment positions) of a decision item, or None when not offered.

    ``pool`` holds the positions of the item's strictly closed candidate pool; ``segments`` is
    the full snapshot. The core needs exactly one typed primary record and one explicit outcome
    verb in the item, no negation, condition, recommendation, future vote or question, a pool
    segment that states that record alone (an explicit agenda header of it first; an unlabelled
    list of records is no statement), a later pool sentence asserting that outcome with the
    record noun as its subject, no other record, header or topic break up to that sentence's
    end, and no failure statement in the pool. An item tally or "as amended" joins only when the
    outcome's own vote states it: its clause or the sentences right before it, back to another
    vote ("N in favor, unopposed" is N-0; a roll call is never counted). Supporting positions
    are ordered; with ``max_refs`` they fit with ``cited``.
    """
    refs = _record_refs(text)
    if text.rstrip().endswith("?") or _item_blocked(text, refs):
        return None
    records = {(family, number) for family, number, _label, _span in refs}
    verbs = _ITEM_OUTCOME.findall(text)
    if len(records) != 1 or len(verbs) != 1:
        return None
    family, number = next(iter(records))
    record_span = refs[0][3]
    # No singular label ends in "s": "Appointments 12" is plural, like a following list.
    if re.search(r"s\s+\S*\d", text[slice(*record_span)], re.I) or _LIST_AFTER.match(
        text, record_span[1]
    ):
        return None
    verb = verbs[0].lower()
    if verb not in _FAMILY_VERBS.get(family, ()):
        return None
    pool = sorted(set(pool))
    texts = {index: str(segments[index].get("text", "")) for index in pool}
    if any(_SOURCE_FAILURE.search(texts[index]) for index in pool):
        return None
    for outcome in reversed(pool):  # the latest outcome: after the vote, not a motion's echo
        found = _outcome_clause(family, verb, texts[outcome])
        if found is None:
            continue
        clause_start, sentence_end, clause_end = found
        # A segment states the record only with no other primary record or list of them; the
        # outcome segment is read up to its outcome sentence, so a later agenda may follow.
        mentions = [
            index
            for index in pool
            if index <= outcome
            and _states_record(
                family, number, texts[index][:sentence_end] if index == outcome else texts[index]
            )
        ]
        headers = [i for i in mentions if _GENERIC_HEADER.search(texts[i])]
        before = headers or mentions
        if not before:
            continue
        record = before[-1]
        if _crosses_boundary(family, number, segments, record, outcome, sentence_end):
            continue
        # A noun-only outcome under a header listing several records names none of them.
        clause_refs = {
            (f, n) for f, n, _l, _s in _record_refs(texts[outcome][clause_start:clause_end])
        }
        if (family, number) not in clause_refs and _ambiguous_agenda(
            family, number, segments, record, outcome, sentence_end
        ):
            return None
        if _retracted_later(family, number, verb, segments, outcome, clause_end):
            return None
        support = {record, outcome}
        spans = [record_span, _ITEM_OUTCOME.search(text).span()]  # type: ignore[union-attr]
        phase = _vote_phase(set(pool), texts, record, outcome, (clause_start, clause_end))
        tally = _item_tally(text)
        if tally:
            (votes, span) = tally
            stated = [
                (index, (_count(m["n"]), 0 if m["un"] else _count(m["m"])))
                for index, unit in phase
                for m in _SOURCE_TALLY.finditer(unit)
            ]
            if len(stated) == 1 and stated[0][1] == votes:
                support.add(stated[0][0])
                spans.append(span)
        qualifier = _AS_AMENDED.search(text)
        amended = [index for index, unit in phase if _AS_AMENDED.search(unit)]
        if qualifier and amended:
            support.add(amended[0])
            spans.append(qualifier.span())
        positions = tuple(sorted(support))
        if max_refs is not None and len(set(cited) | support) > max_refs:
            return None
        return _join(text, spans), positions
    return None


def _join(text: str, spans: list[tuple[int, int]]) -> str:
    """The literal spans in item order; a dropped gap reads as a space, or a comma it held."""
    pieces: list[str] = []
    last = None
    for start, end in sorted(spans):
        if last is not None:
            gap = text[last:start]
            pieces.append(gap if not gap.strip() else ", " if "," in gap else " ")
        pieces.append(text[start:end])
        last = end
    return "".join(pieces)
