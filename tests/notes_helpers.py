from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from seeding import seed_meeting

from asr_studio.notes import accept_notes_task, get_notes_source, publish_notes_revision
from asr_studio.notes_document import build_citations, render_markdown
from asr_studio.storage import database_path, initialize
from asr_studio.tasks import claim_next_task


class StubNotesProviderStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()

    @contextmanager
    def operation(self):
        with self.lock:
            yield


class StubNotesProvider:
    def __init__(self) -> None:
        self.store = StubNotesProviderStore()

    def assert_notes_acceptable(self) -> None:
        return None

    def notes_task_pin(self) -> dict[str, object]:
        return {
            "provider": "chatgpt",
            "connection_version": 7,
            "model": "gpt-6.1-sol",
            "reasoning_effort": "high",
        }


def seed_completed_transcript(
    data_dir: Path,
    *,
    title: str = "Planning meeting",
    meeting_date: str | None = "2026-10-19",
) -> tuple[str, str, list[str]]:
    initialize(data_dir)
    meeting_id = seed_meeting(data_dir, title, meeting_date, duration_seconds=30.0)
    task_id = uuid.uuid4().hex
    generation_id = uuid.uuid4().hex
    segment_ids = [uuid.uuid4().hex, uuid.uuid4().hex]
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
            "model_name, device, compute_type, language_hint, status, stage, result_scope, "
            "audio_duration_seconds, transcript_end_seconds, segment_count) "
            "VALUES (?, ?, ?, ?, 'transcription', '/models/whisper-small', 'whisper-small', "
            "'cpu', 'int8', 'en', 'succeeded', 'complete', 'transcript_only', 30.0, 5.0, 2)",
            (task_id, f"seed-{task_id}", f"hash-{task_id}", meeting_id),
        )
        connection.execute(
            "INSERT INTO transcript_generations(id, meeting_id, task_id, generation_number, "
            "status, language_hint, model_name, language_probability, uncertainty_policy, "
            "segment_count, diarization_model_name, regular_turn_count, exclusive_turn_count, "
            "alignment_policy) VALUES (?, ?, ?, 1, 'complete', 'en', 'whisper-small', 0.9, "
            "'test-policy', 2, 'community-1', 1, 0, 'test-alignment')",
            (generation_id, meeting_id, task_id),
        )
        connection.execute(
            "INSERT INTO generation_speakers(generation_id, speaker_id, display_name, "
            "first_seen_seconds, last_seen_seconds) VALUES (?, 'SPEAKER_00', 'Morgan', 1.0, 5.0)",
            (generation_id,),
        )
        connection.executemany(
            "INSERT INTO transcript_segments(id, generation_id, sequence, start_seconds, "
            "end_seconds, text, original_speaker_id, speaker_id, avg_logprob, no_speech_prob, "
            "compression_ratio, uncertain, review_reasons, overlap, speaker_dominance, "
            "assignment_version) VALUES (?, ?, ?, ?, ?, ?, 'SPEAKER_00', 'SPEAKER_00', "
            "-0.2, 0.05, 1.0, 0, '[]', 0, 1.0, 1)",
            [
                (segment_ids[0], generation_id, 0, 1.0, 2.0, "We need a launch plan."),
                (segment_ids[1], generation_id, 1, 4.0, 5.0, "Morgan will send a draft."),
            ],
        )
        connection.execute(
            "UPDATE meetings SET current_generation_id = ? WHERE id = ?",
            (generation_id, meeting_id),
        )
    return meeting_id, generation_id, segment_ids


