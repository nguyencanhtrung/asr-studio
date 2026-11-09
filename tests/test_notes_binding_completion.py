"""Opt-in binding completion: typed record headers and quantitative phrases.

All transcripts are synthetic. Each segment is 1 second long; `gap` spaces segment starts.
"""

import copy

import pytest

from asr_studio import notes_context as nc
from asr_studio.notes_context import link_antecedent_context

FILLER = "The speaker continues on the general budget."


def _snapshot(texts, gap=10.0):
    return {
        "segments": [
            {"id": f"{i:032x}", "start_seconds": i * gap, "end_seconds": i * gap + 1, "text": t}
            for i, t in enumerate(texts)
        ]
    }


def _document(text, cited, section):
    document = {"schema_version": 1, "overview": [], "key_topics": [], "decisions": []}
    document[section] = [{"text": text, "source_ids": [f"{i:032x}" for i in cited], "owner": None}]
    return document


def _link(snapshot, text, cited, *, section="key_topics", max_refs=200, **options):
    options.setdefault("strict", True)
    options.setdefault("complete_bindings", True)
    document = _document(text, cited, section)
    before = copy.deepcopy(document)
    linked, _ = link_antecedent_context(document, snapshot, max_refs_per_item=max_refs, **options)
    assert document == before  # the input is never mutated
    item = linked[section][0]
    assert {k: v for k, v in item.items() if k != "source_ids"} == {
        k: v for k, v in before[section][0].items() if k != "source_ids"
    }  # text and metadata are never edited
    return [int(ref, 16) for ref in item["source_ids"]]


def _cases(texts, item, cited, **kwargs):
    """(new, legacy) pools for the same item."""
    snapshot = _snapshot(texts, **{k: kwargs.pop(k) for k in ("gap",) if k in kwargs})
    return (
        _link(snapshot, item, cited, **kwargs),
        _link(snapshot, item, cited, **{**kwargs, "complete_bindings": False}),
    )


# ----- (1) typed record headers ----------------------------------------------------------

BILL_TEXTS = [
    FILLER,
    "Agenda item 7. Council Bill 230 456 relating to tenant notices.",
    "The committee recommends the bill pass.",
    "Thank you. Colleagues, this legislation updates the notice rules.",
    "It also funds outreach to renters.",
]
BILL_ITEM = "Council Bill 230456 updates tenant notice rules and funds renter outreach"


@pytest.mark.parametrize("section", ["key_topics", "overview"])
def test_typed_record_links_the_nearest_header_without_a_bridge(section):
    new, legacy = _cases(BILL_TEXTS, BILL_ITEM, [3, 4], section=section)
    assert new == [1, 3, 4]
    assert 1 not in legacy


@pytest.mark.parametrize(
    ("header", "item"),
    [
        ("Agenda item 12 Council Bill 230 456 relating to notices.", BILL_ITEM),
        ("Agenda item 3. Resolution 42 761 relating to notices.", "Resolution 42761 adopted"),
        ("Agenda item 3. Ordinance 412 650 relating to notices.", "Ordinance 412650 amended"),
        ("Agenda item 3. Appointment 2468 of a board member.", "Appointment 2468 confirmed"),
        ("Item 3, Bill 230456, relating to notices.", "Bill 230456 amends notice rules"),
    ],
)
def test_record_aliases_and_grouped_numbers_after_a_label(header, item):
    texts = [FILLER, header, "The committee recommends it.", "Thank you. This updates it."]
    assert _cases(texts, item, [3], section="overview") == ([1, 3], [3])


def test_cb_abbreviation_needs_a_legal_cue():
    # "Bill" alone does not spell the acronym, so the legacy acronym link stays out.
    header = "Agenda item 7. Bill 230 456 relating to tenant notices."
    texts = [
        FILLER,
        header,
        BILL_TEXTS[2],
        "Thank you. Colleagues, this updates the rules.",
        FILLER,
    ]
    assert _cases(texts, "CB 230456 implements the notice amendment", [3])[0] == [1, 3]
    pool_cue = [FILLER, header, BILL_TEXTS[2], BILL_TEXTS[3], FILLER]
    assert _cases(pool_cue, "CB 230456 updates notices", [3])[0] == [1, 3]
    assert _cases(texts, "CB 230456 updates notices", [3])[0] == [3]
    assert _cases(texts, "CB 230456 updates notices", [3], section="overview")[0] == [3]


