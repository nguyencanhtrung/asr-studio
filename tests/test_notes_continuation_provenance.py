"""Opt-in strict completion: quantity continuation and the long agenda header window."""

import copy

import pytest

from asr_studio.notes_context import link_antecedent_context


def _snapshot(*rows):
    """rows: (start_seconds, text); each segment lasts one second."""
    return {
        "segments": [
            {"id": f"{i:032x}", "start_seconds": s, "end_seconds": s + 1, "text": t}
            for i, (s, t) in enumerate(rows)
        ]
    }


def _link(section, text, cited, snapshot, **flags):
    document = {
        "schema_version": 1,
        section: [{"text": text, "source_ids": [f"{i:032x}" for i in cited]}],
    }
    frozen = copy.deepcopy(document)
    out, _counts = link_antecedent_context(document, snapshot, max_refs_per_item=200, **flags)
    assert document == frozen
    assert out[section][0]["text"] == text
    return sorted(int(ref, 16) for ref in out[section][0]["source_ids"])


STRICT = {"strict": True, "complete_bindings": True}

# --- quantity continuation ---------------------------------------------------------------

QUANTITY = "The plan would build 730 homes for 5100 residents"


def _cut(continuation, *, gap=1.0, cut="The plan would build 730 homes for up to"):
    return _snapshot(
        (0.0, "We heard 5100 dollars was spent and residents spoke at length about it."),
        (10.0, cut),
        (11.0 + gap, continuation),
        (30.0, "Thank you."),
    )


def test_cut_sentence_continuation_adds_the_amount_and_noun():
    # The amount and the noun each repeat elsewhere in the pool; only the phrase is missing.
    assert _link(
        "key_topics", QUANTITY, [0, 1], _cut("5100 residents. So it costs more."), **STRICT
    ) == [
        0,
        1,
        2,
    ]


def test_continuation_is_opt_in_only():
    snapshot = _cut("5100 residents. So it costs more.")
    assert _link("key_topics", QUANTITY, [0, 1], snapshot) == [0, 1]
    assert _link("key_topics", QUANTITY, [0, 1], snapshot, strict=True) == [0, 1]
    assert _link("key_topics", QUANTITY, [0, 1], snapshot, complete_bindings=True) == [0, 1]


@pytest.mark.parametrize(
    ("continuation", "kwargs"),
    [
        ("5100 people. So it costs more.", {}),  # different noun
        ("5100. So it costs more.", {}),  # the number alone
        ("5100 residents halls. So it costs more.", {}),  # a longer, different noun phrase
        ("5100 residents. So it costs more.", {"cut": "The plan would build 730 homes."}),  # ended
        ("5100 residents. So it costs more.", {"gap": 200.0}),  # past the forward window
        ("Next agenda item. 5100 residents.", {}),  # topic break
        ("Bill 4321 is next. 5100 residents.", {}),  # another header
    ],
)
def test_continuation_rejects_unbound_or_partial_evidence(continuation, kwargs):
    assert _link("key_topics", QUANTITY, [0, 1], _cut(continuation, **kwargs), **STRICT) == [0, 1]


def test_continuation_is_bounded_to_four_segments():
    rows = [
        (0.0, "We discussed 5100 dollars and residents."),
        (10.0, "The plan builds 730 homes for"),
    ]
    rows += [(11.0 + i, "a long and winding") for i in range(4)]
    rows += [(16.0, "5100 residents.")]
    assert _link("key_topics", QUANTITY, [0, 1], _snapshot(*rows), **STRICT) == [0, 1]
    rows.pop(2)
    assert _link("key_topics", QUANTITY, [0, 1], _snapshot(*rows), **STRICT) == [0, 1, 2, 3, 4, 5]


# --- long agenda header window -------------------------------------------------------------

DECISION = "confirmed Appointment 4321 of the director as amended"


def _agenda(header, *, before=10, step=20.0, between=""):
    """Header, `before` remarks, a same-record motion, 20 remarks (400 s), cited outcome."""
    rows = [(0.0, "The next report. Please read it."), (10.0, header)]
    rows += [(20.0 + i * step, f"Remarks about the department, part {i}.") for i in range(before)]
    t = 20.0 + before * step
    mention = len(rows)
    rows += [(t, "Thank you. I move to amend appointment 4321 to add a letter.")]
    if between:
        rows += [(t + 1, between)]
    rows += [(t + 10 + i * 20.0, f"More praise, part {i}.") for i in range(20)]
    cited = [len(rows), len(rows) + 1]
    rows += [
        (t + 420, "Those in favor of confirming the appointment as amended vote."),
        (t + 421, "The motion carries and the appointment is confirmed."),
    ]
    return _snapshot(*rows), mention, cited


HEADER = "The committee report, agenda item one, appointment 4321, appointment as director."


def test_long_agenda_links_the_actual_header_and_keeps_the_mention():
    snapshot, mention, cited = _agenda(HEADER)
    assert _link("decisions", DECISION, cited, snapshot, **STRICT) == [1, mention, *cited]


def test_long_agenda_window_is_opt_in_only():
    snapshot, _mention, cited = _agenda(HEADER)
    assert _link("decisions", DECISION, cited, snapshot, strict=True) == cited
    assert _link("decisions", DECISION, cited, snapshot) == cited


def test_anchored_record_header_without_agenda_words():
    snapshot, mention, cited = _agenda("Appointment 4321, appointment of the director.")
    assert _link("decisions", DECISION, cited, snapshot, **STRICT) == [1, mention, *cited]


@pytest.mark.parametrize(
    ("header", "kwargs"),
    [
        ("Agenda item one, appointment 9876, a different director.", {}),  # wrong record
        ("Agenda item one, appointment 4321 and appointment 9876.", {}),  # multiple primary records
        ("Remarks about appointment of a director.", {}),  # no header at all
        (HEADER, {"between": "Agenda item two, resolution 5555."}),  # competing agenda
        (HEADER, {"between": "We note resolutions 5555 and 5556 are related."}),  # competing record
        (HEADER, {"before": 50, "step": 1.0}),  # past 64 segments
        (HEADER, {"before": 20, "step": 30.0}),  # past 900 seconds
    ],
)
def test_long_agenda_rejects_other_or_unbounded_headers(header, kwargs):
    snapshot, _mention, cited = _agenda(header, **kwargs)
    assert _link("decisions", DECISION, cited, snapshot, **STRICT) == cited


def test_short_window_header_link_is_retained():
    # A mention inside the old 32 segments / 360 seconds keeps its original link (strict alone),
    # and the opt-in window adds the actual header before it.
    rows = [(0.0, HEADER), (10.0, "Thank you. I move to amend appointment 4321 to add a letter.")]
    rows += [(20.0 + i * 15.0, f"More praise, part {i}.") for i in range(18)]
    rows += [(300.0, "Those in favor of confirming the appointment as amended vote.")]
    rows += [(301.0, "The motion carries and the appointment is confirmed.")]
    snapshot = _snapshot(*rows)
    assert _link("decisions", DECISION, [20, 21], snapshot, strict=True) == [1, 20, 21]
    assert _link("decisions", DECISION, [20, 21], snapshot, **STRICT) == [0, 1, 20, 21]


def test_cap_admits_only_complete_links():
    snapshot, mention, cited = _agenda(HEADER)
    document = {"decisions": [{"text": DECISION, "source_ids": [f"{i:032x}" for i in cited]}]}
    out, _ = link_antecedent_context(document, snapshot, max_refs_per_item=3, **STRICT)
    assert sorted(int(r, 16) for r in out["decisions"][0]["source_ids"]) == [1, *cited]