def rerun_transcript(data_dir: Path, meeting_id: str) -> None:
    """Make a second transcript generation the current one, as running the recording again does."""
    newer, task_id = uuid.uuid4().hex, uuid.uuid4().hex
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO tasks(id, request_id, request_hash, meeting_id, kind, model_path, "
            "model_name, device, compute_type, language_hint, status, stage, result_scope, "
            "audio_duration_seconds, transcript_end_seconds, segment_count) VALUES (?, ?, ?, ?, "
            "'transcription', '/models/whisper-small', 'whisper-small', 'cpu', 'int8', 'en', "
            "'succeeded', 'complete', 'transcript_only', 30.0, 5.0, 2)",
            (task_id, f"newer-{task_id}", f"hash-{task_id}", meeting_id),
        )
        connection.execute(
            "INSERT INTO transcript_generations(id, meeting_id, task_id, generation_number, "
            "status, language_hint, model_name, language_probability, uncertainty_policy, "
            "segment_count, diarization_model_name, regular_turn_count, exclusive_turn_count, "
            "alignment_policy) VALUES (?, ?, ?, 2, 'complete', 'en', 'whisper-small', 0.9, "
            "'test-policy', 2, 'community-1', 1, 0, 'test-alignment')",
            (newer, meeting_id, task_id),
        )
        connection.execute(
            "INSERT INTO generation_speakers(generation_id, speaker_id, display_name, "
            "first_seen_seconds, last_seen_seconds) VALUES (?, 'SPEAKER_00', 'Morgan', 1.0, 5.0)",
            (newer,),
        )
        connection.executemany(
            "INSERT INTO transcript_segments(id, generation_id, sequence, start_seconds, "
            "end_seconds, text, original_speaker_id, speaker_id, avg_logprob, no_speech_prob, "
            "compression_ratio, uncertain, review_reasons, overlap, speaker_dominance, "
            "assignment_version) VALUES (?, ?, ?, ?, ?, ?, 'SPEAKER_00', 'SPEAKER_00', "
            "-0.2, 0.05, 1.0, 0, '[]', 0, 1.0, 1)",
            [
                (uuid.uuid4().hex, newer, 0, 1.0, 2.0, "A fresh pass heard the opening."),
                (uuid.uuid4().hex, newer, 1, 4.0, 5.0, "A fresh pass heard the follow-up."),
            ],
        )
        connection.execute(
            "UPDATE meetings SET current_generation_id = ? WHERE id = ?", (newer, meeting_id)
        )


def default_cited_document(segment_ids: list[str]) -> dict[str, object]:
    """Six sections of bullets that cite the first two transcript segments."""
    first, second = segment_ids[0], segment_ids[1]
    return {
        "schema_version": 1,
        "overview": [
            {"text": "The team reviewed its launch plan.", "source_ids": [first]},
            {"text": "A second point cites both parts.", "source_ids": [first, second]},
        ],
        "key_topics": [],
        "decisions": [{"text": "Prepare the launch plan.", "source_ids": [first]}],
        "open_questions": [{"text": "Who will review the draft?", "source_ids": [second]}],
        "action_items": [
            {
                "text": "Send the draft.",
                "source_ids": [second],
                "owner": "Morgan",
                "deadline_text": "tomorrow",
                "deadline_date": None,
            }
        ],
        "confirmation_items": [{"text": "Confirm the review date.", "source_ids": [second]}],
    }


def seed_cited_notes(
    data_dir: Path,
    build_document=default_cited_document,
    *,
    title: str = "Cited notes",
    output_language: str = "en",
    segment_starts: list[float] | None = None,
    with_citations: bool = True,
    existing: tuple[str, str, list[str]] | None = None,
    source_version: int = 1,
):
    """A meeting with one saved notes revision, published as the worker does.

    The Markdown comes from `render_markdown` and the citations from `build_citations`, both over
    the frozen source snapshot, so they agree as they do for a real run. `segment_starts` replaces
    the transcript with one-second segments starting there; `with_citations=False` publishes the
    revision without any, as notes made before citations were kept read. `existing` adds another
    revision to a meeting seeded before, whose transcript is then at `source_version`.
    """
    meeting_id, generation_id, segment_ids = existing or seed_completed_transcript(
        data_dir, title=title
    )
    if segment_starts is not None:
        segment_ids = [uuid.uuid4().hex for _ in segment_starts]
        with sqlite3.connect(database_path(data_dir)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "DELETE FROM transcript_segments WHERE generation_id = ?", (generation_id,)
            )
            connection.executemany(
                "INSERT INTO transcript_segments(id, generation_id, sequence, start_seconds, "
                "end_seconds, text, original_speaker_id, speaker_id, avg_logprob, no_speech_prob, "
                "compression_ratio, uncertain, review_reasons, overlap, speaker_dominance, "
                "assignment_version) VALUES (?, ?, ?, ?, ?, ?, 'SPEAKER_00', 'SPEAKER_00', "
                "-0.2, 0.05, 1.0, 0, '[]', 0, 1.0, 1)",
                [
                    (identifier, generation_id, index, start, start + 1.0, f"Segment {index}.")
                    for index, (identifier, start) in enumerate(
                        zip(segment_ids, segment_starts, strict=True)
                    )
                ],
            )
            connection.execute(
                "UPDATE transcript_generations SET segment_count = ? WHERE id = ?",
                (len(segment_ids), generation_id),
            )
    request_id = f"cited-{uuid.uuid4().hex}"
    accepted = accept_notes_task(
        data_dir,
        provider=StubNotesProvider(),
        meeting_id=meeting_id,
        request_id=request_id,
        expected_generation_id=generation_id,
        expected_source_version=source_version,
        expected_metadata_version=1,
        worker_ready=True,
        output_language=output_language,
    )
    owner_id = f"cited-worker-{uuid.uuid4().hex}"
    claimed = claim_next_task(data_dir, owner_id=owner_id)
    assert claimed is not None and claimed["id"] == accepted["id"]
    with sqlite3.connect(database_path(data_dir)) as connection:
        source_id = connection.execute(
            "SELECT notes_source_id FROM tasks WHERE id = ?", (accepted["id"],)
        ).fetchone()[0]
    snapshot = get_notes_source(data_dir, source_id)
    document = build_document(segment_ids)
    revision = publish_notes_revision(
        data_dir,
        accepted["id"],
        owner_id=owner_id,
        markdown=render_markdown(document, snapshot, output_language=output_language),
        elapsed_seconds=0.1,
        citations=(
            build_citations(document, snapshot, output_language=output_language)
            if with_citations
            else None
        ),
    )
    assert revision is not None
    return meeting_id, revision, snapshot, segment_ids


