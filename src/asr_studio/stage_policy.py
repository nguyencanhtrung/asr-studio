"""Strict, durable stage-policy contract shared by the operator and extension APIs.

The policy format is deliberately independent from admission.  A client may
understand a format while the server keeps that format fenced from execution
until the runtime cutover is complete.
"""

from __future__ import annotations

from typing import Any

STAGE_POLICY_CONTRACT_VERSION = 1
STAGE_POLICY_FORMAT_VERSION = 1
STAGE_POLICY_FORMATS = frozenset({STAGE_POLICY_FORMAT_VERSION})
STAGE_ADMISSION_STATES = frozenset({"enabled", "draining", "disabled"})
STAGE_POLICY_KEYS = frozenset({"asr", "diarization", "notes"})


class StagePolicyValidationError(ValueError):
    """A client-visible stage-policy validation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_format_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StagePolicyValidationError(
            "invalid_stage_policy_format", "format_version must be an integer."
        )
    if value not in STAGE_POLICY_FORMATS:
        raise StagePolicyValidationError(
            "unsupported_stage_policy_format",
            "The requested stage-policy format is not supported.",
        )
    return value


def validate_policy_values(
    *, format_version: object, asr: object, diarization: object, notes: object
) -> dict[str, object]:
    version = validate_format_version(format_version)
    values = {"asr": asr, "diarization": diarization, "notes": notes}
    if any(type(value) is not bool for value in values.values()):
        raise StagePolicyValidationError(
            "invalid_stage_policy", "Stage policy values must be booleans."
        )
    return {"format_version": version, **values}


def validate_intent(value: object) -> int:
    """Validate the only durable create-time intent a capable client pins."""
    if not isinstance(value, dict) or set(value) != {"format_version"}:
        raise StagePolicyValidationError(
            "invalid_stage_policy_intent",
            "stage_policy intent must contain only format_version.",
        )
    return validate_format_version(value["format_version"])


def policy_from_row(row: Any) -> dict[str, bool]:
    return {
        "asr": bool(row["stage_asr_enabled"]),
        "diarization": bool(row["stage_diarization_enabled"]),
        "notes": bool(row["stage_notes_enabled"]),
    }


def admission_from_row(row: Any) -> dict[str, object]:
    state = row["admission"]
    if state not in STAGE_ADMISSION_STATES:
        # Unknown persisted gates are fail-closed and never decoded as enabled.
        state = "disabled"
        reason = "unknown_admission_state"
    else:
        reason = row["reason"]
    return {
        "state": state,
        "reason": reason,
        "format_version": int(row["format_version"]),
    }


def capabilities(*, admission: dict[str, object]) -> dict[str, object]:
    """Return format knowledge without advertising runtime readiness."""
    return {
        "contract_version": STAGE_POLICY_CONTRACT_VERSION,
        "format_version": STAGE_POLICY_FORMAT_VERSION,
        "supported": True,
        "admission": admission,
    }
