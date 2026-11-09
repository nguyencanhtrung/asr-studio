"""Strict (registered Gemini) structured context: a far agenda header and a named voter's block.

All transcripts are synthetic. Each segment is 1 second long; `gap` spaces segment starts.
"""

import pytest

from asr_studio.notes_context import link_antecedent_context

FILLER = "The speaker continues on the general budget."


def _snapshot(texts, gap=12.5):
    return {
        "segments": [
            {"id": f"{i:032x}", "start_seconds": i * gap, "end_seconds": i * gap + 1, "text": t}
            for i, t in enumerate(texts)
        ]
    }


def _link(snapshot, text, cited, *, strict=True, section="decisions", max_refs=200):
    document = {"schema_version": 1, "overview": [], section: []}
    document[section] = [{"text": text, "source_ids": [f"{i:032x}" for i in cited]}]
    linked, _ = link_antecedent_context(
        document, snapshot, max_refs_per_item=max_refs, strict=strict
    )
    assert linked[section][0]["text"] == text  # prose is never edited
    return [int(ref, 16) for ref in linked[section][0]["source_ids"]]


# ----- (1) far explicit agenda header for a literal record number -------------------------

HEADER = "Agenda item four. Council Bill 204877 relating to tenant notices."
BILL_ITEM = "Council Bill 204877 passed by 7-0 vote"
VOTE = "Seven in favor, none opposed. The bill passed."


def _far_header(*between, gap=12.5):
    """Header, then 24 segments (25 back, 300 s at the default gap), then the cited result."""
    fillers = [FILLER] * (24 - len(between))
    return _snapshot([HEADER, *between, *fillers, VOTE], gap=gap)


def test_far_header_links_only_in_strict_mode_and_adds_no_bridge():
    snapshot = _far_header(gap=311 / 25)  # 25 segments and 311 s back
    assert _link(snapshot, BILL_ITEM, [25]) == [0, 25]
    assert _link(snapshot, BILL_ITEM, [25], strict=False) == [25]  # default unchanged


def test_header_is_not_linked_for_a_wrong_new_or_ambiguous_agenda_item():
    wrong = _snapshot([HEADER.replace("204877", "204878"), *[FILLER] * 24, VOTE])
    assert _link(wrong, BILL_ITEM, [25]) == [25]
    new_header = _far_header("Agenda item five. Council Bill 204880 relating to parks.")
    assert _link(new_header, BILL_ITEM, [25]) == [25]
    next_item = _far_header("Please read the next agenda item.")
    assert _link(next_item, BILL_ITEM, [25]) == [25]
    two_bills = _snapshot([HEADER + " Also Council Bill 204899.", *[FILLER] * 24, VOTE])
    assert _link(two_bills, BILL_ITEM, [25]) == [25]
    unlabelled = _snapshot(["We discussed 204877 last week.", *[FILLER] * 24, VOTE])
    assert _link(unlabelled, BILL_ITEM, [25]) == [25]


def test_header_beyond_the_structured_window_is_not_linked():
    too_far = _snapshot([HEADER, *[FILLER] * 32, VOTE])  # 33 segments back
    assert _link(too_far, BILL_ITEM, [33]) == [33]
    too_long = _far_header(gap=361 / 25)  # 361 s back
    assert _link(too_long, BILL_ITEM, [25]) == [25]


def test_number_already_cited_needs_no_header():
    snapshot = _far_header()
    assert _link(snapshot, BILL_ITEM, [0, 25]) == [0, 25]


def _generic_plus_header(generic_back):
    """A name bridge of `generic_back` IDs (generic rule) and a far header (structured)."""
    texts = [HEADER, *[FILLER] * 24, VOTE]
    texts[25 - generic_back] = "Ms. Oduya, you have the floor."
    return _snapshot(texts, gap=5), "Ms. Oduya: " + BILL_ITEM


