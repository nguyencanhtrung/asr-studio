import copy
import json

import pytest

from asr_studio import notes_context as nc
from asr_studio.notes_context import link_antecedent_context


def _snapshot(*rows):
    """rows: (start_seconds, speaker label, text)."""
    return {
        "segments": [
            {
                "id": f"{index:032x}",
                "start_seconds": start,
                "end_seconds": start + 1,
                "text": text,
                "speaker_id": label,
            }
            for index, (start, label, text) in enumerate(rows)
        ]
    }


def _sid(index):
    return f"{index:032x}"


def _doc(section, item):
    document = {
        "schema_version": 1,
        "overview": [],
        "key_topics": [],
        "decisions": [],
        "open_questions": [],
        "action_items": [],
        "confirmation_items": [],
    }
    document[section] = [item]
    return document


def _added(document, snapshot, section="key_topics"):
    linked, counts = link_antecedent_context(document, snapshot, max_refs_per_item=200)
    before = set(document[section][0]["source_ids"])
    return [ref for ref in linked[section][0]["source_ids"] if ref not in before], counts, linked


def test_self_introduction_and_bridge_link_a_named_view_across_flipping_labels():
    snapshot = _snapshot(
        (0, "A", "Next speaker please."),
        (5, "B", "Hello, my name is Priya Oduya and I speak for the Lantern"),
        (9, "A", "Guild of Weavers. We think"),  # label flips mid-sentence
        (12, "B", "the studio lease should be renewed for two years."),
    )
    item = {"text": "Ms. Oduya: renew studio lease two years", "source_ids": [_sid(3)]}
    added, counts, _ = _added(_doc("key_topics", item), snapshot)
    assert added == [_sid(1), _sid(2)]
    assert counts == {"items": 1, "added_ids": 2}


def test_recurring_name_uses_the_nearest_occurrence_only():
    snapshot = _snapshot(
        (0, "A", "Thank you, Mr. Varga, for last year's report."),
        *[(10 + i, "A", f"Unrelated budget line {i}.") for i in range(5)],
        (20, "A", "Mr. Varga, please come forward."),
        (25, "C", "I will audit the depot fleet."),
    )
    item = {"text": "Mr. Varga: audit depot fleet", "source_ids": [_sid(7)]}
    added, _, _ = _added(_doc("key_topics", item), snapshot)
    assert added == [_sid(6)]


def test_item_number_antecedent_links_the_vote_to_its_bill():
    snapshot = _snapshot(
        (0, "A", "Please call the roll on council bill 204871."),
        (4, "B", "Aye, aye, aye."),
        (8, "A", "Seven in favor, none opposed. The bill passes."),
        (12, "A", "Item 3.204871 is a decimal and not a bill."),
    )
    decision = {"text": "Council Bill 204871 passed 7-0", "source_ids": [_sid(2)]}
    added, _, _ = _added(_doc("decisions", decision), snapshot, "decisions")
    assert added == [_sid(0), _sid(1)]


def test_organization_matches_despite_asr_spelling_of_its_last_word():
    snapshot = _snapshot(
        (0, "A", "I'm here on behalf of Friends of the Fernbonk."),
        (6, "B", "We oppose the parking plan."),
    )
    item = {"text": "Friends of the Fernbank view: oppose parking plan", "source_ids": [_sid(1)]}
    added, _, _ = _added(_doc("key_topics", item), snapshot)
    assert added == [_sid(0)]
    item["text"] = "Friends of the Harbor view: oppose parking plan"  # another organization
    assert _added(_doc("key_topics", item), snapshot)[0] == []


@pytest.mark.parametrize(
    ("heard", "named"),
    [
        ("County", "Country"),  # different length
        ("Park", "Parts"),  # short and different length
        ("Garden", "Warden"),  # first letter differs
        ("Marina", "Marine"),  # last letters differ
        ("Fernbonk", "Fernbink2"),  # different length
        ("Fernbank", "Fornbonk"),  # two substitutions
    ],
)
def test_distinct_organizations_are_not_joined_by_the_spelling_fallback(heard, named):
    snapshot = _snapshot(
        (0, "A", f"I speak for Friends of the {heard}."),
        (4, "B", "We oppose the parking plan."),
    )
    item = {"text": f"Friends of the {named} view: oppose plan", "source_ids": [_sid(1)]}
    assert _added(_doc("key_topics", item), snapshot)[0] == []


def test_exact_organization_wins_and_ambiguous_near_spellings_stay_unresolved():
    exact = _snapshot(
        (0, "A", "Friends of the Fernbank asked to speak."),
        (3, "A", "Friends of the Fernbonk is next."),
        (6, "B", "We oppose the parking plan."),
    )
    item = {"text": "Friends of the Fernbank view: oppose plan", "source_ids": [_sid(2)]}
    assert _added(_doc("key_topics", item), exact)[0] == [_sid(0), _sid(1)]
    ambiguous = _snapshot(
        (0, "A", "Friends of the Fernbunk asked to speak."),
        (3, "A", "Friends of the Fernbonk is next."),
        (6, "B", "We oppose the parking plan."),
    )
    assert _added(_doc("key_topics", item), ambiguous)[0] == []


