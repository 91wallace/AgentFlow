"""Pure execution limits and financial policy values; no executor or store IO."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re


class FinancialBudgetMode(str, Enum):
    """Financial policy; an estimate is never a provider-enforced hard cap."""

    HARD_CAP_REQUIRED = "hard_cap_required"
    ESTIMATED_COST_ACCEPTED = "estimated_cost_accepted"


@dataclass(frozen=True)
class FinancialBudgetPolicy:
    mode: FinancialBudgetMode
    estimated_amount: float | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, FinancialBudgetMode):
            raise ValueError("financial_budget_mode_invalid")
        if self.mode is FinancialBudgetMode.HARD_CAP_REQUIRED:
            if self.estimated_amount is not None or self.currency is not None:
                raise ValueError("hard_cap_policy_cannot_claim_estimate_as_enforcement")
            return
        if (type(self.estimated_amount) not in (int, float)
                or not math.isfinite(self.estimated_amount) or self.estimated_amount <= 0
                or type(self.currency) is not str
                or not re.fullmatch(r"[A-Z]{3}", self.currency)):
            raise ValueError("explicit_finite_cost_estimate_required")


@dataclass(frozen=True)
class ExecutionLimits:
    max_jobs: int
    max_attempts_per_job: int
    max_seconds: float
    max_estimated_budget: float = 0.0
    max_iterations: int = 100
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        if (type(self.max_jobs) is not int or self.max_jobs < 1
                or type(self.max_attempts_per_job) is not int or self.max_attempts_per_job < 1
                or type(self.max_seconds) not in (float, int) or not math.isfinite(self.max_seconds)
                or self.max_seconds <= 0
                or type(self.max_estimated_budget) not in (float, int)
                or not math.isfinite(self.max_estimated_budget) or self.max_estimated_budget < 0
                or type(self.max_iterations) is not int or self.max_iterations < 1
                or type(self.max_concurrency) is not int or self.max_concurrency != 1):
            raise ValueError("execution_limits_invalid")


@dataclass(frozen=True)
class TaskExecutionLimits:
    max_attempts: int
    max_seconds: float
    max_estimated_budget: float = 0.0

    def __post_init__(self) -> None:
        if (type(self.max_attempts) is not int or self.max_attempts < 1
                or type(self.max_seconds) not in (int, float) or not math.isfinite(self.max_seconds)
                or self.max_seconds <= 0
                or type(self.max_estimated_budget) not in (int, float)
                or not math.isfinite(self.max_estimated_budget) or self.max_estimated_budget < 0):
            raise ValueError("task_execution_limits_invalid")
