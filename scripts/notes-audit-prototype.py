#!/usr/bin/env python3
"""THROWAWAY TUI. Run: PYTHONPATH=src python3 scripts/notes-audit-prototype.py"""

import json

from asr_studio.notes_audit_prototype import Catalog
from asr_studio.notes_document import SECTIONS

source = {
    "segments": [
        {
            "id": "source1",
            "text": "A checklist is needed. Visitor tracking is not agreed.",
            "start_seconds": 0,
            "end_seconds": 1,
        }
    ],
    "speakers": [],
    "meeting_date": None,
}
draft = {"schema_version": 1, **{section: [] for section, _ in SECTIONS}}
draft["key_topics"] = [
    {"text": "A checklist is needed, a biometric reader was installed", "source_ids": ["source1"]}
]
catalog = Catalog(source, draft, excerpts=True)
state = {"selected": None, "result": None, "error": None}
while True:
    print("\033[2J\033[H", end="")
    print("PROTOTYPE — anchored source audit\n")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    print("\n[s] whole draft [p] supported prefix [x] invented text [q] quit")
    key = input("> ").strip()
    if key == "q":
        break
    selected = {"schema_version": 1, **{section: [] for section, _ in SECTIONS}}
    ref = (
        "d0"
        if key == "s"
        else next(k for k, v in catalog.refs.items() if v[0] == "A checklist is needed,")
        if key == "p"
        else "invented"
    )
    selected["key_topics"] = [{"text": ref, "source_ids": ["source1"]}]
    state["selected"] = selected
    try:
        state["result"] = catalog.apply(selected)
        state["error"] = None
    except ValueError as error:
        state["result"] = None
        state["error"] = str(error)