def test_generic_header_states_an_untyped_primary_beside_another_family():
    header = "Agenda Item to cancel 230456 relating to notices amending ordinance 412 652."
    texts = [FILLER, header, "Thank you. This council bill amends ordinance 412650."]
    item = "Council Bill 230456 extends Ordinance 412650"
    assert _cases(texts, item, [2], section="overview")[0] == [1, 2]
    # The same header never confirms a different ordinance number.
    assert _cases(texts, "Ordinance 412651 extended", [2])[0] == [2]


@pytest.mark.parametrize(
    "header",
    [
        "Agenda item 7. Council Bill 230457 relating to tenant notices.",  # different same family
        "Agenda item 7. Council Bill 230456 and Council Bill 230457.",  # conflicting same family
        "Agenda item 7 relating to 230457 and notices.",  # different untyped primary
        "Agenda item 7 relating to 230456 and 230457.",  # two primaries
        "Agenda item 7. Ordinance 230456 relating to notices.",  # another family only
        "Agenda item 7 relating to permit 230456.",  # a technical identifier
        "Agenda items 230 456 and 7 relating to notices.",  # grouped digits without a label
        "Agenda item 7. Council Bill 230 4567 relating to notices.",  # a guessed digit group
        "Agenda item 7. Council Bill 23 0456 relating to notices.",  # not three-digit groups
        "Agenda item 7. Council Bill 2304 relating to notices.",  # a truncated number
        "Ordinance 412650 relating to 230456.",  # untyped only in a generic agenda header
    ],
)
def test_record_header_negatives(header):
    texts = [FILLER, header, BILL_TEXTS[2], BILL_TEXTS[3], BILL_TEXTS[4]]
    assert _cases(texts, BILL_ITEM, [3, 4], section="overview") == ([3, 4], [3, 4])


def test_record_search_stops_at_another_agenda_header_and_window_bounds():
    other = [*BILL_TEXTS[:3], "Agenda item 8. Resolution 42761 on parks.", *BILL_TEXTS[3:]]
    assert _cases(other, BILL_ITEM, [4, 5])[0] == [4, 5]
    # Without the other header the same far span links.
    near = [*BILL_TEXTS[:3], FILLER, *BILL_TEXTS[3:]]
    assert _cases(near, BILL_ITEM, [4, 5])[0] == [1, 4, 5]
    far = [*BILL_TEXTS[:3], *[FILLER] * nc.MAX_AGENDA_SEGMENTS, *BILL_TEXTS[3:]]
    assert _cases(far, BILL_ITEM, [len(far) - 2, len(far) - 1])[0] == [len(far) - 2, len(far) - 1]
    at_limit = [BILL_TEXTS[1], *[FILLER] * (nc.MAX_STRUCTURED_SEGMENTS - 1), BILL_TEXTS[3]]
    assert _cases(at_limit, BILL_ITEM, [len(at_limit) - 1])[0] == [0, len(at_limit) - 1]
    # An explicit agenda header 2 segments back: 400 s links (agenda window), 920 s does not.
    assert _cases(BILL_TEXTS, BILL_ITEM, [3, 4], gap=200.0)[0] == [1, 3, 4]
    assert _cases(BILL_TEXTS, BILL_ITEM, [3, 4], gap=460.0)[0] == [3, 4]


