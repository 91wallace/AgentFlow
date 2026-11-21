"""Native, reusable Autonomous Planning Loop for AgentFlow.

Public exports load their owning module only when requested.
"""

from importlib import import_module as _import_module

__all__ = ["APLState", "APLLimits", "CycleSnapshot", "Evaluation",
           "ExecutionOutcome", "JobProposal", "Planner", "Executor",
           "Evaluator", "Policy", "CycleStore", "APLController",
           "SQLiteCycleStore", "SupervisedCycleService", "EvidenceReference",
           "PlanProposal", "PlannerAdapter", "ReplanContext", "RemainingLimits",
           "TaskChange", "ValidatedResult", "DeterministicPlannerAdapter",
           "PlannerUnavailable", "UnavailableSemanticPlanner"]
__all__ += ["BrokerHandoffError", "BrokerHandoffService"]
__all__ += ["validate_proposal", "proposal_digest"]
__all__ += ["proposal_from_mapping"]
__all__ += ["CapabilityProbe", "IsolationControl", "IsolationEvidence", "IsolationPolicy",
            "RestrictedInferenceHarness", "RestrictedWorkerSpec", "RestrictedWorkerUnavailable",
            "TerminationStatus", "WorkerLaunchResult", "WorkerStatus", "WorkerTerminationResult",
            "decode_worker_proposal"]
__all__ += ["AuthorizedExecutionController", "AuthorizedExecutionError", "ExecutionLimits",
            "TaskExecutionLimits",
            "NoEffectExecutionError", "SQLiteAuthorizedExecutionStore", "VerifiedExecutionGrant",
            "BrokerExecutionGrantResolver", "FinancialBudgetMode", "FinancialBudgetPolicy"]
__all__ += ["LocalExecutionContract", "LocalExecutionContractResolver",
            "LocalAuthorizedExecutionService"]
__all__ += ["CodexAcceptanceEvidenceValidator", "CodexAuthorizedJobExecutor",
            "CodexExecutionError", "PinnedWorkspace", "AcceptanceEvaluation",
            "PlanGateAcceptanceVerifier"]
__all__ += ["CodexCLIPlannerAdapter"]
__all__ += ["EffectState", "FailureDiagnosis", "FailureKind",
            "IntelligentFailureRecoveryPolicy", "PriorFailure", "QualityResult",
            "Recoverability", "RecoveryContext", "RecoveryDecision", "RecoveryLimits"]


def __getattr__(name):
    try:
        if name in (
            'APLState', 'APLLimits', 'CycleSnapshot',
            'Evaluation', 'ExecutionOutcome', 'JobProposal',
            'Planner', 'Executor', 'Evaluator',
            'Policy', 'CycleStore', 'EvidenceReference',
            'PlanProposal', 'PlannerAdapter', 'ReplanContext',
            'RemainingLimits', 'TaskChange', 'ValidatedResult',
        ):
            module = _import_module("agentflow.apl.contracts")
        elif name in (
            'APLController',
        ):
            module = _import_module("agentflow.apl.controller")
        elif name in (
            'SQLiteCycleStore',
        ):
            module = _import_module("agentflow.apl.persistence")
        elif name in (
            'SupervisedCycleService',
        ):
            module = _import_module("agentflow.apl.supervised")
        elif name in (
            'BrokerHandoffError', 'BrokerHandoffService',
        ):
            module = _import_module("agentflow.apl.broker_handoff")
        elif name in (
            'DeterministicPlannerAdapter', 'PlannerUnavailable', 'UnavailableSemanticPlanner',
            'validate_proposal', 'proposal_digest', 'proposal_from_mapping',
        ):
            module = _import_module("agentflow.apl.replanning")
        elif name in (
            'CapabilityProbe', 'IsolationControl', 'IsolationEvidence',
            'IsolationPolicy', 'RestrictedInferenceHarness', 'RestrictedWorkerSpec',
            'RestrictedWorkerUnavailable', 'TerminationStatus', 'WorkerLaunchResult',
            'WorkerStatus', 'WorkerTerminationResult', 'decode_worker_proposal',
        ):
            module = _import_module("agentflow.apl.restricted_worker")
        elif name in (
            'AuthorizedExecutionController', 'AuthorizedExecutionError',
            'NoEffectExecutionError', 'SQLiteAuthorizedExecutionStore',
            'VerifiedExecutionGrant', 'BrokerExecutionGrantResolver',
            'LocalExecutionContract', 'LocalExecutionContractResolver', 'LocalAuthorizedExecutionService',
        ):
            module = _import_module("agentflow.apl.authorized_execution")
        elif name in (
            'ExecutionLimits', 'TaskExecutionLimits',
            'FinancialBudgetMode', 'FinancialBudgetPolicy',
        ):
            module = _import_module("agentflow.apl.execution_contracts")
        elif name in (
            'AcceptanceEvaluation', 'CodexAcceptanceEvidenceValidator', 'CodexAuthorizedJobExecutor',
            'CodexExecutionError', 'PinnedWorkspace', 'PlanGateAcceptanceVerifier',
        ):
            module = _import_module("agentflow.apl.codex_execution")
        elif name in (
            'CodexCLIPlannerAdapter',
        ):
            module = _import_module("agentflow.apl.codex_planner")
        elif name in (
            'EffectState', 'FailureDiagnosis', 'FailureKind',
            'IntelligentFailureRecoveryPolicy', 'PriorFailure', 'QualityResult',
            'Recoverability', 'RecoveryContext', 'RecoveryDecision',
            'RecoveryLimits',
        ):
            module = _import_module("agentflow.apl.failure_recovery")
        else:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    except ModuleNotFoundError as exc:
        if exc.name == "cryptography" or (exc.name or "").startswith("cryptography."):
            raise ImportError(
                f"{__name__}.{name} requires cryptography; install the cryptography dependency "
                "to use this advanced API."
            ) from exc
        raise
    source_name = name
    value = getattr(module, source_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
