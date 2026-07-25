from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import INVALID_TRANSITION, VsdpError


class CeremonyState(str, Enum):
    DRAFT = "draft"
    DELIBERATING = "deliberating"
    SETUP_FROZEN = "setup_frozen"
    KEY_CEREMONY = "key_ceremony"
    ENROLLING = "enrolling"
    FINAL_FROZEN = "final_frozen"
    VOTING = "voting"
    SEALED = "sealed"
    TALLYING = "tallying"
    PROVISIONAL = "provisional"
    FINALIZED = "finalized"
    DISPUTED = "disputed"
    ABORTED = "aborted"


_FORWARD: dict[CeremonyState, set[CeremonyState]] = {
    CeremonyState.DRAFT: {CeremonyState.DELIBERATING},
    CeremonyState.DELIBERATING: {CeremonyState.SETUP_FROZEN},
    CeremonyState.SETUP_FROZEN: {CeremonyState.KEY_CEREMONY},
    CeremonyState.KEY_CEREMONY: {CeremonyState.ENROLLING},
    CeremonyState.ENROLLING: {CeremonyState.FINAL_FROZEN},
    CeremonyState.FINAL_FROZEN: {CeremonyState.VOTING},
    CeremonyState.VOTING: {CeremonyState.SEALED},
    CeremonyState.SEALED: {CeremonyState.TALLYING},
    CeremonyState.TALLYING: {CeremonyState.PROVISIONAL},
    CeremonyState.PROVISIONAL: {CeremonyState.FINALIZED},
    CeremonyState.DISPUTED: {CeremonyState.ABORTED},
}

_TERMINAL = {CeremonyState.FINALIZED, CeremonyState.ABORTED}


@dataclass(frozen=True)
class Transition:
    old_state: CeremonyState
    new_state: CeremonyState
    input_artifact_root: str
    output_artifact_root: str
    reason_code: str
    evidence_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_ids": list(self.evidence_ids),
            "input_artifact_root": self.input_artifact_root,
            "new_state": self.new_state.value,
            "old_state": self.old_state.value,
            "output_artifact_root": self.output_artifact_root,
            "reason_code": self.reason_code,
            "schema": "vsdp-transition/1",
        }


def transition(
    current: CeremonyState,
    target: CeremonyState,
    *,
    input_artifact_root: str,
    output_artifact_root: str,
    reason_code: str,
    evidence_ids: tuple[str, ...] = (),
) -> Transition:
    if current in _TERMINAL:
        raise VsdpError(INVALID_TRANSITION, f"{current.value} is terminal")
    allowed = set(_FORWARD.get(current, set()))
    if current != CeremonyState.DISPUTED:
        allowed.update({CeremonyState.DISPUTED, CeremonyState.ABORTED})
    if target not in allowed:
        raise VsdpError(
            INVALID_TRANSITION,
            f"transition {current.value} -> {target.value} is not allowed",
        )
    if target == CeremonyState.DISPUTED and not evidence_ids:
        raise VsdpError(INVALID_TRANSITION, "disputed transition requires evidence")
    if not reason_code:
        raise VsdpError(INVALID_TRANSITION, "transition reason code is required")
    return Transition(
        old_state=current,
        new_state=target,
        input_artifact_root=input_artifact_root,
        output_artifact_root=output_artifact_root,
        reason_code=reason_code,
        evidence_ids=evidence_ids,
    )


def replay(initial: CeremonyState, events: list[dict[str, object]]) -> CeremonyState:
    current = initial
    for index, event in enumerate(events):
        if event.get("schema") != "vsdp-transition/1":
            raise VsdpError(INVALID_TRANSITION, f"event {index} has an unknown schema")
        try:
            old_state = CeremonyState(str(event["old_state"]))
            new_state = CeremonyState(str(event["new_state"]))
        except (KeyError, ValueError) as exc:
            raise VsdpError(INVALID_TRANSITION, f"event {index} has invalid state") from exc
        if old_state != current:
            raise VsdpError(INVALID_TRANSITION, f"event {index} does not extend the state chain")
        evidence = tuple(str(item) for item in event.get("evidence_ids", []))
        transition(
            current,
            new_state,
            input_artifact_root=str(event.get("input_artifact_root", "")),
            output_artifact_root=str(event.get("output_artifact_root", "")),
            reason_code=str(event.get("reason_code", "")),
            evidence_ids=evidence,
        )
        current = new_state
    return current
