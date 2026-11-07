"""Fail-closed trusted execution gateway contracts and local journal."""

from .contracts import (
    AuthorizationConsumption, GatewayAuthorityStatus, GatewayContractError,
    GatewayEffectResult, GatewayExecutionError, GatewayHandlerDescriptor,
    GatewayIdentity, GatewayPreparation, GatewaySubmission, build_contract,
)
from .gateway import (
    GatewayAuthority, GatewayHandler, GatewayHandlerRegistry,
    TrustedExecutionGateway, UnavailableGatewayAuthority,
)
from .protocol import (
    AuthorizationQuery, AuthorizationQueryResult, AuthorizationState,
    AuthorizationStateQuery, ConsumptionConfirmation, ConsumptionConfirmationRequest,
    ConsumptionReservation, ConsumptionReservationRequest, ExecutionBinding,
    GatewayExecutionReceipt, ReconciliationRequest, ReconciliationResult,
    build_message,
)
from .store import (
    GatewayConflict, GatewayIntegrityError, GatewayStore, GatewayStoreError,
    GatewayStoreLocked,
)

__all__ = [
    "AuthorizationConsumption", "GatewayAuthorityStatus", "GatewayContractError",
    "GatewayEffectResult", "GatewayExecutionError", "GatewayHandlerDescriptor",
    "GatewayIdentity", "GatewayPreparation", "GatewaySubmission", "build_contract",
    "GatewayAuthority", "GatewayHandler", "GatewayHandlerRegistry",
    "TrustedExecutionGateway", "UnavailableGatewayAuthority", "GatewayConflict",
    "GatewayIntegrityError", "GatewayStore", "GatewayStoreError", "GatewayStoreLocked",
    "ExecutionBinding", "AuthorizationQuery", "AuthorizationQueryResult",
    "ConsumptionReservationRequest", "ConsumptionReservation",
    "ConsumptionConfirmationRequest", "ConsumptionConfirmation",
    "AuthorizationStateQuery", "AuthorizationState", "ReconciliationRequest",
    "ReconciliationResult", "GatewayExecutionReceipt", "build_message",
]
