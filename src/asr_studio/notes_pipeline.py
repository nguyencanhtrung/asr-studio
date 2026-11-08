"""Provider-neutral notes generation: plan, call, validate, repair, reduce and restore refs."""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import suppress
from threading import Event, Lock
from typing import Any, Protocol

# Limits are read through the module at call time so tests can lower them.
from asr_studio import notes_document as nd
from asr_studio.notes_audit import AuditCatalog
from asr_studio.notes_context import link_antecedent_context
from asr_studio.notes_reconcile import (
    MAX_RECONCILE_OUTPUT_BYTES,
    ReconcileLedger,
    ReconcileRepair,
    ReconcileResult,
)
from asr_studio.notes_semantic import MAX_SEMANTIC_OUTPUT_BYTES, SemanticAudit

# The code a transport gives a response over its output cap.
_OUTPUT_TOO_LARGE = "provider_output_too_large"
_LOGGER = logging.getLogger(__name__)
_NOTES_VALIDATION_STAGES = frozenset(
    {"single", "chunk", "reduce", "final", "audit", "reconcile", "semantic_audit"}
)
_NOTES_VALIDATION_ATTEMPTS = frozenset({"primary", "repair", "recovery"})
_NOTES_REPAIR_STATES = frozenset({"unavailable", "already_consumed"})


def _bounded_diagnostic_int(value: object, *, minimum: int, maximum: int) -> int | None:
    if type(value) is int and minimum <= value <= maximum:
        return value
    return None


def bounded_notes_validation_diagnostics(
    diagnostics: object, *, budget: nd.NotesBudget, output_bytes_limit: int
) -> dict[str, Any]:
    """Keep only allowlisted notes validation codes and bounded measurements."""
    if not isinstance(diagnostics, dict):
        return {}
    result: dict[str, Any] = {}
    reason = diagnostics.get("validation_reason")
    if isinstance(reason, str) and reason in nd.NOTES_VALIDATION_REASON_CODES:
        result["validation_reason"] = reason
    detail = diagnostics.get("validation_detail")
    if (
        reason == "source_ids"
        and isinstance(detail, str)
        and detail in nd.NOTES_VALIDATION_DETAIL_CODES
    ):
        result["validation_detail"] = detail
    field_name = diagnostics.get("validation_field")
    if (
        reason == "action_fields"
        and isinstance(field_name, str)
        and field_name in nd.NOTES_ACTION_FIELD_NAMES
    ):
        result["validation_field"] = field_name
    stage = diagnostics.get("stage")
    if isinstance(stage, str) and stage in _NOTES_VALIDATION_STAGES:
        result["stage"] = stage
    attempt = diagnostics.get("attempt")
    if isinstance(attempt, str) and attempt in _NOTES_VALIDATION_ATTEMPTS:
        result["attempt"] = attempt
    repair_state = diagnostics.get("repair_state")
    if isinstance(repair_state, str) and repair_state in _NOTES_REPAIR_STATES:
        result["repair_state"] = repair_state
    for field, minimum, maximum in (
        ("provider_call_index", 1, nd.MAX_PROVIDER_CALLS),
        ("prompt_bytes", 0, budget.request_guard_bytes),
        ("output_bytes", 0, output_bytes_limit),
        ("response_limit", 1, nd.MAX_OUTPUT_BYTES),
    ):
        value = _bounded_diagnostic_int(diagnostics.get(field), minimum=minimum, maximum=maximum)
        if value is not None:
            result[field] = value
    return result


def log_notes_provider_call(
    *,
    provider: str,
    model: str,
    stage: str,
    attempt: str,
    provider_call_index: int,
    prompt_bytes: int,
    estimated_input_tokens: int,
    usage: object = None,
) -> None:
    """Emit bounded notes-call evidence without prompt, transcript or output text."""
    fields = [
        f"provider={provider}",
        f"model={model}",
        f"stage={stage}",
        f"attempt={attempt}",
        f"provider_call_index={provider_call_index}",
        f"prompt_bytes={prompt_bytes}",
        f"estimated_input_tokens={estimated_input_tokens}",
    ]
    if isinstance(usage, dict):
        for field in ("input_tokens", "output_tokens", "reasoning_tokens"):
            value = usage.get(field)
            if type(value) is int and value >= 0:
                fields.append(f"{field}={value}")
    _LOGGER.info("notes_provider_call %s", " ".join(fields))


def log_notes_validation_failure(
    *,
    provider: str,
    model: str,
    diagnostics: object,
    budget: nd.NotesBudget,
    output_bytes_limit: int | None = None,
) -> dict[str, Any]:
    """Log one safe validation-failure line and return the same bounded diagnostics."""
    safe = bounded_notes_validation_diagnostics(
        diagnostics,
        budget=budget,
        output_bytes_limit=(
            budget.request_guard_bytes if output_bytes_limit is None else output_bytes_limit
        ),
    )
    fields = [f"provider={provider}", f"model={model}"]
    fields.extend(f"{field}={safe[field]}" for field in safe)
    _LOGGER.warning("notes_invalid_output %s", " ".join(fields))
    return safe