def test_record_already_stated_by_the_pool_or_raw_excerpt_is_skipped():
    texts = [*BILL_TEXTS[:3], "Thank you. Council Bill 230 456 updates the rules.", FILLER]
    assert _cases(texts, BILL_ITEM, [3], section="overview")[0] == [3]
    literal = [*BILL_TEXTS[:3], "Thank you. Council Bill 230456 updates the rules.", FILLER]
    assert _cases(literal, BILL_ITEM, [3], section="overview")[0] == [3]
    excerpt = f"Needs review. Source excerpt: “{BILL_TEXTS[3]}” Council Bill 230456"
    assert _cases(BILL_TEXTS, excerpt, [3])[0] == [3]
    short = "Thank you."  # a short cited segment inside the text is no excerpt
    assert _cases([*BILL_TEXTS[:3], short], f"{short} Council Bill 230456", [3])[0] == [1, 3]


def test_short_numbers_are_not_records():
    texts = [FILLER, "Agenda item 7. Bill 23 relating to notices.", BILL_TEXTS[3]]
    assert _cases(texts, "Bill 23 updates notices", [2], section="overview")[0] == [2]


def test_numeric_list_in_the_item_is_not_a_record():
    texts = [FILLER, "Agenda item 7. Council Bill 230 relating to notices.", BILL_TEXTS[3]]
    assert _cases(texts, "Council Bill 23 voted 230 times", [2])[0] == [2]


# ----- (2) quantitative phrases inside the cited span ------------------------------------

SPAN = [
    "The project adds new public space to the station.",
    "Riders asked for more access and better lighting.",
    "The design has four accessible entrances and two elevators.",
    "There are also new bike racks near the plaza.",
    "The project opens next spring for all riders.",
]
QTY_ITEM = "Station project adds four accessible entrances and opens next spring"


@pytest.mark.parametrize(
    "item",
    [
        QTY_ITEM,
        "Station adds 4 Accessible Entrances, opening next spring",
        "Station adds four accessible entrances",
    ],
)
def test_quantity_phrase_links_its_witness_between_cited_runs(item):
    new, legacy = _cases(SPAN, item, [0, 4])
    assert new == [0, 2, 4]
    assert legacy == [0, 4]


@pytest.mark.parametrize(
    ("witness", "item"),
    [
        ("The design has four accessible ramps.", QTY_ITEM),  # a different head noun
        ("The design has four accessible entrances.", "Station adds four accessible exits"),
        ("The design has four accessible entrances.", "The vote was four"),  # amount alone
        ("The design has four of them.", "Station adds four of them"),  # function words
        ("The design has 4 accessible entrances.", "Item 4 accessible entrances reviewed"),
        ("The design has 4 accessible entrances.", "Station adds 3 4 accessible entrances"),
        ("The design has three, four accessible entrances.", QTY_ITEM),  # a number list
        ("The design has 3 4 accessible entrances.", QTY_ITEM),
        ("The design has four accessible entrance doors.", QTY_ITEM),  # the noun goes on
        ("Agenda item 9. Four accessible entrances.", QTY_ITEM),  # another agenda item
        ("The design has five accessible entrances.", QTY_ITEM),  # a different amount
    ],
)
def test_quantity_phrase_negatives(witness, item):
    texts = [SPAN[0], SPAN[1], witness, SPAN[3], SPAN[4]]
    assert _cases(texts, item, [0, 4]) == ([0, 4], [0, 4])


def test_quantity_phrase_is_searched_only_inside_the_cited_span():
    texts = [SPAN[2], SPAN[0], SPAN[1], SPAN[4], SPAN[2]]
    assert _cases(texts, QTY_ITEM, [1, 3])[0] == [1, 3]
    assert _cases(SPAN, QTY_ITEM, [0, 1])[0] == [0, 1]  # one run: no interior


def test_quantity_span_bounds_and_agenda_breaks():
    far = [SPAN[0], *[FILLER] * nc.MAX_STRUCTURED_SEGMENTS, SPAN[2], SPAN[4]]
    end = len(far) - 1
    assert _cases(far, QTY_ITEM, [0, end])[0] == [0, end]
    assert _cases(SPAN, QTY_ITEM, [0, 4], gap=100.0)[0] == [0, 4]  # 400 s apart
    breaking = [SPAN[0], SPAN[2], "Please read the next agenda item.", SPAN[4]]
    assert _cases(breaking, QTY_ITEM, [0, 3])[0] == [0, 3]
    next_item = [SPAN[0], SPAN[2], "Agenda item 9. The project opens next spring."]
    assert _cases(next_item, QTY_ITEM, [0, 2])[0] == [0, 2]


