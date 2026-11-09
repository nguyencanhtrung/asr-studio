"""A '.c' candidate is offered, never forced: only a valid model change selects it."""

from __future__ import annotations

import pytest
from notes_helpers import semantic_patch

from asr_studio import notes_document as nd
from asr_studio.notes_semantic import SemanticAudit

HEADER = "Agenda item 3 Council Bill 204611 relating to the harbor fund."
VOTE = (
    HEADER,
    "Please call the roll on the passage of the bill.",
    "Avery, Brook, Cole, Dane, Ellis, Ford.",
    "Six in favor, not opposed.",
    "The bill passed and the chair will sign it.",
)
ITEM = "Council Bill 204611 for harbor repairs passed 6-0"
CHANGE = {
    "ref": "i0",
    "text_ref": "i0.c",
    "add_source_ids": [],
    "clear_owner": False,
    "clear_deadline": False,
}


def _audit(text: str = ITEM, texts=VOTE, cited=(3, 4)):
    segments = [
        {
            "id": f"{i + 1:032x}",
            "start_seconds": i * 4.0,
            "end_seconds": i * 4.0 + 1,
            "text": t,
            "speaker_name": "SPEAKER_00",
        }
        for i, t in enumerate(texts)
    ]
    source = {"meeting_date": "2026-03-04", "speakers": [], "segments": segments}
    ids = [s["id"] for s in segments]
    document = {"schema_version": 1, **{name: [] for name, _ in nd.SECTIONS}}
    document["decisions"] = [{"text": text, "source_ids": [ids[i] for i in cited]}]
    return ids, SemanticAudit(source, nd.validate_document(document, source))


def test_unresolved_with_an_available_candidate_publishes_no_decision():
    ids, audit = _audit()
    assert "i0.c" in {t["text_ref"] for t in audit.prompt_payload()["items"][0]["text_refs"]}
    published, counts = audit.parse(semantic_patch(["i0"], unresolved=["i0"]))
    assert published["decisions"] == []
    assert [e["source_ids"] for e in published["confirmation_items"]] == [[ids[3], ids[4]]]
    assert (counts["core_preserved"], counts["quarantined"], counts["model_verified"]) == (0, 1, 0)


@pytest.mark.parametrize(
    "item, texts",
    [
        (  # being held
            "Council Bill 204611 passed 6-0",
            (HEADER, "Six in favor, not opposed.", "The bill passed, though it is being held."),
        ),
        (  # not voted
            "Council Bill 204611 passed 6-0",
            (HEADER, "Six in favor, not opposed.", "The bill passed. We have not voted on it."),
        ),
        (  # another body
            "Council Bill 204611 passed 6-0",
            (HEADER, "Six in favor, not opposed.", "The bill passed the planning commission."),
        ),
        (  # uncertainty
            "Council Bill 204611 passed 6-0",
            (HEADER, "Six in favor, not opposed.", "I think the bill passed, I am not sure."),
        ),
    ],
)
def test_model_unresolved_is_never_overridden_by_the_recognizer(item, texts):
    _ids, audit = _audit(item, texts, cited=tuple(range(1, len(texts))))
    published, counts = audit.parse(semantic_patch(["i0"], unresolved=["i0"]))
    assert published["decisions"] == []
    assert (counts["core_preserved"], counts["quarantined"]) == (0, 1)


def test_explicit_model_choice_publishes_the_candidate_with_its_ids():
    ids, audit = _audit()
    published, counts = audit.parse(semantic_patch(["i0"], [CHANGE]))
    assert published["decisions"] == [
        {"text": "Council Bill 204611 passed 6-0", "source_ids": [ids[3], ids[4], ids[0]]}
    ]
    assert (counts["core_selected"], counts["core_preserved"], counts["model_verified"]) == (
        1,
        0,
        0,
    )


def test_keeping_the_original_publishes_no_implicit_candidate():
    _ids, audit = _audit()
    published, counts = audit.parse(semantic_patch(["i0"]))
    assert published["decisions"][0]["text"] == ITEM
    assert (counts["core_selected"], counts["core_preserved"], counts["model_verified"]) == (
        0,
        0,
        1,
    )


@pytest.mark.parametrize("bad", ["both", "nonbool"])
def test_contradictory_or_nonboolean_decisions_are_rejected(bad):
    _ids, audit = _audit()
    patch = semantic_patch(["i0"], [CHANGE], unresolved=["i0"] if bad == "both" else ())
    if bad == "nonbool":
        patch = patch.replace('"i0": false', '"i0": "false"')
    with pytest.raises(Exception):  # noqa: B017 - any invalid-output error
        audit.parse(patch)


def test_proposed_candidate_ids_are_shown_and_equal_the_appended_ids():
    ids, audit = _audit()
    entries = [t for i in audit.prompt_payload()["items"] for t in i["text_refs"]]
    for entry in entries:  # only '.c' options carry source_ids
        assert ("source_ids" in entry) == entry["text_ref"].endswith(".c")
    proposed = next(e["source_ids"] for e in entries if e["text_ref"] == "i0.c")
    visible = {s["id"] for s in audit.visible_snapshot()["segments"]}
    assert proposed and set(proposed) <= visible and set(proposed) <= set(ids)
    published, _counts = audit.parse(semantic_patch(["i0"], [CHANGE]))
    original = [ids[3], ids[4]]
    assert published["decisions"][0]["source_ids"] == [
        *original,
        *(i for i in proposed if i not in original),
    ]