class NotesTransport(Protocol):
    """Send one prompt through the pinned provider and return its raw text."""

    def __call__(
        self,
        prompt: str,
        *,
        allowed_source_ids: Sequence[str],
        max_output_bytes: int,
        stage: str,
        attempt: str,
        provider_call_index: int,
        prompt_bytes: int,
        budget: nd.NotesBudget,
        max_unit_size: int = 1,
        response_schema: Mapping[str, Any] | None = None,
    ) -> str: ...


def generate_notes_document(
    *,
    snapshot: dict[str, Any],
    output_language: str,
    transport: NotesTransport,
    invalid_output_error: Callable[[dict[str, Any]], Exception],
    limit_error: Callable[[str], Exception],
    on_coverage: Callable[[dict[str, Any]], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Run the notes synthesis for one task through a provider transport.

    The transport owns credentials, model pinning and provider errors. This function owns
    the call budget, the single repair allowed per task, the bounded recovery from a response
    over its output cap, validation and source-ref restoring, so no provider can skip them.
    `invalid_output_error` receives bounded diagnostics only.
    `on_coverage` gets the coverage manifest, with the accepted call of every planned request
    and the call and recovery counts, only after the final document is valid, so it never
    describes a partial run. `on_progress` receives only bounded stage/count measurements and
    is independent from that final-only coverage callback.
    """
    output_language = nd.validate_output_language(output_language)
    single_budget = nd.notes_budget(provider, model)
    projection = nd.select_notes_source_projection(
        snapshot,
        output_language=output_language,
        budget=single_budget,
    )
    projected_snapshot = projection.snapshot
    max_unit_size = projection.max_unit_size
    plan = nd.plan_source(
        snapshot,
        projected_snapshot=projected_snapshot,
        output_language=output_language,
        provider=provider,
        model=model,
        max_unit_size=max_unit_size,
    )
    nd.verify_coverage(snapshot, plan)  # before any provider call
    synthesis_budget = nd.conservative_notes_budget(provider)
    if provider in {"chatgpt", "antigravity"}:
        synthesis_budget = nd.notes_budget(provider, model)
    chunk_output_bytes = nd.notes_chunk_output_bytes(synthesis_budget)
    coverage = copy.deepcopy(plan.manifest)
    call_count = 0
    repair_used = False
    output_recoveries = 0
    last_successful_response: tuple[int, str, str] | None = None
    ledger_lock = Lock()
    aborted = Event()
    progress_lock = Lock()
    progress_high_water: dict[tuple[str, str], int] = {}

    def progress(stage: str, current: int, total: int | None, unit: str) -> None:
        if on_progress is not None:
            with progress_lock:
                key = (stage, unit)
                current = max(current, progress_high_water.get(key, 0))
                if total is not None:
                    current = min(current, total)
                progress_high_water[key] = current
                on_progress({"stage": stage, "current": current, "total": total, "unit": unit})

    progress("preparing_source", 1, 1, "phase")

    def request(
        prompt: str,
        *,
        allowed_source_ids: Sequence[str],
        max_output_bytes: int,
        stage: str,
        attempt: str,
        budget: nd.NotesBudget,
        response_schema: Mapping[str, Any] | None = None,
    ) -> tuple[str, int]:
        nonlocal call_count
        with ledger_lock:
            if aborted.is_set():  # a concurrent semantic-audit part already failed the task
                raise limit_error("The notes request was stopped after a failed request.")
            if call_count >= nd.MAX_PROVIDER_CALLS:
                raise limit_error(
                    "This transcript exceeds the bounded notes synthesis request limit."
                )
            call_count += 1
            provider_call_index = call_count
        transport_kwargs: dict[str, object] = {
            "allowed_source_ids": allowed_source_ids,
            "max_output_bytes": max_output_bytes,
            "stage": stage,
            "attempt": attempt,
            "provider_call_index": provider_call_index,
            "prompt_bytes": len(prompt.encode("utf-8")),
            "budget": budget,
        }
        if max_unit_size > 1:
            transport_kwargs["max_unit_size"] = max_unit_size
        if response_schema is not None:  # None keeps the schema-1 request byte-for-byte
            transport_kwargs["response_schema"] = response_schema
        return transport(prompt, **transport_kwargs), provider_call_index

    def invalid_output(
        validation_error: Exception,
        *,
        stage: str,
        provider_call_index: int,
        attempt: str | None,
        raw_output: object,
        repair_state: str | None,
        reported_output_bytes: int | None = None,
        prompt_bytes: int | None = None,
        response_limit: int | None = None,
    ) -> Exception:
        diagnostics: dict[str, Any] = {
            "stage": stage,
            "provider_call_index": provider_call_index,
        }
        if attempt is not None:
            diagnostics["attempt"] = attempt
        if repair_state is not None:
            diagnostics["repair_state"] = repair_state
        if prompt_bytes is not None:
            diagnostics["prompt_bytes"] = prompt_bytes
        if response_limit is not None:
            diagnostics["response_limit"] = response_limit
        reason = getattr(validation_error, "reason_code", None)
        if isinstance(reason, str) and reason in nd.NOTES_VALIDATION_REASON_CODES:
            diagnostics["validation_reason"] = reason
        detail = getattr(validation_error, "detail_code", None)
        if (
            reason == "source_ids"
            and isinstance(detail, str)
            and detail in nd.NOTES_VALIDATION_DETAIL_CODES
        ):
            diagnostics["validation_detail"] = detail
        field_name = getattr(validation_error, "field_name", None)
        if (
            reason == "action_fields"
            and isinstance(field_name, str)
            and field_name in nd.NOTES_ACTION_FIELD_NAMES
        ):
            diagnostics["validation_field"] = field_name
        if reported_output_bytes is not None:
            diagnostics["output_bytes"] = reported_output_bytes
        elif isinstance(raw_output, str):
            try:
                diagnostics["output_bytes"] = len(raw_output.encode("utf-8"))
            except UnicodeError:
                diagnostics["output_bytes"] = len(
                    raw_output.encode("utf-8", errors="surrogatepass")
                )
        return invalid_output_error(diagnostics)

    def parse_and_validate(
        raw_output: object,
        citation_scope: dict[str, Any],
        *,
        max_output_bytes: int,
        extra_validator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(raw_output, str):
            raise nd.NotesDocumentError("The notes response must be text.", reason_code="json")
        try:
            document = json.loads(raw_output)
        except (json.JSONDecodeError, RecursionError):
            raise nd.NotesDocumentError(
                "The notes response must be valid JSON.", reason_code="json"
            ) from None
        try:
            if extra_validator is None:
                document = nd.normalize_duplicate_source_refs(
                    document,
                    nd._unique_source_ids(
                        tuple(segment["id"] for segment in citation_scope["segments"])
                    ),
                    max_unit_size=max_unit_size,
                )
                document = nd.coalesce_exact_duplicate_items(document, max_unit_size=max_unit_size)
            validated = nd.validate_document(
                document,
                citation_scope,
                max_output_bytes=max_output_bytes,
                max_unit_size=max_unit_size,
            )
            if extra_validator is None:
                return validated
            resolved = extra_validator(validated)
            return nd.validate_document(
                resolved,
                citation_scope,
                max_output_bytes=max_output_bytes,
                max_unit_size=max_unit_size,
            )
        except RecursionError:
            raise nd.NotesDocumentError(
                "The notes response must be valid JSON.", reason_code="json"
            ) from None

    def recovery_prompts(
        prompt: str,
        *,
        target: int,
        allowed_source_ids: Sequence[str],
        budget: nd.NotesBudget | None = None,
    ) -> Iterator[str]:
        """Yield the lower-target prompts one request may still send, within both budgets."""
        nonlocal output_recoveries
        for _ in range(nd.MAX_OUTPUT_RECOVERY_ATTEMPTS):
            target = nd.lower_output_target(target)
            try:
                lowered = nd.build_recovery_prompt(
                    prompt,
                    target_bytes=target,
                    allowed_source_ids=allowed_source_ids,
                    output_language=output_language,
                    budget=budget,
                    max_unit_size=max_unit_size,
                )
            except nd.NotesDocumentError:
                return  # the request budget has no room for the lower-target prompt
            with ledger_lock:
                if output_recoveries >= nd.MAX_OUTPUT_RECOVERY_CALLS:
                    return
                output_recoveries += 1
            yield lowered

    def request_document(
        prompt: str,
        citation_scope: dict[str, Any],
        *,
        max_output_bytes: int,
        stage: str,
        repair_prompt: Callable[[str], str],
        budget: nd.NotesBudget,
        extra_validator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        response_schema: Mapping[str, Any] | None = None,
        parse: Callable[[object], dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], int]:
        """Run one logical request; `parse` replaces schema-1 parsing for an internal schema."""
        nonlocal last_successful_response, repair_used
        allowed_source_ids = nd._unique_source_ids(
            tuple(segment["id"] for segment in citation_scope["segments"])
        )
        # A lower-size recovery prompt describes a schema-1 document, so an internal-schema
        # request gets none; its over-cap or oversized answer goes to the shared repair or fails.
        recoveries: Iterator[str] = (
            iter(())
            if parse is not None
            else recovery_prompts(
                prompt,
                # The aim the primary prompt states: the reduction target, else the stage cap.
                target=nd.MAX_REDUCTION_TARGET_BYTES if stage == "reduce" else max_output_bytes,
                allowed_source_ids=allowed_source_ids,
                budget=budget,
            )
        )

        def validate(raw: object) -> dict[str, Any]:
            if parse is not None:
                return parse(raw)
            return parse_and_validate(
                raw,
                citation_scope,
                max_output_bytes=max_output_bytes,
                extra_validator=extra_validator,
            )

        attempt, sent = "primary", prompt
        while True:
            try:
                raw_output, provider_call_index = request(
                    sent,
                    allowed_source_ids=allowed_source_ids,
                    max_output_bytes=max_output_bytes,
                    stage=stage,
                    attempt=attempt,
                    budget=budget,
                    response_schema=response_schema,
                )
            except Exception as error:
                # Only a response over its cap earns a smaller ask; any other failure is final.
                if getattr(error, "code", None) != _OUTPUT_TOO_LARGE:
                    raise
                lowered = next(recoveries, None)
                if lowered is None:
                    raise
                with ledger_lock:
                    recovery_count = output_recoveries
                progress("repairing", max(0, recovery_count - 1), None, "recoveries")
                attempt, sent = "recovery", lowered
                continue
            try:
                validated = validate(raw_output)
                with ledger_lock:
                    if (
                        last_successful_response is None
                        or provider_call_index >= last_successful_response[0]
                    ):
                        last_successful_response = (provider_call_index, attempt, raw_output)
                if attempt == "recovery":
                    with ledger_lock:
                        recovery_count = output_recoveries
                    progress("repairing", recovery_count, None, "recoveries")
                return validated, provider_call_index
            except (nd.NotesDocumentError, TypeError) as validation_error:
                # OMP ignores the response cap, so its overflow is a size failure here.
                if getattr(validation_error, "reason_code", None) == "size":
                    lowered = next(recoveries, None)
                    if lowered is not None:
                        attempt, sent = "recovery", lowered
                        continue
                with ledger_lock:
                    can_repair = not repair_used
                    if can_repair:
                        repair_used = True
                if not can_repair:
                    raise invalid_output(
                        validation_error,
                        stage=stage,
                        provider_call_index=provider_call_index,
                        attempt=attempt,
                        raw_output=raw_output,
                        repair_state="already_consumed",
                        prompt_bytes=len(sent.encode("utf-8")),
                        response_limit=max_output_bytes,
                    ) from None
                progress("repairing", 0, 1, "repairs")
                try:
                    bounded_repair_prompt = repair_prompt(raw_output)
                except (nd.NotesDocumentError, TypeError, AttributeError):
                    raise invalid_output(
                        validation_error,
                        stage=stage,
                        provider_call_index=provider_call_index,
                        attempt=attempt,
                        raw_output=raw_output,
                        repair_state="unavailable",
                        prompt_bytes=len(sent.encode("utf-8")),
                        response_limit=max_output_bytes,
                    ) from None
                repaired_output, repair_call_index = request(
                    bounded_repair_prompt,
                    allowed_source_ids=allowed_source_ids,
                    max_output_bytes=max_output_bytes,
                    stage=stage,
                    attempt="repair",
                    budget=budget,
                    response_schema=response_schema,
                )
                try:
                    validated = validate(repaired_output)
                    with ledger_lock:
                        if (
                            last_successful_response is None
                            or repair_call_index >= last_successful_response[0]
                        ):
                            last_successful_response = (
                                repair_call_index,
                                "repair",
                                repaired_output,
                            )
                    progress("repairing", 1, 1, "repairs")
                    return validated, repair_call_index
                except (nd.NotesDocumentError, TypeError) as repair_error:
                    raise invalid_output(
                        repair_error,
                        stage=stage,
                        provider_call_index=repair_call_index,
                        attempt="repair",
                        raw_output=repaired_output,
                        repair_state="already_consumed",
                        prompt_bytes=len(bounded_repair_prompt.encode("utf-8")),
                        response_limit=max_output_bytes,
                    ) from None

    def restore_final_document(document: dict[str, Any], *, stage: str) -> dict[str, Any]:
        restored = nd.restore_source_refs(document, projection.alias_to_original)
        try:
            restored = nd.coalesce_exact_duplicate_items(restored)
            return nd.validate_document(restored, snapshot, max_output_bytes=nd.MAX_OUTPUT_BYTES)
        except (nd.NotesDocumentError, TypeError) as validation_error:
            with ledger_lock:
                response = last_successful_response
                latest_call_count = call_count
            call_index = response[0] if response is not None else max(latest_call_count, 1)
            try:
                expanded_bytes = len(
                    json.dumps(
                        restored,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                )
            except (TypeError, ValueError, UnicodeError):
                expanded_bytes = None
            raise invalid_output(
                validation_error,
                stage=stage,
                provider_call_index=call_index,
                attempt=None,
                raw_output=None,
                repair_state=None,
                reported_output_bytes=expanded_bytes,
            ) from None

    def finish(document: dict[str, Any]) -> dict[str, Any]:
        with ledger_lock:
            coverage["provider_calls"] = call_count
            coverage["output_recoveries"] = output_recoveries
        if on_coverage is not None:
            on_coverage(coverage)
        return document

    def audit_request_parts(
        draft: dict[str, Any],
    ) -> tuple[Callable[[str | None], str], Callable[[dict[str, Any]], dict[str, Any]] | None]:
        """Build one source-audit prompt and its boundary resolver for one accepted draft."""
        catalog = (
            AuditCatalog.from_documents(projected_snapshot, draft)
            if provider == "antigravity"
            else None
        )
        catalog_payload = catalog.prompt_payload() if catalog is not None else None

        def audit_prompt(invalid_output: str | None = None) -> str:
            return nd.build_reduction_prompt(
                projected_snapshot,
                [draft],
                final=True,
                invalid_output=invalid_output,
                output_language=output_language,
                budget=single_budget,
                max_unit_size=max_unit_size,
                require_source=True,
                audit=True,
                audit_catalog=catalog_payload,
            )

        def audit_validator(document: dict[str, Any]) -> dict[str, Any]:
            assert catalog is not None
            return catalog.apply(
                document,
                max_output_bytes=nd.MAX_OUTPUT_BYTES,
                max_unit_size=max_unit_size,
                output_language=output_language,
            )

        return audit_prompt, audit_validator if catalog is not None else None

    if not plan.chunked:
        source_ids = nd._unique_source_ids(
            tuple(segment["id"] for segment in projected_snapshot["segments"])
        )
        progress("generating", 0, 1, "calls")
        document, accepted_call_index = request_document(
            nd.build_prompt(
                projected_snapshot,
                allowed_source_ids=source_ids,
                output_language=output_language,
                budget=single_budget,
                max_unit_size=max_unit_size,
            ),
            projected_snapshot,
            max_output_bytes=nd.MAX_OUTPUT_BYTES,
            stage="single",
            repair_prompt=lambda raw: nd.build_prompt(
                projected_snapshot,
                allowed_source_ids=source_ids,
                invalid_output=raw,
                output_language=output_language,
                budget=single_budget,
                max_unit_size=max_unit_size,
            ),
            budget=single_budget,
        )
        coverage["chunks"][0]["provider_call_index"] = accepted_call_index
        if single_budget.uses_token_estimate:
            draft = document
            audit_prompt, audit_validator = audit_request_parts(draft)

            try:
                source_audit_prompt = audit_prompt()
            except nd.NotesDocumentError:
                coverage["source_audit"] = {"status": "skipped", "reason": "prompt_budget"}
            else:
                progress("merging", 0, 1, "calls")
                audited_document, audit_call_index = request_document(
                    source_audit_prompt,
                    projected_snapshot,
                    max_output_bytes=nd.MAX_OUTPUT_BYTES,
                    stage="audit",
                    repair_prompt=audit_prompt,
                    budget=single_budget,
                    extra_validator=audit_validator,
                )
                document = audited_document
                coverage["source_audit"] = {
                    "status": "applied",
                    "provider_call_index": audit_call_index,
                    "mode": "references" if provider == "antigravity" else "rewrite",
                }
                progress("merging", 1, 1, "calls")
        document = restore_final_document(
            document,
            stage="audit"
            if coverage.get("source_audit", {}).get("status") == "applied"
            else "single",
        )
        progress("generating", 1, 1, "calls")
        return finish(document)

    prepared_chunks: list[tuple[dict[str, Any], str, Callable[[str], str]]] = []
    for index, chunk in enumerate(plan.chunks, start=1):
        projected_chunk = projection.project_chunk(chunk)
        source_ids = nd._unique_source_ids(
            tuple(segment["id"] for segment in projected_chunk["segments"])
        )
        prompt = nd.build_chunk_prompt(
            projected_chunk,
            chunk_index=index,
            chunk_count=len(plan.chunks),
            allowed_source_ids=source_ids,
            output_language=output_language,
            budget=synthesis_budget,
            max_unit_size=max_unit_size,
            max_output_bytes=chunk_output_bytes,
            fact_inventory=plan.fact_inventory,
        )

        def repair_chunk(
            raw: str,
            *,
            source_chunk: dict[str, Any] = projected_chunk,
            chunk_index: int = index,
            allowed_ids: tuple[str, ...] = source_ids,
        ) -> str:
            return nd.build_chunk_prompt(
                source_chunk,
                chunk_index=chunk_index,
                chunk_count=len(plan.chunks),
                allowed_source_ids=allowed_ids,
                invalid_output=raw,
                output_language=output_language,
                budget=synthesis_budget,
                max_unit_size=max_unit_size,
                max_output_bytes=chunk_output_bytes,
                fact_inventory=plan.fact_inventory,
            )

        prepared_chunks.append((projected_chunk, prompt, repair_chunk))

    map_results: list[tuple[dict[str, Any], int] | None] = [None] * len(prepared_chunks)
    parallel_maps = (
        provider in {"chatgpt", "antigravity"}
        and synthesis_budget.uses_token_estimate
        and len(prepared_chunks) > 1
    )
    if parallel_maps:
        progress("chunking", 0, len(prepared_chunks), "chunks")
        executor = ThreadPoolExecutor(
            max_workers=3 if plan.fact_inventory else 2, thread_name_prefix="notes-map"
        )
        futures: dict[Future[tuple[dict[str, Any], int]], int] = {}
        try:
            for index, (projected_chunk, prompt, repair_chunk) in enumerate(prepared_chunks):
                futures[
                    executor.submit(
                        request_document,
                        prompt,
                        projected_chunk,
                        max_output_bytes=chunk_output_bytes,
                        stage="chunk",
                        repair_prompt=repair_chunk,
                        budget=synthesis_budget,
                    )
                ] = index
            for completed_count, future in enumerate(as_completed(futures), start=1):
                map_results[futures[future]] = future.result()
                progress("chunking", completed_count, len(prepared_chunks), "chunks")
        except BaseException:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
    else:
        for index, (projected_chunk, prompt, repair_chunk) in enumerate(prepared_chunks):
            progress("chunking", index, len(prepared_chunks), "chunks")
            map_results[index] = request_document(
                prompt,
                projected_chunk,
                max_output_bytes=chunk_output_bytes,
                stage="chunk",
                repair_prompt=repair_chunk,
                budget=synthesis_budget,
            )
            progress("chunking", index + 1, len(prepared_chunks), "chunks")

    summaries: list[dict[str, Any]] = []
    previous_document: dict[str, Any] | None = None
    for index, ((projected_chunk, _prompt, _repair), result) in enumerate(
        zip(prepared_chunks, map_results, strict=True), start=1
    ):
        if result is None:
            raise limit_error("The notes map did not return a document for every source part.")
        document, accepted_call_index = result
        coverage["chunks"][index - 1]["provider_call_index"] = accepted_call_index
        if previous_document is not None:
            # The previous chunk read the overlap in full, so an exact repeat can be removed;
            # paraphrases and metadata/source differences remain for final reconciliation.
            overlap_ids = [
                segment["id"] for segment in projected_chunk["segments"] if segment.get("overlap")
            ]
            summaries.append(nd.drop_overlap_duplicates(previous_document, document, overlap_ids))
        else:
            summaries.append(document)
        previous_document = document

    reduction_calls = 0

    def reduction_prompt(
        group: list[dict[str, Any]], *, final: bool, invalid_output: str | None = None
    ) -> str:
        return nd.build_reduction_prompt(
            projected_snapshot,
            group,
            final=final,
            invalid_output=invalid_output,
            output_language=output_language,
            budget=synthesis_budget,
            max_unit_size=max_unit_size,
        )

    def fits(group: list[dict[str, Any]]) -> bool:
        try:
            reduction_prompt(group, final=False)
        except nd.NotesDocumentError:
            return False
        return True

    def reduce_group(group: list[dict[str, Any]]) -> dict[str, Any]:
        nonlocal reduction_calls
        if reduction_calls >= nd.MAX_REDUCTION_CALLS:
            raise limit_error("This transcript exceeds the bounded notes reduction limit.")
        progress("merging", reduction_calls, None, "calls")
        reduction_calls += 1
        result, _accepted_call_index = request_document(
            reduction_prompt(group, final=False),
            nd._prompt_citation_scope(projected_snapshot, group),
            max_output_bytes=nd.MAX_REDUCTION_OUTPUT_BYTES,
            stage="reduce",
            repair_prompt=lambda raw, inputs=group: reduction_prompt(
                inputs, final=False, invalid_output=raw
            ),
            budget=synthesis_budget,
        )
        progress("merging", reduction_calls, None, "calls")
        return result

    def document_bytes(document: dict[str, Any]) -> int:
        return len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def compact(document: dict[str, Any]) -> dict[str, Any]:
        reduced = reduce_group([document])
        if document_bytes(reduced) >= document_bytes(document):
            raise limit_error("Notes synthesis could not compact a summary within its limit.")
        return reduced

    def reconcile(
        final_document: dict[str, Any],
        *,
        reference_evidence: bool = False,
        split: bool = False,
    ) -> dict[str, Any]:
        """One structured patch over the projected final draft, map ledger and full source.

        With `split` (registered Gemini fact inventory only) the ledger is cut into at most
        `MAX_RECONCILE_PARTS` owned views, each over the full source with the rest of the
        ledger as read-only context. They run concurrently; every view must validate before
        `combine_results` merges them, and any failed view fails the task.
        """
        nonlocal reduction_calls
        ledger = ReconcileLedger.build(
            projected_snapshot,
            final_document,
            summaries,
            max_unit_size=max_unit_size,
            strict_names=nd._is_known_gemini(synthesis_budget),
            reference_evidence=reference_evidence,
        )
        views = ledger.partitions() if split else (ledger,)
        if len(views) > 1:
            # The caller charged one reconcile call; every further view is one more.
            if reduction_calls + len(views) - 1 > nd.MAX_REDUCTION_CALLS:
                raise limit_error("This transcript exceeds the bounded notes reduction limit.")
            reduction_calls += len(views) - 1

        def prepare(
            view: ReconcileLedger,
        ) -> tuple[str, Callable[[str | None], str], Callable[[object], dict[str, Any]], list[Any]]:
            # Each view owns its repair plan and accepted result; the one repair stays global.
            repairs: list[ReconcileRepair] = []
            accepted: list[ReconcileResult] = []

            def reconcile_prompt(invalid_output: str | None = None) -> str:
                # Per-ref failures get a sparse repair over the accepted ops; a global failure
                # keeps the bounded generic repair, unavailable above its raw-output cap.
                plan = None if invalid_output is None else view.plan_repair(invalid_output)
                if plan is not None:
                    repairs.append(plan)
                    return view.build_repair_prompt(
                        plan, output_language=output_language, budget=synthesis_budget
                    )
                return view.build_prompt(
                    output_language=output_language,
                    invalid_output=invalid_output,
                    budget=synthesis_budget,
                )

            def parse(raw: object) -> dict[str, Any]:
                result = view.parse_repair(raw, repairs[-1]) if repairs else view.parse(raw)
                accepted.append(result)
                return result.document

            try:
                prompt = reconcile_prompt()
            except nd.NotesDocumentError:
                # The long known-budget path requires this audit; publishing the unchecked draft
                # would misreport it, so the bounded limit fails the task instead.
                raise limit_error(
                    "This transcript exceeds the bounded notes source reconciliation limit."
                ) from None
            return prompt, reconcile_prompt, parse, accepted

        prepared = [prepare(view) for view in views]

        def run(
            view: ReconcileLedger,
            prompt: str,
            repair_prompt: Callable[[str | None], str],
            parse: Callable[[object], dict[str, Any]],
        ) -> tuple[dict[str, Any], int]:
            try:
                return request_document(
                    prompt,
                    projected_snapshot,
                    max_output_bytes=MAX_RECONCILE_OUTPUT_BYTES,
                    stage="reconcile",
                    repair_prompt=repair_prompt,
                    budget=synthesis_budget,
                    response_schema=view.schema(),
                    parse=parse,
                )
            except BaseException:
                aborted.set()  # a sibling view sends no further request
                raise

        progress("merging", reduction_calls, None, "calls")
        if len(views) == 1:
            prompt, repair_prompt, parse, accepted = prepared[0]
            document, call_index = request_document(
                prompt,
                projected_snapshot,
                max_output_bytes=MAX_RECONCILE_OUTPUT_BYTES,
                stage="reconcile",
                repair_prompt=repair_prompt,
                budget=synthesis_budget,
                response_schema=views[0].schema(),
                parse=parse,
            )
            indices = [call_index]
            counts = accepted[-1].counts
        else:
            results: list[tuple[dict[str, Any], int] | None] = [None] * len(views)
            executor = ThreadPoolExecutor(
                max_workers=len(views), thread_name_prefix="notes-reconcile"
            )
            futures = {
                executor.submit(run, view, prompt, repair_prompt, parse): index
                for index, (view, (prompt, repair_prompt, parse, _accepted)) in enumerate(
                    zip(views, prepared, strict=True)
                )
            }
            try:
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
            except BaseException:
                aborted.set()
                executor.shutdown(wait=True, cancel_futures=True)
                raise
            executor.shutdown(wait=True)
            indices = [result[1] for result in results if result is not None]
            if len(indices) != len(views) or not all(item[3] for item in prepared):
                raise limit_error("The notes reconciliation did not return every partition.")
            try:
                combined = ledger.combine_results(
                    [(view, item[3][-1]) for view, item in zip(views, prepared, strict=True)]
                )
            except nd.NotesDocumentError as error:
                raise invalid_output(
                    error,
                    stage="reconcile",
                    provider_call_index=max(indices),
                    attempt=None,
                    raw_output=None,
                    repair_state=None,
                ) from None
            document, counts = combined.document, combined.counts
        source_audit: dict[str, Any] = {
            "status": "applied",
            "mode": "reconcile",
            "provider_call_index": max(indices),
            "counts": counts,
        }
        if split:
            source_audit["provider_call_indices"] = indices
            source_audit["part_count"] = len(views)
        coverage["source_audit"] = source_audit
        return document

    def semantic_audit(final_document: dict[str, Any]) -> dict[str, Any]:
        """The mandatory sparse source-binding pass (registered Gemini only).

        At most four disjoint item partitions run concurrently, each over the full source; their
        validated patches are merged and applied once. Any failed part fails the whole task.
        """
        audit = SemanticAudit(
            projected_snapshot,
            final_document,
            max_unit_size=max_unit_size,
            output_language=output_language,
        )
        parts: list[tuple[SemanticAudit, str, Callable[[str | None], str]]] = []
        for part in audit.partitions():

            def audit_prompt(invalid_output: str | None = None, part: SemanticAudit = part) -> str:
                return part.build_prompt(
                    output_language=output_language,
                    invalid_output=invalid_output,
                    budget=synthesis_budget,
                )

            try:
                parts.append((part, audit_prompt(), audit_prompt))
            except nd.NotesDocumentError:
                # Required for this path: skipping it would publish an unaudited binding.
                raise limit_error(
                    "This transcript exceeds the bounded notes semantic audit limit."
                ) from None

        def run(
            part: SemanticAudit, prompt: str, audit_prompt: Callable[[str | None], str]
        ) -> tuple[dict[str, Any], int]:
            try:
                return request_document(
                    prompt,
                    projected_snapshot,
                    max_output_bytes=MAX_SEMANTIC_OUTPUT_BYTES,
                    stage="semantic_audit",
                    repair_prompt=audit_prompt,
                    budget=synthesis_budget,
                    response_schema=part.schema(),
                    parse=lambda raw: part.validated_patch(raw, output_language=output_language),
                )
            except BaseException:
                aborted.set()  # the other part sends no further request
                raise

        results: list[tuple[dict[str, Any], int] | None] = [None] * len(parts)
        if len(parts) == 1:
            results[0] = run(*parts[0])
        else:
            executor = ThreadPoolExecutor(max_workers=len(parts), thread_name_prefix="notes-audit")
            futures = {executor.submit(run, *part): index for index, part in enumerate(parts)}
            try:
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
            except BaseException:
                aborted.set()
                executor.shutdown(wait=True, cancel_futures=True)
                raise
            executor.shutdown(wait=True)
        indices = [result[1] for result in results if result is not None]
        if len(indices) != len(parts):
            raise limit_error("The notes semantic audit did not return every partition.")
        try:
            document, counts = audit.combine_patches(
                [
                    (part, result[0])
                    for (part, _p, _r), result in zip(parts, results, strict=True)
                    if result is not None
                ],
                output_language=output_language,
            )
        except nd.NotesDocumentError as error:
            raise invalid_output(
                error,
                stage="semantic_audit",
                provider_call_index=max(indices),
                attempt=None,
                raw_output=None,
                repair_state=None,
            ) from None
        coverage["semantic_audit"] = {
            "status": "applied",
            "provider_call_index": max(indices),
            "provider_call_indices": indices,
            "part_count": len(parts),
            "counts": counts,
        }
        return document

    if plan.fact_inventory:
        # Long known budget: the structured reconcile over every map fact and the full source
        # is the final synthesis; no lossy free-form final or reduction runs before it.
        empty_seed: dict[str, Any] = {
            "schema_version": nd.SCHEMA_VERSION,
            **{section: [] for section, _heading in nd.SECTIONS},
        }
        reduction_calls += 1
        # Registered Gemini: the semantic audit is mandatory, so reconcile may rebind evidence.
        semantic_required = nd._is_known_gemini(synthesis_budget)
        final_document = reconcile(
            empty_seed, reference_evidence=semantic_required, split=semantic_required
        )
        if semantic_required:
            final_document = semantic_audit(final_document)
        final_document = restore_final_document(
            final_document,
            stage="semantic_audit" if "semantic_audit" in coverage else "reconcile",
        )
        # After the last provider request: add local antecedent-context citations in code.
        linked, context_counts = link_antecedent_context(
            final_document,
            snapshot,
            max_refs_per_item=nd.MAX_SOURCE_REFS_PER_ITEM,
            strict=nd._is_known_gemini(synthesis_budget),
        )
        try:
            final_document = nd.validate_document(
                linked, snapshot, max_output_bytes=nd.MAX_OUTPUT_BYTES
            )
            coverage["source_context"] = {"status": "applied", **context_counts}
        except nd.NotesDocumentError:
            # Context links are optional provenance; the validated document stays as it was.
            coverage["source_context"] = {"status": "skipped", "reason": "limit"}
        progress("merging", reduction_calls, None, "calls")
        return finish(final_document)

    documents = summaries
    for _level in range(nd.MAX_REDUCTION_LEVELS):
        final_prompt = None
        with suppress(nd.NotesDocumentError):
            final_prompt = reduction_prompt(documents, final=True)
        if final_prompt is not None:
            final_scope = (
                projected_snapshot
                if nd.reduction_prompt_includes_source(final_prompt)
                else nd._prompt_citation_scope(projected_snapshot, documents)
            )
            if reduction_calls >= nd.MAX_REDUCTION_CALLS:
                raise limit_error("This transcript exceeds the bounded notes reduction limit.")
            progress("merging", reduction_calls, None, "calls")
            reduction_calls += 1
            final_document, _accepted_call_index = request_document(
                final_prompt,
                final_scope,
                max_output_bytes=nd.MAX_OUTPUT_BYTES,
                stage="final",
                repair_prompt=lambda raw, inputs=documents: reduction_prompt(
                    inputs, final=True, invalid_output=raw
                ),
                budget=synthesis_budget,
            )
            final_stage = "final"
            if provider == "chatgpt" and synthesis_budget.uses_token_estimate:
                final_document = reconcile(final_document)
                final_stage = "reconcile"
            elif synthesis_budget.uses_token_estimate:
                coverage["source_audit"] = {
                    "status": "skipped",
                    "reason": "quality_not_qualified",
                }
            # Projected IDs are restored exactly once, at publication.
            final_document = restore_final_document(final_document, stage=final_stage)
            progress("merging", reduction_calls, None, "calls")
            return finish(final_document)

        # Merge neighbours in source order. When two summaries leave no room for each other, the
        # larger one is compacted alone and the other moves on, so none is dropped or cut.
        next_documents: list[dict[str, Any]] = []
        for position in range(0, len(documents), 2):
            group = documents[position : position + 2]
            if not all(fits([item]) for item in group):
                raise limit_error("A notes summary exceeds the bounded synthesis request limit.")
            if len(group) == 2 and fits(group):
                next_documents.append(reduce_group(group))
            elif len(group) == 2:
                larger = max(group, key=document_bytes)
                next_documents.extend(compact(item) if item is larger else item for item in group)
            else:
                next_documents.extend(group)
        documents = next_documents

    raise limit_error("This transcript exceeds the bounded notes reduction depth.")
