"""THROWAWAY: can a source audit select anchored text without generating new claims?

The in-memory catalog permits existing draft text, safe prefixes and optional whole-source
quotes. The model returns catalog references in the existing Notes schema. No database is used.
This is an experiment, not production code. Run scripts/notes-audit-prototype.py for the TUI.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from asr_studio import notes_document as nd

_MARKERS = frozenset(
    {
        "not",
        "no",
        "never",
        "unknown",
        "unconfirmed",
        "undecided",
        "uncommitted",
        "pending",
        "proposed",
        "proposal",
        "may",
        "might",
        "could",
        "if",
        "unless",
        "không",
        "chưa",
        "nếu",
    }
)


def words(text):
    return frozenset(re.findall(r"\w+", text.casefold()))


@dataclass
class Catalog:
    source: dict[str, Any]
    draft: dict[str, Any]
    excerpts: bool = False

    def __post_init__(self):
        self.refs = {}
        self.index = []
        number = 0
        for section, _ in nd.SECTIONS:
            for position, item in enumerate(self.draft[section]):
                origin = (section, position)
                text = item["text"]
                key = f"d{number}"
                self.refs[key] = (text, origin, None)
                self.index.append({"ref": key, "section": section, "index": position})
                endings = []
                for match in re.finditer(r"\S+", text):
                    end = match.end()
                    if end == len(text) or words(text[end:]) & _MARKERS:
                        continue
                    ref = f"{key}p{end}"
                    self.refs[ref] = (text[:end], origin, None)
                    endings.append({"ref": ref, "ending": text[max(0, end - 32) : end]})
                self.index[-1]["prefixes"] = endings
                number += 1
        if self.excerpts:
            for index, segment in enumerate(self.source["segments"]):
                key = f"s{index}"
                self.refs[key] = (segment["text"], None, segment["id"])

    def prompt(self):
        instruction = (
            "Verify a draft against the complete source. Return the existing six-section "
            "Notes JSON. "
            "IMPORTANT: every item.text must be a catalog reference ID, never prose. "
            "This is selection, not rewriting. Do not add words or domain knowledge. "
            "Keep a draft reference only when all its claims have explicit source support. "
            "If only a prefix is supported, choose its prefix reference. Delete unsupported tails, "
            "including named procedures inferred from noisy speech. A plausible completion "
            "is not evidence. "
            "Keep uncertainty and final states. Each distinct fact appears once. Remove duplicate "
            "decision/action and question/topic restatements without losing distinct facts. "
            "Use the same section unless a claim must become tentative. Never promote an item. "
            "Copy action metadata from its draft or clear unsupported metadata. Cite "
            "complete source context. "
        )
        if self.excerpts:
            instruction += (
                "Recover omitted distinct source facts with sN references: sN is the WHOLE "
                "source.segments[N].text. Use source excerpts only in key_topics or "
                "confirmation_items. Preserve public support/opposition separately from "
                "formal decisions. Do not omit a perspective just because the formal result is "
                "present. "
            )
        else:
            instruction += "No source-excerpt references are enabled. "
        return (
            instruction
            + "\nAUDIT_INPUT_JSON:\n"
            + json.dumps(
                {
                    "source": nd.provider_source(self.source),
                    "draft": self.draft,
                    "catalog": self.index,
                    "source_excerpt_count": len(self.source["segments"]) if self.excerpts else 0,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    def apply(self, selected):
        result = {"schema_version": 1, **{section: [] for section, _ in nd.SECTIONS}}
        used = set()
        for section, _ in nd.SECTIONS:
            for item in selected[section]:
                ref = item["text"].strip()
                if ref not in self.refs:
                    raise ValueError("text is not an allowed reference")
                text, origin, source_id = self.refs[ref]
                candidate = copy.deepcopy(item)
                if origin is None:
                    if section not in ("key_topics", "confirmation_items"):
                        raise ValueError("a source excerpt cannot become a decision or action")
                    identity = ("source", source_id)
                    candidate["source_ids"] = list(
                        dict.fromkeys([source_id, *candidate["source_ids"]])
                    )
                    candidate["text"] = "Source excerpt: “" + text + "”"
                else:
                    old_section, index = origin
                    if section != old_section and section not in (
                        "key_topics",
                        "confirmation_items",
                    ):
                        raise ValueError("section promotion is not allowed")
                    identity = origin
                    if section == "action_items":
                        for key in nd.NOTES_ACTION_FIELD_NAMES:
                            if (
                                candidate[key] is not None
                                and candidate[key] != self.draft[old_section][index][key]
                            ):
                                raise ValueError("new action metadata is not allowed")
                    candidate["text"] = text
                if identity in used:
                    raise ValueError("an origin can be selected only once")
                used.add(identity)
                result[section].append(candidate)
        return nd.validate_document(result, self.source)
