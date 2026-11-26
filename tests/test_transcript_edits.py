from __future__ import annotations

import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from test_storage import _create_legacy_database
from test_transcription_api import _authorize
from test_transcription_tasks import _publish_generation, _segment, _task_store

from asr_studio.app import create_app
from asr_studio.auth import AuthStore
from asr_studio.notes import _source_snapshot, get_notes_source
from asr_studio.storage import initialize
from asr_studio.tasks import (
    TaskError,
    reassign_current_generation_speaker,
    transcription_state,
    update_current_generation_segment_text,
)
from asr_studio.transcript_edits import (
    merge_transcript_speakers,
    preview_speaker_merge,
    read_transcript_edit_history,
    set_transcript_segment_checked,
    undo_transcript_speaker_merge,
)

_A, _B = "SPEAKER_00", "SPEAKER_01"
_FIRST, _SECOND, _THIRD = (character * 32 for character in "abc")


@pytest.fixture
def transcript(tmp_path):
    data_dir, model_dir, (meeting_id,) = _task_store(tmp_path)
    generation = _publish_generation(
        data_dir,
        model_dir,
        meeting_id,
        "batch-merge-fixture",
        "fixture-worker",
        [
            _segment(segment_id, speaker_id=speaker, original_speaker_id=speaker)
            for segment_id, speaker in zip((_FIRST, _SECOND, _THIRD), (_A, _A, _B), strict=True)
        ],
        [
            {
                "speaker_id": speaker,
                "display_name": f"Speaker {index}",
                "first_seen_seconds": 1.25,
                "last_seen_seconds": 2.5,
            }
            for index, speaker in enumerate((_A, _B), 1)
        ],
    )
    return data_dir, meeting_id, generation["id"]


def _merge(target, *, expected_count=2):
    return merge_transcript_speakers(
        *target,
        source=_A,
        target=_B,
        expected_source_version=1,
        expected_count=expected_count,
        request_id="merge-fixture",
    )


def _undo(target, *, merge_edit_version=2, source_version=3):
    return undo_transcript_speaker_merge(
        *target,
        merge_edit_version,
        expected_source_version=source_version,
        request_id="undo-fixture",
    )


def _generation(target):
    data_dir, meeting_id, _generation_id = target
    return transcription_state(data_dir, meeting_id)["generation"]


def test_merge_and_undo_preserve_unrelated_text_and_each_revision(transcript):
    before = preview_speaker_merge(*transcript, source=_A, target=_B)
    assert before["segment_count"] == 2 and before["source_version"] == 1
    with pytest.raises(TaskError) as count_changed:
        _merge(transcript, expected_count=1)
    assert count_changed.value.code == "speaker_merge_changed"

    merged = _merge(transcript)
    assert merged["source_version"] == merged["edit_version"] == 2
    assert _merge(transcript) == merged
    with pytest.raises(TaskError) as different_request:
        _merge(transcript, expected_count=1)
    assert different_request.value.code == "request_conflict"

    data_dir, meeting_id, generation_id = transcript
    changed = update_current_generation_segment_text(
        data_dir,
        meeting_id,
        _THIRD,
        expected_generation_id=generation_id,
        expected_source_version=2,
        text="Keep the unrelated change",
    )
    assert changed["source_version"] == 3
    restored = _undo(transcript)
    assert restored["source_version"] == restored["edit_version"] == 4
    assert _undo(transcript) == restored

    result = _generation(transcript)
    assert [segment["speaker_id"] for segment in result["segments"]] == [_A, _A, _B]
    assert [segment["original_speaker_id"] for segment in result["segments"]] == [_A, _A, _B]
    assert [segment["assignment_version"] for segment in result["segments"]] == [3, 3, 1]
    assert result["segments"][2]["text"] == "Keep the unrelated change"
    assert len(result["speakers"]) == 2

    versions = read_transcript_edit_history(*transcript)["revisions"]
    assert versions[0]["undo_of_version"] == merged["edit_version"]
    assert [entry["kind"] for entry in versions] == [
        "speaker_merge_undone",
        "segment_text",
        "speakers_merged",
        "generated",
    ]
    initial = read_transcript_edit_history(*transcript, edit_version=1)
    assert [segment["speaker_id"] for segment in initial["segments"]] == [_A, _A, _B]
    historical = read_transcript_edit_history(*transcript, edit_version=2)
    assert [segment["speaker_id"] for segment in historical["segments"]] == [_B, _B, _B]
    assert historical["segments"][2]["text"] != "Keep the unrelated change"


def test_undo_conflicts_without_applying_any_of_the_batch_if_a_touched_assignment_changed(
    transcript,
):
    _merge(transcript)
    data_dir, meeting_id, generation_id = transcript

    def assign(speaker, source, assignment):
        return reassign_current_generation_speaker(
            data_dir,
            meeting_id,
            _SECOND,
            expected_generation_id=generation_id,
            expected_source_version=source,
            expected_assignment_version=assignment,
            speaker_id=speaker,
        )

    assign(_A, 2, 2)
    assign(_B, 3, 3)
    before = _generation(transcript)
    with pytest.raises(TaskError) as conflict:
        _undo(transcript, source_version=4)
    assert conflict.value.code == "assignment_version_changed"
    assert _generation(transcript) == before


