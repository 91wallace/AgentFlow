"""Automatic fallback execution governed by GlobalConfig fallback_policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .global_config import GlobalConfig, RoleModelConfig, load_global_config

RECOVERABLE_MARKERS = (
    "timeout",
    "timed out",
    "rate_limit",
    "rate limit",
    "429",
    "503",
    "unavailable",
    "connection",
    "temporary",
)

NON_RECOVERABLE_MARKERS = (
    "invalid_request",
    "permission_denied",
    "unauthorized",
    "auth_invalid",
    "cancelled",
)


def classify_error(error: BaseException | str) -> tuple[str, bool]:
    """Return (category, is_recoverable) for an executor error."""
    text = str(error).lower()
    for marker in NON_RECOVERABLE_MARKERS:
        if marker in text:
            return marker, False
    for marker in RECOVERABLE_MARKERS:
        if marker in text:
            return marker, True
    return "unknown_error", False


@dataclass
class FallbackAttempt:
    agent: str
    provider: str
    model: str
    status: str
    reason: str = ""


@dataclass
class FallbackResult:
    succeeded: bool
    result: Any = None
    selected: RoleModelConfig | None = None
    attempts: list[FallbackAttempt] = field(default_factory=list)
    error: str = ""


def execute_with_fallback(
    primary: RoleModelConfig,
    runner: Callable[[RoleModelConfig, str], Any],
    *,
    task_id: str,
    idempotency_key: str,
    availability_check: Callable[[RoleModelConfig], bool] | None = None,
    config: GlobalConfig | None = None,
    max_attempts: int = 3,
) -> FallbackResult:
    """Execute a task with policy-governed fallback, never falling back silently."""
    cfg = config or load_global_config()
    policy = getattr(cfg, "fallback_policy", "none") if cfg is not None else "none"
    fallback_models: list[RoleModelConfig] = list(getattr(cfg, "fallback_models", []) or []) if cfg is not None else []

    candidates: list[RoleModelConfig] = [primary]
    if policy not in ("none", "disabled", "") and fallback_models:
        for item in fallback_models:
            if isinstance(item, RoleModelConfig):
                candidates.append(item)
            elif isinstance(item, str) and item.strip():
                candidates.append(RoleModelConfig(agent=item.strip()))

    attempts: list[FallbackAttempt] = []
    executed_keys: set[str] = set()

    for idx, candidate in enumerate(candidates[: max(1, max_attempts)]):
        if availability_check is not None and not availability_check(candidate):
            attempts.append(
                FallbackAttempt(
                    agent=candidate.agent,
                    provider=candidate.provider,
                    model=candidate.model,
                    status="skipped",
                    reason="unavailable_or_unauthenticated",
                )
            )
            continue

        dedup_key = f"{idempotency_key}:{candidate.agent}:{candidate.provider}:{candidate.model}"
        if dedup_key in executed_keys:
            continue
        executed_keys.add(dedup_key)

        try:
            res = runner(candidate, idempotency_key)
            attempts.append(
                FallbackAttempt(
                    agent=candidate.agent,
                    provider=candidate.provider,
                    model=candidate.model,
                    status="succeeded",
                    reason="primary" if idx == 0 else "fallback_applied",
                )
            )
            return FallbackResult(succeeded=True, result=res, selected=candidate, attempts=attempts)
        except Exception as exc:
            category, recoverable = classify_error(exc)
            attempts.append(
                FallbackAttempt(
                    agent=candidate.agent,
                    provider=candidate.provider,
                    model=candidate.model,
                    status="failed",
                    reason=f"{category}: {exc}",
                )
            )
            if not recoverable or policy in ("none", "disabled", ""):
                return FallbackResult(succeeded=False, attempts=attempts, error=str(exc))

    last_err = attempts[-1].reason if attempts else "no_available_executor"
    return FallbackResult(succeeded=False, attempts=attempts, error=last_err)
