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