def test_checked_state_stays_with_generation_and_keeps_detector_flags(transcript):
    baseline = _generation(transcript)
    marked = set_transcript_segment_checked(
        *transcript, _FIRST, checked=True, expected_review_version=0
    )
    assert marked["checked"] is True
    assert marked["review_version"] == 1
    assert _generation(transcript) == baseline
    with pytest.raises(TaskError) as conflict:
        set_transcript_segment_checked(
            *transcript, _FIRST, checked=False, expected_review_version=0
        )
    assert conflict.value.code == "segment_review_changed"
    _merge(transcript)
    history = read_transcript_edit_history(*transcript)
    assert history["reviews"][_FIRST]["checked"] is True
    initial = read_transcript_edit_history(*transcript, edit_version=1)
    assert initial["segments"][0]["checked"] is False
    latest = read_transcript_edit_history(*transcript, edit_version=3)
    assert latest["segments"][0]["checked"] is True
    assert latest["segments"][0]["review_reasons"] == baseline["segments"][0]["review_reasons"]


@pytest.fixture
async def legacy_review_client(settings, request):
    settings.data_dir.mkdir(mode=0o700)
    db = settings.data_dir / "app.sqlite3"
    _create_legacy_database(db, version=4)
    ids = getattr(request, "param", ())
    if ids:
        with sqlite3.connect(db) as connection:
            connection.executemany(
                "INSERT INTO transcript_segments(id, generation_id, sequence, start_seconds, "
                "end_seconds, text, original_speaker_id, speaker_id, uncertain) "
                "SELECT ?, generation_id, ?, start_seconds, end_seconds, text, "
                "original_speaker_id, speaker_id, uncertain FROM transcript_segments "
                "WHERE id = 'old-segment'",
                [(segment_id, sequence) for sequence, segment_id in enumerate(ids, 1)],
            )
            connection.execute(
                "UPDATE transcript_generations SET segment_count = ?",
                (len(ids) + 1,),
            )
    initialize(settings.data_dir)
    AuthStore(settings.auth_dir).ensure()
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings)), base_url=settings.public_url
    ) as client:
        yield client


