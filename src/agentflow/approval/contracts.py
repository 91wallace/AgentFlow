"""Narrow, provider-independent approval boundary; it never issues grants."""

from dataclasses import dataclass
from typing import Protocol


class ApprovalError(RuntimeError):
    """Approval is unavailable, invalid, expired, or already consumed."""


@dataclass(frozen=True)
class ApprovalRequest:
    operation_id: str
    project_id: str
    action: str
    scope_digest: str
    specification_revision_id: str
    specification_digest: str
    plan_revision_id: str
    plan_digest: str
    impact_digest: str


@dataclass(frozen=True)
class GrantReference:
    grant_id: str
    request_digest: str


@dataclass(frozen=True)
class ApprovalStatus:
    available: bool
    reason: str
    verified_capabilities: tuple[str, ...] = ()


class ApprovalAuthority(Protocol):
    """AgentFlow can ask/consume; only an independent broker can issue grants."""

    def status(self) -> ApprovalStatus: ...
    def verify(self, request: ApprovalRequest, reference: GrantReference) -> bool: ...
    def consume(self, request: ApprovalRequest, reference: GrantReference) -> str: ...


class UnavailableApprovalAuthority:
    """Production default until a broker trust boundary is independently homologated."""

    def status(self) -> ApprovalStatus:
        return ApprovalStatus(False, "approval_broker_not_homologated")

    def verify(self, request: ApprovalRequest, reference: GrantReference) -> bool:
        return False

    def consume(self, request: ApprovalRequest, reference: GrantReference) -> str:
        raise ApprovalError("approval_unavailable: broker trust boundary is not homologated")


def verify_and_consume(authority: ApprovalAuthority, request: ApprovalRequest,
                       reference: GrantReference) -> str:
    """Consume only through the authority; this helper cannot issue or persist a grant."""
    status = authority.status()
    if not status.available:
        raise ApprovalError(f"approval_unavailable: {status.reason}")
    if not authority.verify(request, reference):
        raise ApprovalError("grant inválido, expirado, divergente ou já consumido")
    return authority.consume(request, reference)