def test_structured_link_never_displaces_generic_links_over_the_cap():
    snapshot, text = _generic_plus_header(12)
    generic = list(range(13, 26))
    assert _link(snapshot, text, [25], strict=False) == generic
    assert _link(snapshot, text, [25]) == generic  # 13 would exceed 12: header not admitted
    snapshot, text = _generic_plus_header(11)
    assert _link(snapshot, text, [25]) == [0, *range(14, 26)]  # 11 + 1 fits
    # The per-item reference cap also admits only a complete link.
    assert _link(snapshot, text, [25], max_refs=12) == list(range(14, 26))


# ----- (2) a named voter's next explicit vote block ---------------------------------------

PRAISE = "Okafor, thank you for voting your conscience on this."
ROLL = [
    "With that we will vote on Council Bill 204877. Clerk, please call the roll.",
    "Lindqvist. Aye. Okafor. Now.",  # literal uncertain heard vote; never interpreted
    "Seven in favor, one opposed.",
    "The bill passed.",
]
VOTER_ITEM = "Praise for Okafor voting against the measure"


def _voter(*between, roll=ROLL, gap=200 / 16):
    """Praise (cited), 15 segments over ~200 s, then the roll call block."""
    fillers = [FILLER] * (15 - len(between))
    return _snapshot([PRAISE, *between, *fillers, *roll], gap=gap)


def test_named_voter_links_the_complete_upcoming_vote_block_in_strict_mode_only():
    snapshot = _voter()
    assert _link(snapshot, VOTER_ITEM, [0], section="key_topics") == [0, 16, 17, 18, 19]
    assert _link(snapshot, VOTER_ITEM, [0], section="key_topics", strict=False) == [0]


def test_voter_block_is_not_linked_across_agenda_or_for_a_wrong_person():
    cross = _voter("Please read the next agenda item.")
    assert _link(cross, VOTER_ITEM, [0], section="key_topics") == [0]
    wrong = _voter(roll=[ROLL[0], "Lindqvist. Aye. Marsh. Aye.", *ROLL[2:]])
    assert _link(wrong, VOTER_ITEM, [0], section="key_topics") == [0]
    unnamed_item = "Praise for voting against the measure"
    assert _link(_voter(), unnamed_item, [0], section="key_topics") == [0]
    uncited = _link(_voter(), VOTER_ITEM, [1], section="key_topics")  # name not in cited pool
    assert uncited == [1]


def test_voter_block_needs_a_stated_result_within_bounds():
    no_result = _voter(roll=[*ROLL[:3], "Please read the next agenda item. It passed."])
    assert _link(no_result, VOTER_ITEM, [0], section="key_topics") == [0]
    end_of_source = _voter(roll=ROLL[:3])
    assert _link(end_of_source, VOTER_ITEM, [0], section="key_topics") == [0]
    too_long = _voter(gap=361 / 16)
    assert _link(too_long, VOTER_ITEM, [0], section="key_topics") == [0]
    too_far = _snapshot([PRAISE, *[FILLER] * 30, *ROLL], gap=5)  # result 34 segments ahead
    assert _link(too_far, VOTER_ITEM, [0], section="key_topics") == [0]


def test_oversized_vote_block_is_rejected_not_truncated():
    long_roll = [ROLL[0], *[f"Member {i}. Aye." for i in range(10)], ROLL[1], *ROLL[2:]]
    snapshot = _snapshot([PRAISE, *long_roll], gap=5)  # 14-ID block > 12
    assert _link(snapshot, VOTER_ITEM, [0], section="key_topics") == [0]


