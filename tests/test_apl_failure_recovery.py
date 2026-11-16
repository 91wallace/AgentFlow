from dataclasses import replace

import pytest

from agentflow.apl.failure_recovery import (
    EffectState, FailureDiagnosis, FailureKind, IntelligentFailureRecoveryPolicy,
    PriorFailure, QualityResult, Recoverability, RecoveryContext, RecoveryLimits,
)


def _diagnosis(*, kind=FailureKind.TRANSIENT,
               recoverability=Recoverability.POTENTIALLY_RECOVERABLE,
               effects=EffectState.NO_EFFECT_PROVEN, code="temporary_failure",
               strategy="alternate", quality=None, progress="unchanged"):
    return FailureDiagnosis(kind, recoverability, effects, code,
                            context=(("adapter", "synthetic"),),
                            hypothesis="synthetic diagnosis", progress_key=progress,
                            recommended_strategy_id=strategy, quality_result=quality)


def _context(diagnosis, **changes):
    base = RecoveryContext(diagnosis, "default", diagnosis.recommended_strategy_id,
                           ("default", "alternate"), 1, 1.0, 0.0, True)
    return replace(base, **changes)


def test_safe_transient_failure_can_select_authorized_alternative():
    decision = IntelligentFailureRecoveryPolicy().decide(_context(_diagnosis()))
    assert (decision.retry, decision.strategy_id, decision.reason_code) == (
        True, "alternate", "safe_retry_authorized")
    assert decision.necessary and decision.useful and decision.authorized and decision.safe


def test_deterministic_failure_requires_strategy_change():
    policy = IntelligentFailureRecoveryPolicy()
    same = _diagnosis(kind=FailureKind.DETERMINISTIC, strategy="default")
    changed = _diagnosis(kind=FailureKind.DETERMINISTIC, strategy="alternate")
    assert policy.decide(_context(same)).reason_code == "equivalent_retry_without_progress"
    assert policy.decide(_context(changed)).retry is True


@pytest.mark.parametrize("kind", [FailureKind.ENVIRONMENT, FailureKind.TRANSIENT])
def test_effects_possible_or_unknown_never_retry(kind):
    diagnosis = _diagnosis(kind=kind, effects=EffectState.EFFECTS_POSSIBLE)
    decision = IntelligentFailureRecoveryPolicy().decide(_context(diagnosis))
    assert decision.retry is False
    assert decision.reason_code == "effect_state_not_reconciled"


@pytest.mark.parametrize("kind,effect", [
    (FailureKind.OPERATIONAL_UNKNOWN, EffectState.UNKNOWN),
    (FailureKind.SECURITY_OR_AUTHORIZATION_BLOCK, EffectState.NO_EFFECT_PROVEN),
])
def test_unknown_and_authorization_block_are_never_retried(kind, effect):
    diagnosis = _diagnosis(kind=kind, effects=effect)
    decision = IntelligentFailureRecoveryPolicy().decide(_context(diagnosis))
    assert decision.retry is False
    assert decision.safe is False


def test_inconclusive_is_not_promoted_to_fail_or_retry():
    diagnosis = _diagnosis(kind=FailureKind.EVALUATION_INCONCLUSIVE,
                           effects=EffectState.EFFECTS_POSSIBLE,
                           quality=QualityResult.INCONCLUSIVE)
    decision = IntelligentFailureRecoveryPolicy().decide(_context(diagnosis))
    assert diagnosis.quality_result is QualityResult.INCONCLUSIVE
    assert decision.retry is False
    assert decision.reason_code == "evaluation_inconclusive_requires_evidence"


def test_quality_pass_never_retries():
    diagnosis = _diagnosis(quality=QualityResult.PASS)
    decision = IntelligentFailureRecoveryPolicy().decide(_context(diagnosis))
    assert decision.retry is False
    assert decision.reason_code == "quality_already_passed"


@pytest.mark.parametrize("changes,reason", [
    ({"attempts_total": 3}, "attempt_limit_reached"),
    ({"elapsed_seconds": 10.0}, "duration_limit_reached"),
    ({"consumed_budget": 5.0}, "budget_limit_reached"),
    ({"grant_valid": False}, "authorization_unavailable"),
    ({"authorized_strategy_ids": ("default",)}, "no_authorized_recovery_strategy"),
])
def test_limits_authorization_and_strategy_scope_fail_closed(changes, reason):
    limits = RecoveryLimits(max_attempts_total=3, max_equivalent_repeats=1,
                            max_duration_seconds=10, max_estimated_budget=5)
    decision = IntelligentFailureRecoveryPolicy(limits).decide(
        _context(_diagnosis(), **changes))
    assert decision.retry is False
    assert decision.reason_code == reason


def test_equivalent_failure_and_strategy_without_progress_is_detected():
    diagnosis = _diagnosis(strategy="default")
    prior = PriorFailure(diagnosis.failure_signature, "default", diagnosis.progress_key)
    policy = IntelligentFailureRecoveryPolicy(RecoveryLimits(max_equivalent_repeats=1))
    decision = policy.decide(_context(diagnosis, prior_failures=(prior,)))
    assert decision.retry is False
    assert decision.reason_code == "equivalent_retry_without_progress"


def test_failure_signature_includes_normalized_context():
    first = _diagnosis()
    distinct = _diagnosis()
    distinct = replace(distinct, context=(("adapter", "different"),))
    assert first.failure_signature != distinct.failure_signature