@pytest.mark.anyio
async def test_manual_review_of_upgraded_v4_segment(legacy_review_client, settings):
    client = legacy_review_client
    headers = await _authorize(client, settings)
    meeting = "/api/meetings/old-meeting"
    root = f"{meeting}/transcript-generations/old-generation"
    history_url = f"{root}/edit-history"
    review_url = f"{root}/segments/review"
    payload = {"segment_id": "old-segment", "checked": True, "expected_review_version": 0}

    before = await client.get(history_url)
    assert before.status_code == 200
    assert before.json() == {"generation_id": "old-generation", "revisions": [], "reviews": {}}

    mark = await client.patch(review_url, headers=headers, json=payload)
    assert mark.status_code == 200, mark.text
    assert mark.json() == {
        "generation_id": "old-generation",
        "segment_id": "old-segment",
        "checked": True,
        "review_version": 1,
    }

    missing = await client.patch(
        review_url, headers=headers, json={**payload, "segment_id": "missing-segment"}
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "segment_not_found"
    for url, status, code in (
        (review_url.replace("old-meeting", "wrong-meeting"), 404, "generation_not_found"),
        (review_url.replace("old-generation", "wrong-generation"), 404, "generation_not_found"),
        (review_url, 409, "segment_review_changed"),
    ):
        rejected = await client.patch(url, headers=headers, json=payload)
        assert rejected.status_code == status
        assert rejected.json()["detail"]["code"] == code

    history = await client.get(history_url)
    assert history.status_code == 200
    assert history.json()["reviews"] == {"old-segment": {"checked": True, "review_version": 1}}
    assert [
        (revision["edit_version"], revision["source_version"], revision["kind"])
        for revision in history.json()["revisions"]
    ] == [(2, 1, "segment_review"), (1, 1, "legacy")]

    for edit_version, checked, review_version in ((1, False, 0), (2, True, 1)):
        saved = await client.get(history_url, params={"edit_version": edit_version})
        assert saved.status_code == 200
        revision = saved.json()
        assert revision["source_version"] == 1
        (segment,) = revision["segments"]
        assert segment["id"] == "old-segment"
        assert segment["text"] == "Kept text."
        assert segment["original_speaker_id"] == segment["speaker_id"] == "legacy-speaker"
        assert segment["checked"] is checked
        assert segment["review_version"] == review_version


_OPAQUE_IDS = ("old/segment", "old%2Fsegment", ".", "..")


@pytest.mark.anyio
@pytest.mark.parametrize("legacy_review_client", [_OPAQUE_IDS], indirect=True)
async def test_manual_review_keeps_migrated_segment_ids_opaque(legacy_review_client, settings):
    client = legacy_review_client
    data = settings.data_dir
    db = data / "app.sqlite3"
    snapshot_id = "7" * 32
    ids = ("old-segment", *_OPAQUE_IDS)
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        meeting = connection.execute("SELECT * FROM meetings").fetchone()
        generation = connection.execute("SELECT * FROM transcript_generations").fetchone()
        snapshot = _source_snapshot(
            connection, meeting=meeting, generation=generation, snapshot_id=snapshot_id
        )
        raw = json.dumps(snapshot)
        connection.execute(
            "INSERT INTO notes_sources(id, meeting_id, generation_id, source_version, "
            "notes_source_version, meeting_date, snapshot_json) VALUES (?, ?, ?, 1, 1, ?, ?)",
            (snapshot_id, "old-meeting", "old-generation", meeting["meeting_date"], raw),
        )
    assert [row["id"] for row in snapshot["segments"]] == list(ids)
    root = "/api/meetings/old-meeting/transcript-generations/old-generation"
    url = f"{root}/segments/review"
    payload = {"segment_id": "old/segment", "checked": True, "expected_review_version": 0}
    origin = {"Origin": settings.public_url}
    assert (await client.patch(url, headers=origin, json=payload)).status_code == 401
    headers = await _authorize(client, settings)
    assert (await client.patch(url, json=payload)).status_code == 403

    for index, segment_id in enumerate(_OPAQUE_IDS, 1):
        mark = await client.patch(url, headers=headers, json={**payload, "segment_id": segment_id})
        assert mark.status_code == 200, mark.text
        assert mark.json() == {
            "generation_id": "old-generation",
            "segment_id": segment_id,
            "checked": True,
            "review_version": 1,
        }
        saved = await client.get(f"{root}/edit-history", params={"edit_version": index + 1})
        assert saved.status_code == 200
        assert saved.json()["source_version"] == 1
        assert [(row["id"], row["checked"]) for row in saved.json()["segments"]] == [
            (stored_id, 0 < position <= index) for position, stored_id in enumerate(ids)
        ]

    for malformed in (
        {"checked": True, "expected_review_version": 0},
        {**payload, "extra": True},
        {**payload, "segment_id": 7},
        {**payload, "checked": 1},
        {**payload, "expected_review_version": False},
    ):
        assert (await client.patch(url, headers=headers, json=malformed)).status_code == 400

    baseline = await client.get(f"{root}/edit-history", params={"edit_version": 1})
    assert [(row["id"], row["checked"]) for row in baseline.json()["segments"]] == [
        (segment_id, False) for segment_id in ids
    ]
    assert get_notes_source(data, snapshot_id) == snapshot
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT snapshot_json FROM notes_sources").fetchone() == (raw,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("segment_id", ["", None, 7, b"old-segment"])
def test_manual_review_rejects_malformed_segment_id(tmp_path, segment_id):
    with pytest.raises(TaskError) as invalid:
        set_transcript_segment_checked(
            tmp_path,
            "old-meeting",
            "old-generation",
            segment_id,
            checked=True,
            expected_review_version=0,
        )
    assert (invalid.value.status_code, invalid.value.code) == (400, "invalid_segment_review")


@pytest.mark.anyio
async def test_history_review_and_speaker_merge_use_the_authenticated_generation_routes(
    client, settings, transcript
):
    _data, meeting_id, generation_id = transcript
    root = f"/api/meetings/{meeting_id}/transcript-generations/{generation_id}"
    assert (await client.get(f"{root}/edit-history")).status_code == 401
    headers = await _authorize(client, settings)
    assert (await client.get(f"{root}/edit-history")).json()["revisions"][0]["kind"] == "generated"
    assert (await client.get(f"{root}/edit-history?edit_version=2")).status_code == 404

    selected = {"source_speaker_id": _A, "target_speaker_id": _B}
    preview = await client.get(f"{root}/speaker-merge", params=selected)
    assert preview.status_code == 200
    assert preview.json()["segment_count"] == 2
    reply = await client.post(
        f"{root}/speaker-merge",
        headers=headers,
        json={
            **selected,
            "expected_source_version": preview.json()["source_version"],
            "expected_segment_count": preview.json()["segment_count"],
            "request_id": "route-confirmation",
        },
    )
    assert reply.status_code == 200
    merge = reply.json()

    checked = await client.patch(
        f"{root}/segments/review",
        headers=headers,
        json={"segment_id": _FIRST, "checked": True, "expected_review_version": 0},
    )
    assert checked.status_code == 200 and checked.json()["checked"] is True
    saved = (await client.get(f"{root}/edit-history?edit_version={merge['edit_version']}")).json()
    assert [segment["speaker_id"] for segment in saved["segments"]] == [_B, _B, _B]
    assert saved["segments"][0]["checked"] is False

    undone = await client.post(
        f"{root}/speaker-merge/{merge['edit_version']}/undo",
        headers=headers,
        json={
            "expected_source_version": merge["source_version"],
            "request_id": "route-undo-fixture",
        },
    )
    assert undone.status_code == 200
    assert undone.json()["segment_count"] == 2
    state = (await client.get(f"{root}/edit-history")).json()
    assert state["reviews"][_FIRST]["checked"] is True
    assert state["revisions"][0]["kind"] == "speaker_merge_undone"
