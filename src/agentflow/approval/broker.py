"""Fail-closed Approval Broker boundary.

This facade intentionally contains no production issuer, IPC server, service installer,
or operational consent UI. Local WebAuthn and presentation modules are demonstrative only
and cannot activate grants. No runtime flag or environment value enables protected
operations; until a separate trust boundary is homologated every operation is unavailable.
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
