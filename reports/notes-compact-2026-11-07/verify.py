"""Verify published synthetic evidence offline; long content remains local."""

import copy
import hashlib
import json
from pathlib import Path

from asr_studio import notes_document as nd

root = Path(__file__).resolve().parent


def read(name):
    return json.loads((root / name).read_text())


plan = read("plan.json")
assert (
    hashlib.sha256((root / "plan.json").read_bytes()).hexdigest()
    == "8e33762b69de9a0aa105b4074dc3f1beab5ddf492b23b5a767e656bfd687fe7b"
)
before, after = read("requests-before-fix.json"), read("requests-after-fix.json")
assert before == after
receipt = read("replay-receipt.json")
assert receipt["new_provider_calls"] == 0
for case in plan["cases"]:
    name = case["id"]
    record = next(item for item in receipt["cases"] if item["case"] == name)
    assert record["request_sha256"] == after[name]["request_sha256"]
    if name == "long":
        continue  # No redistributed transcript or derived Notes in this package.
    source_path = root / case["source"]
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == case["source_sha256"]
    assert hashlib.sha256((root / case["gold"]).read_bytes()).hexdigest() == case["gold_sha256"]
    baseline_path = root / "results" / f"{name}.json"
    candidate_path = root / "candidate-results" / f"{name}.json"
    assert hashlib.sha256(baseline_path.read_bytes()).hexdigest() == record["baseline_sha256"]
    assert hashlib.sha256(candidate_path.read_bytes()).hexdigest() == record["candidate_sha256"]
    baseline = read(f"results/{name}.json")["document"]
    candidate = read(f"candidate-results/{name}.json")["document"]
    source = json.loads(source_path.read_text())
    assert nd.validate_document(copy.deepcopy(baseline), source) == candidate
    expected = copy.deepcopy(baseline)
    if name == "proposal":
        action = next(item for item in expected["action_items"] if item["owner"] == "Priya")
        assert action["deadline_text"] == "by tomorrow" and action["deadline_date"] is None
        action["deadline_date"] = "2026-11-11"
    assert expected == candidate
    budget = nd.notes_budget("chatgpt", plan["model"])
    projection = nd.select_notes_source_projection(
        source, output_language=case["output_language"], budget=budget
    )
    ids = tuple(dict.fromkeys(item["id"] for item in projection.snapshot["segments"]))
    prompt = nd.build_prompt(
        projection.snapshot,
        allowed_source_ids=ids,
        output_language=case["output_language"],
        budget=budget,
        max_unit_size=projection.max_unit_size,
    )
    body = nd.build_chatgpt_notes_request_body(
        model=plan["model"],
        prompt=prompt,
        schema=nd.build_notes_schema(ids, max_unit_size=projection.max_unit_size),
        reasoning_effort=plan["effort"],
        budget=budget,
    )
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(raw).hexdigest() == after[name]["request_sha256"]
    assert len(raw) == after[name]["request_bytes"]
summary = read("generation-summary.json")
assert (
    sum(item["provider_calls"] for item in summary["cases"]) == summary["total_provider_calls"] == 4
)
assert (
    sum(item["output_recoveries"] for item in summary["cases"]) == summary["total_recoveries"] == 0
)
assert all(item["unresolved_ids"] == 0 for item in summary["cases"])
assert (
    next(item for item in summary["cases"] if item["case"] == "long")["elapsed_seconds"]
    <= plan["gate"]["notes_only_time_budget_seconds"]
)
print(
    "PASS: synthetic source/gold/output hashes, actual validation, request fingerprints; no provider calls. Long content not rechecked by this public-only verifier."
)