def test_two_voters_with_different_blocks_conflict():
    second = [r.replace("204877", "204880").replace("Okafor", "Marsh") for r in ROLL]
    snapshot = _snapshot([PRAISE, *ROLL, "Marsh, thank you as well.", *second], gap=5)
    text = "Okafor voting no and Marsh voting yes"
    assert _link(snapshot, text, [0, 5], section="key_topics") == [0, 5]
    assert _link(snapshot, "Okafor voting no", [0, 5], section="key_topics") == [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize("during_block", [False, True])
@pytest.mark.parametrize(
    "header",
    [
        "Item 7. Budget.",
        "Council Bill 555555 relating to budget.",
        "Agenda item seven. Budget.",
        "We will vote on Bill 555555.",
    ],
)
def test_voter_stops_at_each_header_before_or_inside_the_vote_block(header, during_block):
    snapshot = _voter(roll=[ROLL[0], header, *ROLL[1:]]) if during_block else _voter(header)
    assert _link(snapshot, VOTER_ITEM, [0], section="key_topics") == [0]


# ----- (3) acronym, duration and same-agenda provenance links ------------------------------


def _overview(snapshot, text, cited, *, strict=True):
    document = {"schema_version": 1, "overview": [{"text": text, "source_ids": []}]}
    document["overview"][0]["source_ids"] = [f"{i:032x}" for i in cited]
    linked, _ = link_antecedent_context(document, snapshot, max_refs_per_item=200, strict=strict)
    assert linked["overview"][0]["text"] == text
    return [int(ref, 16) for ref in linked["overview"][0]["source_ids"]]


def test_acronym_links_its_full_name_without_a_bridge_and_only_strict_in_the_overview():
    snapshot = _snapshot(
        [
            "I speak for the Board of Water Works today.",
            FILLER,
            FILLER,
            "The board asks you to approve the pipe plan.",
        ]
    )
    text = "BWW asks council to approve the pipe plan"
    assert _link(snapshot, text, [3], section="key_topics") == [0, 3]
    assert _overview(snapshot, text, [3]) == [0, 3]
    assert _overview(snapshot, text, [3], strict=False) == [3]  # default unchanged
    assert _link(snapshot, text, [3], strict=False, section="key_topics") == [3]
    assert _link(snapshot, text, [0, 3], section="key_topics") == [0, 3]  # already cited
    other = _snapshot(["I speak for the Board of Water Rights today.", FILLER, FILLER, FILLER])
    assert _link(other, text, [3], section="key_topics") == [3]  # initials differ


def test_acronym_takes_the_nearest_run_and_ignores_an_earlier_far_mention():
    snapshot = _snapshot(
        [
            "Hi, I run the Valley Transit League.",
            *[FILLER] * 5,
            "The fare survey closed.",
            "Speaking for Valley Transit League now.",
            "We support the fare change.",
        ]
    )
    assert _overview(snapshot, "VTL supports fare change", [6, 8]) == [6, 7, 8]


def test_duration_links_the_same_amount_and_unit_with_its_bridge():
    snapshot = _snapshot(
        [
            "We propose an eight week pilot of the night bus.",
            "Riders asked for it.",
            "So I support giving the department more time.",
        ]
    )
    text = "Member supports 8-week pilot giving more time"
    assert _link(snapshot, text, [2], section="key_topics") == [0, 1, 2]
    assert _link(snapshot, text, [2], strict=False, section="key_topics") == [2]
    assert _link(snapshot, "Member supports 9-week pilot", [2], section="key_topics") == [2]
    assert _link(snapshot, "Member supports 8-day pilot", [2], section="key_topics") == [2]
    vi = _snapshot(["Chúng tôi đề xuất thí điểm 3 tháng.", "Tôi ủng hộ thêm thời gian."])
    assert _link(vi, "Ủng hộ thí điểm three-month", [1], section="key_topics") == [0, 1]
    far = _snapshot(["We propose an eight week pilot.", *[FILLER] * 16, "I support it."])
    assert _link(far, text, [17], section="key_topics") == [17]  # outside 16 segments


SPAN_HEADER = "Agenda item two. Council Bill 300417 relating to bike lanes. Requiring a lane audit."


def test_same_agenda_word_links_the_span_between_cited_header_and_outcome():
    texts = [
        SPAN_HEADER,
        FILLER,
        "The committee voted for a reroute of the corridor.",
        "Seven in favor, none opposed.",
        "The bill passed.",
        "Later someone mentioned a detour on another street.",
    ]
    text = "Passed Council Bill 300417 requiring lane audit with reroute and detour"

    def snap(rows):
        return _snapshot(rows, gap=50)  # header 200 s back: past the generic 180 s window

    assert _link(snap(texts), text, [0, 4]) == [0, 2, 4]  # "detour" after the outcome stays out
    assert _link(snap(texts), text, [0, 4], strict=False) == [0, 4]
    other = [*texts[:2], "Agenda item three. Council Bill 300418.", *texts[2:]]
    assert _link(snap(other), text, [0, 5]) == [0, 5]  # another header between
    assert _link(snap(texts), text, [0]) == [0]  # no cited outcome: no span
    wrong = [SPAN_HEADER.replace("300417", "300419"), *texts[1:]]
    assert _link(snap(wrong), text, [0, 4]) == [0, 4]  # header is another record
    assert _link(_snapshot(texts, gap=100), text, [0, 4]) == [0, 4]  # outcome past 360 s
    # A different agenda header with an outcome word is not this item's outcome.
    next_item = [*texts[:4], "Agenda item three. Council Bill 300418 approved.", *texts[5:]]
    assert _link(snap(next_item), text, [0, 4]) == [0, 4]
    same = [*texts[:4], "Council Bill 300417 passed.", *texts[5:]]
    assert _link(snap(same), text, [0, 4]) == [0, 2, 4]  # restates this record: allowed


def test_new_links_never_displace_prior_links_or_exceed_the_cap():
    snapshot = _snapshot(
        ["Board of Parks here.", "We propose a six month review.", *[FILLER] * 9, "Go ahead."]
    )
    text = "Board of Parks backs six-month review"
    # Generic organization link (11 IDs) is kept; the 10-ID duration link does not also fit.
    assert _link(snapshot, text, [11], section="key_topics", max_refs=12) == list(range(12))
    assert _link(snapshot, "Members back six-month review", [11], section="key_topics") == [
        *range(1, 12)
    ]
    # The generic link no longer fits; the complete duration link still does, never truncated.
    assert _link(snapshot, text, [11], section="key_topics", max_refs=11) == [*range(1, 12)]
    assert _link(snapshot, text, [11], section="key_topics", max_refs=10) == [11]


ORG_NAME = "Department of Harbor Infrastructure"
ORG_ITEM = f"{ORG_NAME} study commenced in January"


def _far_named_org(distance=28, gap=15.5):
    return _snapshot(
        [
            f"The {ORG_NAME} will conduct the study.",
            *[FILLER] * (distance - 1),
            "The department began the study in January.",
        ],
        gap=gap,
    )


def test_exact_named_organization_gets_one_bounded_context_id():
    source = _far_named_org()
    assert _link(source, ORG_ITEM, [28]) == [0, 28]
    assert _link(source, ORG_ITEM, [28], strict=False) == [28]
    assert _link(source, ORG_ITEM, [28], section="overview") == [28]


@pytest.mark.parametrize("distance,expected", [(32, [0, 32]), (33, [33])])
def test_named_organization_context_respects_the_segment_window(distance, expected):
    assert _link(_far_named_org(distance, gap=10), ORG_ITEM, [distance]) == expected


@pytest.mark.parametrize("gap,expected", [(600 / 28, [0, 28]), (600.1 / 28, [28])])
def test_named_organization_context_respects_its_time_window(gap, expected):
    assert _link(_far_named_org(gap=gap), ORG_ITEM, [28]) == expected


def test_named_organization_context_never_crosses_an_agenda_header():
    source = _far_named_org()
    source["segments"][20]["text"] = "Agenda item seven. Council Bill 304998 relating to parks."
    assert _link(source, ORG_ITEM, [28]) == [28]


def test_named_organization_context_requires_exact_name_and_whole_admission():
    source = _far_named_org()
    source["segments"][0]["text"] = source["segments"][0]["text"].replace(
        "Infrastructure", "Infrastructurs"
    )
    assert _link(source, ORG_ITEM, [28]) == [28]
    source = _far_named_org()
    assert _link(source, ORG_ITEM, [28], max_refs=1) == [28]
    assert _link(source, ORG_ITEM, [28], max_refs=2) == [0, 28]
    source, text = _generic_plus_header(12)
    source["segments"][1]["text"] = f"The {ORG_NAME} will conduct the study."
    assert _link(source, text + "; " + ORG_ITEM, [25]) == list(range(13, 26))
