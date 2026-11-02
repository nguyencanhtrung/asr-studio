"""Read-only citation presentation over one immutable source snapshot."""

import math

MAX_GAP_SECONDS = 1
MAX_GROUP_SECONDS = 60


def group_citations(citations, snapshot):
    """Keep barriers from the entire snapshot, including uncited segments."""
    segments = snapshot.get("segments", [])
    if not segments or any(type(s.get("sequence")) is not int for s in segments):
        return citations
    if len({s["sequence"] for s in segments}) != len(segments):
        return citations
    ordered = sorted(segments, key=lambda s: s["sequence"])
    positions = {s["id"]: i for i, s in enumerate(ordered)}

    overlapping = set()
    for left, right in zip(ordered, ordered[1:], strict=False):
        end, start = left.get("end_seconds"), right.get("start_seconds")
        if isinstance(end, (int, float)) and isinstance(start, (int, float)) and end > start:
            overlapping.update((left["id"], right["id"]))

    def safe(segment):
        start, end = segment.get("start_seconds"), segment.get("end_seconds")
        return (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and math.isfinite(start)
            and math.isfinite(end)
            and 0 <= start < end
            and segment.get("speaker_id") is not None
            and segment["id"] not in overlapping
            and not segment.get("overlap")
            and not segment.get("multiple_speakers")
            and not segment.get("low_dominance")
            and not segment.get("speaker_mismatch")
        )

    result = []
    for entry in citations:
        sources = sorted(entry["sources"], key=lambda s: positions.get(s["id"], len(ordered)))
        groups = []
        previous = None
        for source in sources:
            index = positions.get(source["id"])
            segment = ordered[index] if index is not None else None
            join = (
                previous is not None
                and segment is not None
                and safe(segment)
                and safe(previous)
                and index == positions[previous["id"]] + 1
                and segment["speaker_id"] == previous["speaker_id"]
                and 0 <= segment["start_seconds"] - previous["end_seconds"] <= MAX_GAP_SECONDS
                and segment["end_seconds"] - groups[-1]["start_seconds"] <= MAX_GROUP_SECONDS
            )
            if join:
                group = groups[-1]
                group["members"].append(dict(source))
                group["end_seconds"] = segment["end_seconds"]
            else:
                group = dict(source, members=[dict(source)])
                if segment and safe(segment):
                    group["start_seconds"] = segment["start_seconds"]
                    group["end_seconds"] = segment["end_seconds"]
                groups.append(group)
            previous = segment
        result.append(dict(entry, sources=groups))
    return result
