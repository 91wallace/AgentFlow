from agentflow.apl.contracts import APLState, CycleSnapshot, JobProposal
from agentflow.apl.execution_identity import (
    attempt_id,
    canonical_json,
    local_plan_digest,
    operation_id,
    sha256_digest,
)


def test_canonical_identity_vectors_remain_stable():
    snapshot = CycleSnapshot(
        "cycle-μ", "objective Ω", ("workspace", "network"), APLState.READY,
        (
            JobProposal("task-a", "write Ω", (), ("workspace",), ("file exists",)),
            JobProposal("task-b", "verify", ("task-a",), ("workspace",), ("passes",)),
        ),
        (), 3, 4.5, plan_revision_id="revision-7",
    )

    plan_digest = "sha256:4b4b59e959ee165e52cf1cc8de771fb8d34e20ce21f2b8a12ce1d6910f579b74"
    assert canonical_json({"z": "Ω", "a": [1, True, None]}) == (
        '{"a":[1,true,null],"z":"Ω"}'
    )
    assert sha256_digest("objective Ω") == (
        "sha256:34b64fb1135d25b4c06dcd6b6542553109dc7adc64708b94e0955715c1c7d969"
    )
    assert local_plan_digest(snapshot) == plan_digest
    assert operation_id("run-7", "task-a", plan_digest) == (
        "apl-op-9f5ce1c2003d9e4c8b63126f7936ddf9"
    )
    assert attempt_id("run-7", "task-a", 2) == (
        "apl-attempt-c2bb947f3623972e84b35b3b7a9b1330"
    )


def test_legacy_private_helpers_remain_the_same_callable_aliases():
    from agentflow.apl import authorized_execution

    assert authorized_execution._canonical is canonical_json
    assert authorized_execution._sha is sha256_digest
    assert authorized_execution._local_plan_digest is local_plan_digest
    assert authorized_execution._operation_id is operation_id
    assert authorized_execution._attempt_id is attempt_id