def test_attributed_subject_and_role_antecedents():
    snapshot = _snapshot(
        (0, "A", "Councilmember Ruiz, you have the floor."),
        (3, "B", "Thank you. My name is Tomas."),
        (6, "B", "Please add more bike racks."),
    )
    request = {"text": "Tomas requested more bike racks", "source_ids": [_sid(2)]}
    assert _added(_doc("confirmation_items", request), snapshot, "confirmation_items")[0] == [
        _sid(1)
    ]
    proposal = {"text": "Councilmember proposal: more bike racks", "source_ids": [_sid(2)]}
    assert _added(_doc("confirmation_items", proposal), snapshot, "confirmation_items")[0] == [
        _sid(0),
        _sid(1),
    ]


def test_pronoun_fragment_without_any_anchor_is_left_alone():
    snapshot = _snapshot((0, "A", "Dr. Amari joined."), (3, "A", "She chairs the panel."))
    item = {"text": "She chairs the panel", "source_ids": [_sid(1)]}
    added, counts, _ = _added(_doc("key_topics", item), snapshot)
    assert added == [] and counts == {"items": 0, "added_ids": 0}


def test_unrelated_prior_topic_outside_the_window_is_not_linked():
    far = _snapshot(
        (0, "A", "Mr. Okafor presented the zoning map."),
        (nc.MAX_BACK_SECONDS + 10, "B", "The library hours will extend."),
    )
    item = {"text": "Mr. Okafor: library hours extend", "source_ids": [_sid(1)]}
    assert _added(_doc("key_topics", item), far)[0] == []
    many = _snapshot(
        (0, "A", "Mr. Okafor presented the zoning map."),
        *[(1 + i, "A", f"Filler {i}.") for i in range(nc.MAX_BACK_SEGMENTS)],
        (30, "B", "The library hours will extend."),
    )
    item["source_ids"] = [_sid(nc.MAX_BACK_SEGMENTS + 1)]
    assert _added(_doc("key_topics", item), many)[0] == []


def test_anchor_already_cited_or_missing_adds_nothing():
    snapshot = _snapshot(
        (0, "A", "Earlier Mr. Lindqvist spoke."),
        (3, "A", "Mr. Lindqvist wants a crosswalk."),
    )
    item = {"text": "Mr. Lindqvist: crosswalk", "source_ids": [_sid(1)]}
    assert _added(_doc("key_topics", item), snapshot)[0] == []
    item = {"text": "Mr. Halvorsen: crosswalk", "source_ids": [_sid(1)]}
    assert _added(_doc("key_topics", item), snapshot)[0] == []


def test_over_wide_chain_and_reference_limit_stay_unresolved():
    rows = [(0, "A", "Ms. Brandt has the floor.")]
    rows += [(1 + i, "A", f"Filler {i}.") for i in range(nc.MAX_ADDED_PER_ITEM)]
    rows += [(40, "A", "Fund the shelter.")]
    snapshot = _snapshot(*rows)
    claim = len(rows) - 1
    item = {"text": "Ms. Brandt: fund shelter", "source_ids": [_sid(claim)]}
    assert _added(_doc("key_topics", item), snapshot)[0] == []

    near = _snapshot((0, "A", "Ms. Brandt has the floor."), (2, "A", "Fund the shelter."))
    document = _doc("key_topics", {"text": "Ms. Brandt: fund shelter", "source_ids": [_sid(1)]})
    linked, counts = link_antecedent_context(document, near, max_refs_per_item=1)
    assert linked == document and counts["items"] == 0


def test_only_source_ids_change_and_the_input_is_not_mutated():
    snapshot = _snapshot(
        (0, "A", "The chair calls resolution 55012."),
        (3, "A", "Adopted. The chair will sign it."),
    )
    action = {
        "text": "Sign Resolution 55012",
        "owner": "Chair",
        "deadline_text": "today",
        "deadline_date": "2026-01-05",
        "source_ids": [_sid(1)],
    }
    document = _doc("action_items", action)
    document["overview"] = [{"text": "Resolution 55012 adopted", "source_ids": [_sid(1)]}]
    frozen = copy.deepcopy(document)
    linked, counts = link_antecedent_context(document, snapshot, max_refs_per_item=200)
    assert document == frozen
    assert linked["overview"] == frozen["overview"]  # summaries get no context links
    expected = copy.deepcopy(frozen)
    expected["action_items"][0]["source_ids"] = [_sid(0), _sid(1)]
    assert json.dumps(linked, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert counts == {"items": 1, "added_ids": 1}


def test_context_does_not_bridge_across_a_new_agenda_item():
    snapshot = _snapshot(
        (0, "A", "Ms. Brandt will serve on the housing board."),
        (5, "A", "Agenda item eight. Appointment of Theo."),
        (10, "B", "This appointment supports the apprenticeship program."),
    )
    item = {"text": "Ms. Brandt: housing board contribution", "source_ids": [_sid(2)]}
    assert _added(_doc("key_topics", item), snapshot)[0] == []
    # The agenda header itself remains a valid antecedent for its own item.
    snapshot["segments"][1]["text"] = "Agenda item eight. Ms. Brandt's appointment."
    assert _added(_doc("key_topics", item), snapshot)[0] == [_sid(1)]


def test_two_near_organization_spellings_in_one_segment_are_ambiguous():
    snapshot = _snapshot(
        (0, "A", "Friends of the Lanturnes and Friends of the Lantarnes are present."),
        (4, "B", "We support the park."),
    )
    item = {"text": "Friends of the Lanternes view: support park", "source_ids": [_sid(1)]}
    assert _added(_doc("key_topics", item), snapshot)[0] == []
