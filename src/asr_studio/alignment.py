"""Review-safe alignment of ASR intervals with Community-1 diarization turns."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from typing import Any

_SPEAKER_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
ASR_UNCERTAINTY_POLICY = "faster-whisper-review-cues-v1"
ALIGNMENT_POLICY = "community-1-exclusive-max-overlap-v1"
LOW_SPEAKER_DOMINANCE_THRESHOLD = 0.6


class AlignmentError(ValueError):
    """Diarization output cannot be aligned safely to transcript segments."""


def validate_turns(turns: Sequence[dict[str, Any]]) -> list[dict[str, object]]:
    """Validate child-process turn records and return normalized source intervals."""
    normalized: list[dict[str, object]] = []
    for turn in turns:
        start = turn.get("start_seconds")
        end = turn.get("end_seconds")
        speaker_id = turn.get("speaker_id")
        if (
            isinstance(start, bool)
            or not isinstance(start, (int, float))
            or not math.isfinite(float(start))
            or start < 0
            or isinstance(end, bool)
            or not isinstance(end, (int, float))
            or not math.isfinite(float(end))
            or end <= start
            or not isinstance(speaker_id, str)
            or not _SPEAKER_ID_RE.fullmatch(speaker_id)
        ):
            raise AlignmentError("The diarization runner returned an invalid speaker turn.")
        normalized.append(
            {
                "start_seconds": float(start),
                "end_seconds": float(end),
                "speaker_id": speaker_id,
            }
        )
    return sorted(
        normalized,
        key=lambda turn: (turn["start_seconds"], turn["end_seconds"], turn["speaker_id"]),
    )


def _overlap(start: float, end: float, turn: dict[str, object]) -> float:
    return max(0.0, min(end, float(turn["end_seconds"])) - max(start, float(turn["start_seconds"])))


class _TurnCursor:
    """Return overlapping sorted turns in one sweep for chronological ASR segments."""

    def __init__(self, turns: list[dict[str, object]]) -> None:
        self.turns = turns
        self.index = 0
        self.active: list[dict[str, object]] = []
        self.latest_start = float("-inf")

    def overlaps(self, start: float, end: float) -> list[dict[str, object]]:
        if start < self.latest_start:
            return [turn for turn in self.turns if _overlap(start, end, turn) > 0]
        self.latest_start = start
        self.active = [turn for turn in self.active if float(turn["end_seconds"]) > start]
        while self.index < len(self.turns):
            turn = self.turns[self.index]
            if float(turn["start_seconds"]) >= end:
                break
            self.index += 1
            if float(turn["end_seconds"]) > start:
                self.active.append(turn)
        return [turn for turn in self.active if _overlap(start, end, turn) > 0]


def _asr_uncertain(segment: dict[str, Any]) -> bool:
    def finite_number(field: str) -> float | None:
        value = segment.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        result = float(value)
        return result if math.isfinite(result) else None

    avg_logprob = finite_number("avg_logprob")
    no_speech_prob = finite_number("no_speech_prob")
    compression_ratio = finite_number("compression_ratio")
    return (
        (avg_logprob is not None and avg_logprob < -1.0)
        or (no_speech_prob is not None and no_speech_prob >= 0.6)
        or (compression_ratio is not None and compression_ratio > 2.4)
    )


def _normalized_segment_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(
        character.casefold()
        for character in value
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


def _repeated_segment_indices(segments: Sequence[dict[str, Any]]) -> set[int]:
    normalized = [_normalized_segment_text(segment.get("text")) for segment in segments]
    repeated: set[int] = set()
    run_start = 0
    for index in range(1, len(normalized) + 1):
        same_run = (
            index < len(normalized)
            and normalized[index]
            and normalized[index] == normalized[index - 1]
        )
        if same_run:
            continue
        if normalized[index - 1] and index - run_start >= 3:
            repeated.update(range(run_start, index))
        run_start = index
    return repeated


def align_segments_to_speakers(
    segments: Sequence[dict[str, Any]],
    regular_turns: Sequence[dict[str, Any]],
    exclusive_turns: Sequence[dict[str, Any]],
    *,
    low_dominance_threshold: float = LOW_SPEAKER_DOMINANCE_THRESHOLD,
) -> dict[str, object]:
    """Keep each ASR interval intact and attach the best exclusive speaker label."""
    regular = validate_turns(regular_turns)
    exclusive = validate_turns(exclusive_turns)
    if not math.isfinite(low_dominance_threshold) or not 0 < low_dominance_threshold <= 1:
        raise ValueError("low_dominance_threshold must be between zero and one")

    first_seen: dict[str, float] = {}
    last_seen: dict[str, float] = {}
    for turn in [*regular, *exclusive]:
        speaker_id = str(turn["speaker_id"])
        first_seen[speaker_id] = min(
            first_seen.get(speaker_id, float("inf")), float(turn["start_seconds"])
        )
        last_seen[speaker_id] = max(last_seen.get(speaker_id, 0.0), float(turn["end_seconds"]))
    speaker_ids = sorted(first_seen, key=lambda speaker_id: (first_seen[speaker_id], speaker_id))
    speakers = [
        {
            "speaker_id": speaker_id,
            "display_name": f"Speaker {index + 1}",
            "first_seen_seconds": first_seen[speaker_id],
            "last_seen_seconds": last_seen[speaker_id],
        }
        for index, speaker_id in enumerate(speaker_ids)
    ]

    aligned: list[dict[str, object]] = []
    regular_cursor = _TurnCursor(regular)
    exclusive_cursor = _TurnCursor(exclusive)
    repeated_indices = _repeated_segment_indices(segments)
    for index, segment in enumerate(segments):
        start = segment.get("start_seconds")
        end = segment.get("end_seconds")
        if (
            isinstance(start, bool)
            or not isinstance(start, (int, float))
            or not math.isfinite(float(start))
            or isinstance(end, bool)
            or not isinstance(end, (int, float))
            or not math.isfinite(float(end))
            or end < start
        ):
            raise AlignmentError("An ASR segment has invalid source timestamps.")
        interval_start = float(start)
        interval_end = float(end)
        regular_hits = regular_cursor.overlaps(interval_start, interval_end)
        regular_speakers = {str(turn["speaker_id"]) for turn in regular_hits}
        multiple_speakers = len(regular_speakers) > 1
        has_overlap = any(
            first["speaker_id"] != second["speaker_id"]
            and max(
                interval_start,
                float(first["start_seconds"]),
                float(second["start_seconds"]),
            )
            < min(
                interval_end,
                float(first["end_seconds"]),
                float(second["end_seconds"]),
            )
            for index, first in enumerate(regular_hits)
            for second in regular_hits[index + 1 :]
        )

        speaker_overlaps: dict[str, float] = {}
        for turn in exclusive_cursor.overlaps(interval_start, interval_end):
            duration = _overlap(interval_start, interval_end, turn)
            if duration:
                speaker_id = str(turn["speaker_id"])
                speaker_overlaps[speaker_id] = speaker_overlaps.get(speaker_id, 0.0) + duration
        matched_duration = sum(speaker_overlaps.values())
        original_speaker_id: str | None = None
        speaker_dominance: float | None = None
        if matched_duration > 0:
            original_speaker_id = max(
                speaker_overlaps,
                key=lambda speaker_id: (speaker_overlaps[speaker_id], speaker_id),
            )
            interval_duration = interval_end - interval_start
            if interval_duration > 0:
                speaker_dominance = min(
                    1.0, speaker_overlaps[original_speaker_id] / interval_duration
                )

        review_reasons: list[str] = []
        if _asr_uncertain(segment):
            review_reasons.append("asr_uncertain")
        if index in repeated_indices:
            review_reasons.append("repeated_segment")
        if has_overlap:
            review_reasons.append("overlap")
        if multiple_speakers:
            review_reasons.append("multiple_speakers")
        if original_speaker_id is None:
            review_reasons.append("unidentified_speaker")
        elif speaker_dominance is not None and speaker_dominance < low_dominance_threshold:
            review_reasons.append("low_speaker_dominance")

        aligned.append(
            {
                **segment,
                "original_speaker_id": original_speaker_id,
                "speaker_id": original_speaker_id,
                "speaker_dominance": speaker_dominance,
                "overlap": has_overlap,
                "review_reasons": review_reasons,
                "uncertain": bool(review_reasons),
            }
        )

    return {
        "speakers": speakers,
        "segments": aligned,
        "regular_turn_count": len(regular),
        "exclusive_turn_count": len(exclusive),
        "alignment_policy": ALIGNMENT_POLICY,
    }
