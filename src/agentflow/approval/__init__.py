"""Approval authority contracts. No real approval backend is enabled.

Public exports load their owning module only when requested.
"""

from importlib import import_module as _import_module

__all__ = ["ApprovalAuthority", "ApprovalError", "ApprovalRequest", "ApprovalStatus",
           "GrantReference", "UnavailableApprovalAuthority", "ApprovalBroker",
           "verify_and_consume", "AcceptedSnapshot", "SnapshotProposal", "OperationIntent",
           "PendingProposalSubmission", "PendingProposalQuery", "PendingProposalSnapshot",
           "PendingProposalCancellation", "PendingProposalInvalidation", "ProposalLifecycleSnapshot",
           "BrokerApprovalRequest", "ApprovalDecision", "ExecutionAuthorization",
           "ExecutionReceipt", "ProtocolError", "canonical_json", "digest_json",
           "parse_canonical_json", "ClientIdentity", "ClientCredential", "ClientPolicy",
           "ClientAuthorizationDecision", "certificate_fingerprint", "evaluate_client_operation",
           "new_operation_nonce",
           "BrokerEnvelope", "BrokerTransportError", "LoopbackTLSClient",
           "authenticate_tls_peer", "create_broker_server_context"]
__all__ += ["WebAuthnAssertion", "WebAuthnChallenge", "WebAuthnChallengeBinding",
            "WebAuthnCredential", "WebAuthnError", "WebAuthnVerificationError",
            "WebAuthnVerificationResult", "b64url_decode", "b64url_encode",
            "build_credential", "verify_assertion_signature"]
__all__ += ["ConsentPresentation", "PresentationError", "PresentedSnapshot",
            "build_presentation_readonly", "render_consent_preview", "render_readonly_preview"]
__all__ += ["BrokerServiceError", "LocalBrokerService"]
__all__ += ["ApproverDeviceConfig", "AuthorityPreparationError",
            "AuthorityProvisioningManifest", "GrantBindingReport", "PeerPinCheck",
            "ServiceDomainConfig", "inspect_grant_binding", "verify_tls_peer_pin"]


def __getattr__(name):
    try:
        if name in (
            'ApprovalAuthority', 'ApprovalError', 'ApprovalRequest',
            'ApprovalStatus', 'GrantReference', 'UnavailableApprovalAuthority',
            'verify_and_consume',
        ):
            module = _import_module("agentflow.approval.contracts")
        elif name in (
            'ApprovalBroker',
        ):
            module = _import_module("agentflow.approval.broker")
        elif name in (
            'ApproverDeviceConfig', 'AuthorityPreparationError', 'AuthorityProvisioningManifest',
            'GrantBindingReport', 'PeerPinCheck', 'ServiceDomainConfig',
            'inspect_grant_binding', 'verify_tls_peer_pin',
        ):
            module = _import_module("agentflow.approval.preparation")
        elif name in (
            'AcceptedSnapshot', 'ApprovalDecision', 'BrokerApprovalRequest',
            'ExecutionAuthorization', 'ExecutionReceipt', 'OperationIntent',
            'ProtocolError', 'PendingProposalCancellation', 'PendingProposalInvalidation',
            'PendingProposalQuery', 'PendingProposalSnapshot', 'PendingProposalSubmission',
            'ProposalLifecycleSnapshot', 'SnapshotProposal', 'canonical_json',
            'digest_json', 'parse_canonical_json',
        ):
            module = _import_module("agentflow.approval.protocol")
        elif name in (
            'ClientAuthorizationDecision', 'ClientCredential', 'ClientIdentity',
            'ClientPolicy', 'certificate_fingerprint', 'evaluate_client_operation',
            'new_operation_nonce',
        ):
            module = _import_module("agentflow.approval.identity")
        elif name in (
            'BrokerEnvelope', 'BrokerTransportError', 'LoopbackTLSClient',
            'authenticate_tls_peer', 'create_broker_server_context',
        ):
            module = _import_module("agentflow.approval.transport")
        elif name in (
            'WebAuthnAssertion', 'WebAuthnChallenge', 'WebAuthnChallengeBinding',
            'WebAuthnCredential', 'WebAuthnError', 'WebAuthnVerificationError',
            'WebAuthnVerificationResult', 'b64url_decode', 'b64url_encode',
            'build_credential', 'verify_assertion_signature',
        ):
            module = _import_module("agentflow.approval.webauthn")
        elif name in (
            'ConsentPresentation', 'PresentationError', 'PresentedSnapshot',
            'build_presentation_readonly', 'render_consent_preview', 'render_readonly_preview',
        ):
            module = _import_module("agentflow.approval.presentation")
        elif name in (
            'BrokerServiceError', 'LocalBrokerService',
        ):
            module = _import_module("agentflow.approval.service")
        else:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    except ModuleNotFoundError as exc:
        if exc.name == "cryptography" or (exc.name or "").startswith("cryptography."):
            raise ImportError(
                f"{__name__}.{name} requires cryptography; install the cryptography dependency "
                "to use this advanced API."
            ) from exc
        raise
    source_name = {'BrokerApprovalRequest': 'ApprovalRequest'}.get(name, name)
    value = getattr(module, source_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
