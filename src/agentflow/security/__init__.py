"""Security profile selection and non-authorizing consent contracts.

Public exports load their owning module only when requested.
"""

from importlib import import_module as _import_module

__all__ = [
    "ConsentDecision", "ConsentLimits", "ExecutionPlanConsentBinding", "LocalConsentCheck",
    "LocalConsentInterface", "LocalConsentReceipt", "LocalConsentRequest",
    "LocalExecutionConsentRequest", "ProfileConfiguration",
    "ProfileConfigurationError", "ProfileDiagnostic", "SecurityProfile",
    "UnavailableLocalConsent", "inspect_consent_receipt", "load_profile_configuration",
    "profile_diagnostic", "save_profile_configuration",
    "AuthenticatedConsumer", "AuthenticatedOperator", "Ed25519LocalGrantSigner", "Ed25519LocalGrantVerifier",
    "LocalAuthorizationAuthority", "LocalAuthorizationError", "LocalAuthorizationOutcome",
    "LocalAuthorizationPolicy", "LocalAuthorityPolicy", "LocalConsentAuditRecord",
    "LocalConsumerAuthenticator", "LocalGrant", "LocalGrantSigner", "LocalGrantStatus",
    "LocalGrantVerifier", "LocalOperatorAuthenticator", "LocalProposalResolver",
    "LocalRevocationRequest", "ResolvedLocalProposal", "UnavailableLocalAuthorizationAuthority",
    "UnavailableLocalConsumerAuthenticator", "UnavailableLocalOperatorAuthenticator",
    "UnavailableLocalProposalResolver", "local_consent_challenge_digest",
    "ConsentReviewError", "LocalConsentReviewService", "TerminalConsentReview",
    "UnavailableLocalConsentProofProvider", "format_consent_request",
    "review_authentication_challenge",
    "AuthoritativeConsentError", "AuthoritativeConsentProposal",
    "AuthoritativeConsentResolver",
    "LocalAuthenticationCapability", "LocalAuthenticationChallenge",
    "SQLiteLocalAuthenticationChallengeStore", "inspect_local_authentication",
    "LocalPassphraseError", "LocalPassphraseGrantSigner", "LocalPassphraseProof",
    "LocalPassphraseProofProvider", "LocalPassphraseSigningSession",
    "PassphraseLocalOperatorAuthenticator", "SQLiteLocalOperatorCredentialStore",
    "current_local_operator_principal", "enroll_local_operator_interactive",
    "revoke_local_operator_interactive",
    "LocalConsentConfigurationError", "LocalConsentRuntimeConfiguration",
    "load_local_consent_configuration", "make_execution_binding_resolver",
    "prepare_execution_plan_consent_request", "review_accepted_execution_plan",
    "review_persisted_handoff",
    "LocalWebAuthnAssertionProof", "WindowsHelloCredentialEnrollment",
    "WindowsWebAuthnApi", "WindowsWebAuthnError",
    "WindowsWebAuthnOperatorAuthenticator", "WindowsWebAuthnProofProvider",
    "windows_operator_principal",
]


def __getattr__(name):
    try:
        if name in (
            'ConsentDecision', 'ConsentLimits', 'ExecutionPlanConsentBinding',
            'LocalConsentCheck', 'LocalConsentInterface', 'LocalConsentReceipt',
            'LocalConsentRequest', 'LocalExecutionConsentRequest', 'ProfileConfiguration',
            'ProfileConfigurationError', 'ProfileDiagnostic', 'SecurityProfile',
            'UnavailableLocalConsent', 'inspect_consent_receipt', 'load_profile_configuration',
            'profile_diagnostic', 'save_profile_configuration',
        ):
            module = _import_module("agentflow.security.profile")
        elif name in (
            'AuthenticatedConsumer', 'AuthenticatedOperator', 'Ed25519LocalGrantSigner',
            'Ed25519LocalGrantVerifier', 'LocalAuthorizationAuthority', 'LocalAuthorizationError',
            'LocalAuthorizationOutcome', 'LocalAuthorizationPolicy', 'LocalAuthorityPolicy',
            'LocalConsentAuditRecord', 'LocalConsumerAuthenticator', 'LocalGrant',
            'LocalGrantSigner', 'LocalGrantStatus', 'LocalGrantVerifier',
            'LocalOperatorAuthenticator', 'LocalProposalResolver', 'LocalRevocationRequest',
            'ResolvedLocalProposal', 'UnavailableLocalAuthorizationAuthority', 'UnavailableLocalConsumerAuthenticator',
            'UnavailableLocalOperatorAuthenticator', 'UnavailableLocalProposalResolver', 'local_consent_challenge_digest',
        ):
            module = _import_module("agentflow.security.authorization")
        elif name in (
            'ConsentReviewError', 'LocalConsentReviewService', 'TerminalConsentReview',
            'UnavailableLocalConsentProofProvider', 'format_consent_request', 'review_authentication_challenge',
        ):
            module = _import_module("agentflow.security.consent_ui")
        elif name in (
            'AuthoritativeConsentError', 'AuthoritativeConsentProposal', 'AuthoritativeConsentResolver',
        ):
            module = _import_module("agentflow.security.authoritative_consent")
        elif name in (
            'LocalAuthenticationCapability', 'LocalAuthenticationChallenge', 'SQLiteLocalAuthenticationChallengeStore',
            'inspect_local_authentication',
        ):
            module = _import_module("agentflow.security.local_authentication")
        elif name in (
            'LocalPassphraseError', 'LocalPassphraseGrantSigner', 'LocalPassphraseProof',
            'LocalPassphraseProofProvider', 'LocalPassphraseSigningSession', 'PassphraseLocalOperatorAuthenticator',
            'SQLiteLocalOperatorCredentialStore', 'current_local_operator_principal', 'enroll_local_operator_interactive',
            'revoke_local_operator_interactive',
        ):
            module = _import_module("agentflow.security.local_passphrase")
        elif name in (
            'LocalConsentConfigurationError', 'LocalConsentRuntimeConfiguration', 'load_local_consent_configuration',
            'make_execution_binding_resolver', 'prepare_execution_plan_consent_request', 'review_accepted_execution_plan',
            'review_persisted_handoff',
        ):
            module = _import_module("agentflow.security.local_consent_review")
        elif name in (
            'LocalWebAuthnAssertionProof', 'WindowsHelloCredentialEnrollment', 'WindowsWebAuthnApi',
            'WindowsWebAuthnError', 'WindowsWebAuthnOperatorAuthenticator', 'WindowsWebAuthnProofProvider',
            'windows_operator_principal',
        ):
            module = _import_module("agentflow.security.windows_webauthn")
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
