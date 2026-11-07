"""Approval authority contracts. No real approval backend is enabled."""

from .contracts import (ApprovalAuthority, ApprovalError, ApprovalRequest,
                        ApprovalStatus, GrantReference, UnavailableApprovalAuthority)
from .contracts import verify_and_consume
from .broker import ApprovalBroker
from .protocol import (AcceptedSnapshot, ApprovalDecision, ApprovalRequest as BrokerApprovalRequest,
                       ExecutionAuthorization, ExecutionReceipt, OperationIntent, ProtocolError,
                       SnapshotProposal, canonical_json, digest_json, parse_canonical_json)
from .identity import (ClientAuthorizationDecision, ClientCredential, ClientIdentity,
                       ClientPolicy, certificate_fingerprint, evaluate_client_operation,
                       new_operation_nonce)
from .transport import (BrokerEnvelope, BrokerTransportError, LoopbackTLSClient,
                        authenticate_tls_peer, create_broker_server_context)
from .webauthn import (WebAuthnAssertion, WebAuthnChallenge, WebAuthnChallengeBinding,
                       WebAuthnCredential, WebAuthnError, WebAuthnVerificationError,
                       WebAuthnVerificationResult, b64url_decode, b64url_encode,
                       build_credential, verify_assertion_signature)
from .presentation import (ConsentPresentation, PresentationError, PresentedSnapshot,
                           build_presentation_readonly, render_consent_preview,
                           render_readonly_preview)

__all__ = ["ApprovalAuthority", "ApprovalError", "ApprovalRequest", "ApprovalStatus",
           "GrantReference", "UnavailableApprovalAuthority", "ApprovalBroker",
           "verify_and_consume", "AcceptedSnapshot", "SnapshotProposal", "OperationIntent",
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
