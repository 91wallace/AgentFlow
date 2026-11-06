"""Fail-closed Approval Broker boundary.

This package intentionally contains no production issuer, IPC server, WebAuthn verifier,
credential store, service installer, or consent UI. No runtime flag or environment value
can activate grants. A separately homologated service can implement ApprovalAuthority in
a later approved change; until then every operation is unavailable.
"""

from .contracts import ApprovalError, ApprovalRequest, ApprovalStatus, GrantReference


class ApprovalBroker:
    """AgentFlow-side broker facade; never issues grants and cannot be locally enabled."""

    def status(self) -> ApprovalStatus:
        return ApprovalStatus(False, "broker_trust_boundary_not_homologated")

    def verify(self, request: ApprovalRequest, reference: GrantReference) -> bool:
        return False

    def consume(self, request: ApprovalRequest, reference: GrantReference) -> str:
        raise ApprovalError("approval_unavailable: broker trust boundary is not homologated")