def test_quantity_phrase_prefers_the_longest_exact_phrase_and_skips_covered():
    texts = [SPAN[0], "We get two covered pools.", "We get two covered courts.", SPAN[4]]
    item = "Plan adds two covered courts addressed by schools"
    assert _cases(texts, item, [0, 3])[0] == [0, 2, 3]
    shorter = [SPAN[0], "We get two covered.", "We get two covered courts.", SPAN[4]]
    assert _cases(shorter, item, [0, 3])[0] == [0, 2, 3]
    # A short descriptor alone is never the phrase.
    short = [SPAN[0], "The design has four new.", SPAN[4]]
    assert _cases(short, "Station adds four new entrances", [0, 2])[0] == [0, 2]
    covered = [SPAN[2], SPAN[1], SPAN[2], SPAN[4]]
    assert _cases(covered, QTY_ITEM, [0, 3])[0] == [0, 3]


def test_overview_gets_records_but_no_quantity_link():
    assert _cases(SPAN, QTY_ITEM, [0, 4], section="overview")[0] == [0, 4]


# ----- legacy behavior, ordering and admission -------------------------------------------


@pytest.mark.parametrize(
    ("texts", "item", "cited"),
    [(BILL_TEXTS, BILL_ITEM, [3, 4]), (SPAN, QTY_ITEM, [0, 4])],
)
@pytest.mark.parametrize("section", ["key_topics", "overview", "decisions"])
def test_false_default_and_non_strict_are_the_legacy_result(texts, item, cited, section):
    snapshot = _snapshot(texts)
    document = _document(item, cited, section)
    for strict in (False, True):
        default, default_counts = link_antecedent_context(
            document, snapshot, max_refs_per_item=200, strict=strict
        )
        off = link_antecedent_context(
            document, snapshot, max_refs_per_item=200, strict=strict, complete_bindings=False
        )
        assert off == (default, default_counts)
    non_strict = link_antecedent_context(document, snapshot, max_refs_per_item=200)
    assert non_strict == link_antecedent_context(
        document, snapshot, max_refs_per_item=200, complete_bindings=True
    )


def test_complete_bindings_is_keyword_only():
    with pytest.raises(TypeError):
        link_antecedent_context({}, {"segments": []}, 200, True, True)  # type: ignore[misc]


def test_new_links_keep_old_ids_and_never_truncate():
    # The old strict path adds the outcome after the cited motion; new links come after it.
    texts = [
        "Agenda item 2. Council Bill 230456 relating to notices.",
        "I move to pass the bill with four accessible entrances.",
        "The design has four accessible entrances overall.",
        "Second. All in favor?",
        "The bill passed.",
    ]
    item = "Council Bill 230456 with four accessible entrances overall passed"
    new, legacy = _cases(texts, item, [1, 3], section="decisions")
    assert set(legacy) <= set(new)
    assert [ref for ref in new if ref in legacy] == legacy
    # A cap of one more ID admits no new group; the legacy result is kept unchanged.
    capped = _cases(BILL_TEXTS, BILL_ITEM, [3, 4], max_refs=2)
    assert capped == ([3, 4], [3, 4])


def test_new_groups_respect_the_added_id_budget():
    # The legacy strict path admits a full duration bridge (12 IDs); nothing more joins.
    bridge = ["The extension lasts six months.", *[FILLER] * 11]
    texts = [BILL_TEXTS[1], *bridge, "Thank you. This legislation updates notices for renters."]
    end = len(texts) - 1
    item = "Council Bill 230456 updates notices for six months"
    new, legacy = _cases(texts, item, [end])
    assert len(set(legacy) - {end}) == nc.MAX_ADDED_PER_ITEM
    assert new == legacy
