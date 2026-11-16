"""Typed, fail-closed Intelligent Failure Recovery contracts and policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math
import re


class QualityResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class FailureKind(StrEnum):
    QUALITY_FAILURE = "quality_failure"
    DETERMINISTIC = "deterministic"
    TRANSIENT = "transient"
    ENVIRONMENT = "environment"
    EVALUATION_INCONCLUSIVE = "evaluation_inconclusive"
    OPERATIONAL_UNKNOWN = "operational_unknown"
    SECURITY_OR_AUTHORIZATION_BLOCK = "security_or_authorization_block"


class Recoverability(StrEnum):
    POTENTIALLY_RECOVERABLE = "potentially_recoverable"
    NOT_RECOVERABLE = "not_recoverable"


class EffectState(StrEnum):
    NO_EFFECT_PROVEN = "no_effect_proven"
    RECONCILED_SAFE = "reconciled_safe"
    EFFECTS_POSSIBLE = "effects_possible"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailureDiagnosis:
    failure_kind: FailureKind
    recoverability: Recoverability
    effect_state: EffectState
    failure_code: str
    context: tuple[tuple[str, str], ...] = ()
    evidence_refs: tuple[str, ...] = ()
    hypothesis: str = ""
    progress_key: str = ""
    recommended_strategy_id: str | None = None
    quality_result: QualityResult | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("ifr_diagnosis_schema_unsupported")
        if (not isinstance(self.failure_kind, FailureKind)
                or not isinstance(self.recoverability, Recoverability)
                or not isinstance(self.effect_state, EffectState)
                or (self.quality_result is not None
                    and not isinstance(self.quality_result, QualityResult))):
            raise ValueError("ifr_diagnosis_enum_invalid")
        if (type(self.hypothesis) is not str or len(self.hypothesis) > 2048
                or type(self.progress_key) is not str or len(self.progress_key) > 256):
            raise ValueError("ifr_diagnosis_text_invalid")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.failure_code):
            raise ValueError("ifr_failure_code_invalid")
        if (type(self.context) is not tuple
                or any(type(item) is not tuple or len(item) != 2 for item in self.context)):
            raise ValueError("ifr_context_invalid")
        if tuple(sorted(self.context)) != self.context or len({key for key, _ in self.context}) != len(self.context):
            raise ValueError("ifr_context_must_be_sorted_and_unique")
        if any(not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", key) or type(value) is not str
               or len(value) > 256 for key, value in self.context):
            raise ValueError("ifr_context_invalid")
        if any(type(value) is not str or not value.startswith("sha256:") or len(value) != 71
               for value in self.evidence_refs):
            raise ValueError("ifr_evidence_reference_invalid")
        if self.recommended_strategy_id is not None and not re.fullmatch(
                r"[a-z][a-z0-9_.-]{0,63}", self.recommended_strategy_id):
            raise ValueError("ifr_strategy_id_invalid")

    @property
    def failure_signature(self) -> str:
        # Deliberately include failure context: equal message/code alone does not
        # establish an equal cause across adapters, phases, or environments.
        canonical = json.dumps({
            "kind": self.failure_kind.value,
            "code": self.failure_code,
            "context": self.context,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecoveryLimits:
    max_attempts_total: int = 3
    max_equivalent_repeats: int = 0
    max_duration_seconds: float = 3600
    max_estimated_budget: float = 0

    def __post_init__(self) -> None:
        if (type(self.max_attempts_total) is not int or self.max_attempts_total < 1
                or type(self.max_equivalent_repeats) is not int or self.max_equivalent_repeats < 0
                or type(self.max_duration_seconds) not in (int, float)
                or not math.isfinite(self.max_duration_seconds) or self.max_duration_seconds <= 0
                or type(self.max_estimated_budget) not in (int, float)
                or not math.isfinite(self.max_estimated_budget) or self.max_estimated_budget < 0):
            raise ValueError("ifr_limits_invalid")


@dataclass(frozen=True)
class PriorFailure:
    failure_signature: str
    strategy_id: str
    progress_key: str

    def __post_init__(self) -> None:
        if (type(self.failure_signature) is not str or not self.failure_signature.startswith("sha256:")
                or len(self.failure_signature) != 71
                or type(self.strategy_id) is not str
                or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.strategy_id)
                or type(self.progress_key) is not str or len(self.progress_key) > 256):
            raise ValueError("ifr_prior_failure_invalid")


@dataclass(frozen=True)
class RecoveryContext:
    diagnosis: FailureDiagnosis
    current_strategy_id: str
    recommended_strategy_id: str | None
    authorized_strategy_ids: tuple[str, ...]
    attempts_total: int
    elapsed_seconds: float
    consumed_budget: float
    grant_valid: bool
    prior_failures: tuple[PriorFailure, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.diagnosis, FailureDiagnosis):
            raise ValueError("ifr_context_diagnosis_invalid")
        for value in (self.current_strategy_id,):
            if type(value) is not str or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", value):
                raise ValueError("ifr_context_strategy_invalid")
        if (self.recommended_strategy_id is not None
                and (type(self.recommended_strategy_id) is not str
                     or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.recommended_strategy_id))):
            raise ValueError("ifr_context_strategy_invalid")
        if (type(self.authorized_strategy_ids) is not tuple
                or any(type(value) is not str
                       or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", value)
                       for value in self.authorized_strategy_ids)
                or len(set(self.authorized_strategy_ids)) != len(self.authorized_strategy_ids)):
            raise ValueError("ifr_context_authorized_strategies_invalid")
        if (type(self.attempts_total) is not int or self.attempts_total < 1
                or type(self.elapsed_seconds) not in (int, float)
                or not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0
                or type(self.consumed_budget) not in (int, float)
                or not math.isfinite(self.consumed_budget) or self.consumed_budget < 0
                or type(self.grant_valid) is not bool
                or type(self.prior_failures) is not tuple
                or any(not isinstance(item, PriorFailure) for item in self.prior_failures)):
            raise ValueError("ifr_context_limits_invalid")


@dataclass(frozen=True)
class RecoveryDecision:
    retry: bool
    necessary: bool
    useful: bool
    authorized: bool
    safe: bool
    strategy_id: str | None
    reason_code: str
    failure_signature: str


class IntelligentFailureRecoveryPolicy:
    """Pure decision policy. It cannot grant permissions or dispatch work."""

    def __init__(self, limits: RecoveryLimits | None = None):
        self.limits = limits or RecoveryLimits()

    def decide(self, context: RecoveryContext) -> RecoveryDecision:
        diagnosis = context.diagnosis
        signature = diagnosis.failure_signature
        candidate = context.recommended_strategy_id or diagnosis.recommended_strategy_id
        necessary = diagnosis.quality_result is not QualityResult.PASS
        useful = diagnosis.recoverability is Recoverability.POTENTIALLY_RECOVERABLE
        safe_effect = diagnosis.effect_state in {
            EffectState.NO_EFFECT_PROVEN, EffectState.RECONCILED_SAFE,
        }
        authorized = context.grant_valid and candidate is not None and candidate in context.authorized_strategy_ids
        if not necessary:
            return self._decision(False, context, signature, False, False, authorized, safe_effect,
                                  "quality_already_passed", None)
        if diagnosis.quality_result is QualityResult.INCONCLUSIVE or diagnosis.failure_kind is FailureKind.EVALUATION_INCONCLUSIVE:
            return self._decision(False, context, signature, True, False, authorized, safe_effect,
                                  "evaluation_inconclusive_requires_evidence", None)
        if diagnosis.failure_kind in {FailureKind.OPERATIONAL_UNKNOWN,
                                      FailureKind.SECURITY_OR_AUTHORIZATION_BLOCK}:
            return self._decision(False, context, signature, True, useful, authorized, False,
                                  "unknown_or_security_block", None)
        if not useful:
            return self._decision(False, context, signature, True, False, authorized, safe_effect,
                                  "failure_not_recoverable", None)
        if not safe_effect:
            return self._decision(False, context, signature, True, useful, authorized, False,
                                  "effect_state_not_reconciled", None)
        if not context.grant_valid:
            return self._decision(False, context, signature, True, useful, False, True,
                                  "authorization_unavailable", None)
        if context.attempts_total >= self.limits.max_attempts_total:
            return self._decision(False, context, signature, True, useful, authorized, True,
                                  "attempt_limit_reached", None)
        if context.elapsed_seconds >= self.limits.max_duration_seconds:
            return self._decision(False, context, signature, True, useful, authorized, True,
                                  "duration_limit_reached", None)
        if self.limits.max_estimated_budget and context.consumed_budget >= self.limits.max_estimated_budget:
            return self._decision(False, context, signature, True, useful, authorized, True,
                                  "budget_limit_reached", None)
        if candidate is None or candidate not in context.authorized_strategy_ids:
            return self._decision(False, context, signature, True, useful, False, True,
                                  "no_authorized_recovery_strategy", None)
        if candidate == context.current_strategy_id:
            equivalent = sum(1 for prior in context.prior_failures
                             if prior.failure_signature == signature
                             and prior.strategy_id == candidate
                             and prior.progress_key == diagnosis.progress_key)
            if equivalent >= self.limits.max_equivalent_repeats:
                return self._decision(False, context, signature, True, False, True, True,
                                      "equivalent_retry_without_progress", None)
        if diagnosis.failure_kind is FailureKind.DETERMINISTIC and candidate == context.current_strategy_id:
            return self._decision(False, context, signature, True, False, True, True,
                                  "deterministic_failure_requires_new_strategy", None)
        return self._decision(True, context, signature, True, True, True, True,
                              "safe_retry_authorized", candidate)

    @staticmethod
    def _decision(retry, context, signature, necessary, useful, authorized, safe, reason, strategy):
        return RecoveryDecision(bool(retry), necessary, useful, authorized, safe, strategy,
                                reason, signature)