def reconcile_payload(prompt: str) -> dict:
    """The ledger JSON of one reconcile prompt; a repair suffix may follow it."""
    return json.JSONDecoder().raw_decode(prompt.split("RECONCILE_INPUT_JSON:\n", 1)[1])[0]


def reconcile_disposition(ref: str, action: str = "keep", **fields: object) -> dict:
    return {
        "ref": ref,
        "action": action,
        "reason": "none",
        "target": None,
        "text": None,
        "source_ids": [],
        "evidence": [],
        "owner": None,
        "deadline_text": None,
        **fields,
    }


def reconcile_keep_patch(prompt: str) -> str:
    """Keep every final item; a map item is a duplicate of an allowed final target, else kept."""
    payload = reconcile_payload(prompt)
    finals = [item["ref"] for item in payload["final_items"]]
    dispositions = [reconcile_disposition(ref) for ref in finals]
    for item in payload["map_items"]:
        targets = [ref for ref in item["allowed_merge_targets"] if ref in finals]
        dispositions.append(
            reconcile_disposition(item["ref"], "remove", reason="duplicate", target=targets[0])
            if targets
            else reconcile_disposition(item["ref"])
        )
    patch = {"ledger_version": 2, "dispositions": dispositions, "additions": []}
    cued = [
        item["ref"]
        for item in payload["final_items"] + payload["map_items"]
        if "unstated_clauses" in item.get("attention", {})
    ]
    if cued:  # a clause-cue ledger: decline every cue (decision coverage, not truth)
        patch["cue_checked"] = dict.fromkeys(cued, False)
    return json.dumps(patch)


def semantic_patch(refs, changes=(), unresolved=()) -> str:
    """An audit-v2 patch deciding every owned ref: false exactly for those changed or unresolved."""
    changes, unresolved = list(changes), list(unresolved)
    listed = {c.get("ref") for c in changes if isinstance(c, dict)} | set(unresolved)
    return json.dumps(
        {
            "audit_version": 2,
            "verified": {ref: ref not in listed for ref in refs},
            "changes": changes,
            "unresolved_refs": unresolved,
        }
    )


def semantic_refs(prompt_or_audit) -> list[str]:
    """The refs one audit prompt (its payload) or audit view owns, in order."""
    if isinstance(prompt_or_audit, str):
        return [item["ref"] for item in semantic_payload(prompt_or_audit)["items"]]
    return [item.ref for item in prompt_or_audit.items]


def semantic_keep_patch(prompt_or_audit) -> str:
    """Verify every owned ref unchanged."""
    return semantic_patch(semantic_refs(prompt_or_audit))


def semantic_payload(prompt: str) -> dict:
    """The audit JSON of one semantic-audit prompt; a repair suffix may follow it."""
    return json.JSONDecoder().raw_decode(prompt.split("AUDIT_INPUT_JSON:\n", 1)[1])[0]
