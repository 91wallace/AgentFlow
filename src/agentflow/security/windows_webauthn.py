"""Windows WebAuthn ceremonies for LOCAL_SUPERVISED.

The native API displays its own system UI. This module stores only credential
public keys and verifies assertions with AgentFlow's existing WebAuthn verifier.
It does not approve consent, issue grants, or dispatch work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import sys
from typing import Any

from .authorization import AuthenticatedOperator, LocalAuthorizationError
from .profile import LocalConsentReceipt, LocalConsentRequest

RP_ID = "localhost"
ORIGIN = "http://localhost"
_MAX_ATTESTATION = 65536


class WindowsWebAuthnError(RuntimeError):
    """Native Windows WebAuthn ceremony failed closed."""


def _native_error(operation: str, result: int) -> WindowsWebAuthnError:
    code = result & 0xFFFFFFFF
    if code in {0x800704C7, 0x80090036}:
        return WindowsWebAuthnError(f"windows_webauthn_{operation}_cancelled")
    return WindowsWebAuthnError(f"windows_webauthn_{operation}_failed_{code:08x}")


@dataclass(frozen=True)
class LocalWebAuthnAssertionProof:
    assertion: Any = field(repr=False)
    expires_at: str


@dataclass(frozen=True)
class _Binding:
    protocol_version: int
    challenge_id: str
    challenge_digest: str
    approver_principal: str
    rp_id: str
    allowed_origins: tuple[str, ...]
    expires_at: str


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def windows_operator_principal() -> str:
    """Stable opaque principal derived from the current Windows access token SID."""
    if sys.platform != "win32":
        raise WindowsWebAuthnError("windows_platform_required")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise WindowsWebAuthnError("windows_operator_token_unavailable")
    try:
        required = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if not required.value or required.value > 65536:
            raise WindowsWebAuthnError("windows_operator_sid_unavailable")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi.GetTokenInformation(token, 1, buffer, required, ctypes.byref(required)):
            raise WindowsWebAuthnError("windows_operator_sid_unavailable")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise WindowsWebAuthnError("windows_operator_sid_unavailable")
        try:
            sid = sid_text.value
        finally:
            kernel.LocalFree(sid_text)
    finally:
        kernel.CloseHandle(token)
    return "win-sid-" + hashlib.sha256(sid.encode("ascii")).hexdigest()[:32]


def _read_cbor(data: bytes, offset: int = 0, depth: int = 0):
    """Small strict definite-length CBOR reader used only to extract COSE keys."""
    if depth > 16 or offset >= len(data):
        raise WindowsWebAuthnError("webauthn_cbor_invalid")
    initial = data[offset]
    major, additional = initial >> 5, initial & 31
    offset += 1
    if additional < 24:
        length = additional
    elif additional in (24, 25, 26, 27):
        size = 1 << (additional - 24)
        if offset + size > len(data):
            raise WindowsWebAuthnError("webauthn_cbor_truncated")
        length = int.from_bytes(data[offset:offset + size], "big")
        offset += size
        if length < (24 if size == 1 else 256 if size == 2 else 65536 if size == 4 else 4294967296):
            raise WindowsWebAuthnError("webauthn_cbor_noncanonical")
    else:
        raise WindowsWebAuthnError("webauthn_cbor_indefinite_or_reserved")
    if major == 0:
        value = length
    elif major == 1:
        value = -1 - length
    elif major in (2, 3):
        if length > _MAX_ATTESTATION or offset + length > len(data):
            raise WindowsWebAuthnError("webauthn_cbor_length_invalid")
        chunk = data[offset:offset + length]
        value = chunk if major == 2 else chunk.decode("utf-8", "strict")
        offset += length
    elif major in (4, 5):
        if length > 128:
            raise WindowsWebAuthnError("webauthn_cbor_collection_too_large")
        count = length if major == 4 else length * 2
        items = []
        for _ in range(count):
            item, offset = _read_cbor(data, offset, depth + 1)
            items.append(item)
        if major == 4:
            value = items
        else:
            if length * 2 != len(items):
                raise WindowsWebAuthnError("webauthn_cbor_map_invalid")
            value = {}
            for index in range(0, len(items), 2):
                if type(items[index]) not in (str, int, bytes):
                    raise WindowsWebAuthnError("webauthn_cbor_map_key_invalid")
                if items[index] in value:
                    raise WindowsWebAuthnError("webauthn_cbor_duplicate_key")
                value[items[index]] = items[index + 1]
    elif major == 7 and additional in (20, 21, 22):
        value = {20: False, 21: True, 22: None}[additional]
    else:
        raise WindowsWebAuthnError("webauthn_cbor_type_unsupported")
    return value, offset


def _public_key_from_cose(cose: dict[int, Any]) -> tuple[int, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    algorithm = cose.get(3)
    if algorithm == -7 and cose.get(1) == 2 and cose.get(-1) == 1:
        x, y = cose.get(-2), cose.get(-3)
        if type(x) is not bytes or type(y) is not bytes or len(x) != 32 or len(y) != 32:
            raise WindowsWebAuthnError("webauthn_cose_es256_invalid")
        key = ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"),
                                            ec.SECP256R1()).public_key()
    elif algorithm == -257 and cose.get(1) == 3:
        modulus, exponent = cose.get(-1), cose.get(-2)
        if type(modulus) is not bytes or type(exponent) is not bytes:
            raise WindowsWebAuthnError("webauthn_cose_rs256_invalid")
        key = rsa.RSAPublicNumbers(int.from_bytes(exponent, "big"),
                                   int.from_bytes(modulus, "big")).public_key()
    elif algorithm == -8 and cose.get(1) == 1 and cose.get(-1) == 6:
        raw = cose.get(-2)
        if type(raw) is not bytes or len(raw) != 32:
            raise WindowsWebAuthnError("webauthn_cose_ed25519_invalid")
        key = ed25519.Ed25519PublicKey.from_public_bytes(raw)
    else:
        raise WindowsWebAuthnError("webauthn_cose_algorithm_unsupported")
    return algorithm, key.public_bytes(serialization.Encoding.DER,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)


def _registration_material(attestation_object: bytes, *, challenge: str, rp_id: str):
    from agentflow.approval.webauthn import b64url_encode

    if type(attestation_object) is not bytes or not 1 <= len(attestation_object) <= _MAX_ATTESTATION:
        raise WindowsWebAuthnError("webauthn_attestation_size_invalid")
    value, end = _read_cbor(attestation_object)
    if end != len(attestation_object) or type(value) is not dict:
        raise WindowsWebAuthnError("webauthn_attestation_object_invalid")
    if value.get("fmt") != "none" or value.get("attStmt") != {}:
        raise WindowsWebAuthnError("webauthn_attestation_policy_mismatch")
    auth_data = value.get("authData")
    if type(auth_data) is not bytes or len(auth_data) < 55:
        raise WindowsWebAuthnError("webauthn_registration_authenticator_data_invalid")
    if auth_data[:32] != hashlib.sha256(rp_id.encode("ascii")).digest():
        raise WindowsWebAuthnError("webauthn_registration_rp_id_mismatch")
    flags = auth_data[32]
    if flags & 0x01 == 0 or flags & 0x04 == 0 or flags & 0x40 == 0:
        raise WindowsWebAuthnError("webauthn_registration_up_uv_attested_data_required")
    if flags & 0x22 or flags & 0x80 or (flags & 0x10 and not flags & 0x08):
        raise WindowsWebAuthnError("webauthn_registration_flags_unsupported")
    offset = 37 + 16
    credential_size = int.from_bytes(auth_data[offset:offset + 2], "big")
    offset += 2
    if credential_size < 1 or credential_size > 1024 or offset + credential_size >= len(auth_data):
        raise WindowsWebAuthnError("webauthn_registration_credential_id_invalid")
    credential_id = auth_data[offset:offset + credential_size]
    offset += credential_size
    cose, consumed = _read_cbor(auth_data, offset)
    if type(cose) is not dict or consumed != len(auth_data):
        raise WindowsWebAuthnError("webauthn_registration_cose_key_invalid")
    algorithm, public_key = _public_key_from_cose(cose)
    if type(challenge) is not str or b64url_encode(base64.urlsafe_b64decode(
            challenge + "=" * ((4 - len(challenge) % 4) % 4))) != challenge:
        raise WindowsWebAuthnError("webauthn_registration_challenge_invalid")
    return b64url_encode(credential_id), algorithm, b64url_encode(public_key), flags


def _pointer(data: bytes):
    return (ctypes.c_ubyte * len(data)).from_buffer_copy(data)


class _Rp(ctypes.Structure):
    _fields_ = [("dwVersion", wintypes.DWORD), ("pwszId", wintypes.LPCWSTR),
                ("pwszName", wintypes.LPCWSTR), ("pwszIcon", wintypes.LPCWSTR)]


class _User(ctypes.Structure):
    _fields_ = [("dwVersion", wintypes.DWORD), ("cbId", wintypes.DWORD),
                ("pbId", ctypes.POINTER(ctypes.c_ubyte)), ("pwszName", wintypes.LPCWSTR),
                ("pwszIcon", wintypes.LPCWSTR), ("pwszDisplayName", wintypes.LPCWSTR)]


class _CoseParameter(ctypes.Structure):
    _fields_ = [("dwVersion", wintypes.DWORD), ("pwszCredentialType", wintypes.LPCWSTR),
                ("lAlg", ctypes.c_long)]


class _CoseParameters(ctypes.Structure):
    _fields_ = [("cCredentialParameters", wintypes.DWORD),
                ("pCredentialParameters", ctypes.POINTER(_CoseParameter))]


class _Credential(ctypes.Structure):
    _fields_ = [("dwVersion", wintypes.DWORD), ("cbId", wintypes.DWORD),
                ("pbId", ctypes.POINTER(ctypes.c_ubyte)),
                ("pwszCredentialType", wintypes.LPCWSTR)]


class _Credentials(ctypes.Structure):
    _fields_ = [("cCredentials", wintypes.DWORD), ("pCredentials", ctypes.POINTER(_Credential))]


class _Extensions(ctypes.Structure):
    _fields_ = [("cExtensions", wintypes.DWORD), ("pExtensions", ctypes.c_void_p)]


class _CredentialList(ctypes.Structure):
    _fields_ = [("cCredentials", wintypes.DWORD),
                ("ppCredentials", ctypes.POINTER(ctypes.POINTER(_Credential)))]


class _ClientData(ctypes.Structure):
    _fields_ = [("dwVersion", wintypes.DWORD), ("cbClientDataJSON", wintypes.DWORD),
                ("pbClientDataJSON", ctypes.POINTER(ctypes.c_ubyte)),
                ("pwszHashAlgId", wintypes.LPCWSTR)]


class _MakeOptionsV3(ctypes.Structure):
    _fields_ = [("dwVersion", wintypes.DWORD), ("dwTimeoutMilliseconds", wintypes.DWORD),
                ("CredentialList", _Credentials), ("Extensions", _Extensions),
                ("dwAuthenticatorAttachment", wintypes.DWORD),
                ("bRequireResidentKey", wintypes.BOOL),
                ("dwUserVerificationRequirement", wintypes.DWORD),
                ("dwAttestationConveyancePreference", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("pCancellationId", ctypes.c_void_p),
                ("pExcludeCredentialList", ctypes.POINTER(_CredentialList))]


class _GetOptionsV1(ctypes.Structure):
    _fields_ = [("dwVersion", wintypes.DWORD), ("dwTimeoutMilliseconds", wintypes.DWORD),
                ("CredentialList", _Credentials), ("Extensions", _Extensions),
                ("dwAuthenticatorAttachment", wintypes.DWORD),
                ("dwUserVerificationRequirement", wintypes.DWORD), ("dwFlags", wintypes.DWORD)]


class _AttestationV1(ctypes.Structure):
    _fields_ = [("dwVersion", wintypes.DWORD), ("pwszFormatType", wintypes.LPCWSTR),
                ("cbAuthenticatorData", wintypes.DWORD), ("pbAuthenticatorData", ctypes.POINTER(ctypes.c_ubyte)),
                ("cbAttestation", wintypes.DWORD), ("pbAttestation", ctypes.POINTER(ctypes.c_ubyte)),
                ("dwAttestationDecodeType", wintypes.DWORD), ("pvAttestationDecode", ctypes.c_void_p),
                ("cbAttestationObject", wintypes.DWORD),
                ("pbAttestationObject", ctypes.POINTER(ctypes.c_ubyte)),
                ("cbCredentialId", wintypes.DWORD), ("pbCredentialId", ctypes.POINTER(ctypes.c_ubyte)),
                ("Extensions", _Extensions), ("dwUsedTransport", wintypes.DWORD),
                ("bEpAtt", wintypes.BOOL), ("bLargeBlobSupported", wintypes.BOOL),
                ("bResidentKey", wintypes.BOOL), ("bPrfEnabled", wintypes.BOOL),
                ("cbUnsignedExtensionOutputs", wintypes.DWORD),
                ("pbUnsignedExtensionOutputs", ctypes.POINTER(ctypes.c_ubyte)),
                ("pHmacSecret", ctypes.c_void_p), ("bThirdPartyPayment", wintypes.BOOL),
                ("dwTransports", wintypes.DWORD), ("cbClientDataJSON", wintypes.DWORD),
                ("pbClientDataJSON", ctypes.POINTER(ctypes.c_ubyte)),
                ("cbRegistrationResponseJSON", wintypes.DWORD),
                ("pbRegistrationResponseJSON", ctypes.POINTER(ctypes.c_ubyte))]


class _Assertion(ctypes.Structure):
    _fields_ = [("dwVersion", wintypes.DWORD),
                ("cbAuthenticatorData", wintypes.DWORD),
                ("pbAuthenticatorData", ctypes.POINTER(ctypes.c_ubyte)),
                ("cbSignature", wintypes.DWORD), ("pbSignature", ctypes.POINTER(ctypes.c_ubyte)),
                ("Credential", _Credential), ("cbUserId", wintypes.DWORD),
                ("pbUserId", ctypes.POINTER(ctypes.c_ubyte)), ("Extensions", _Extensions),
                ("cbCredLargeBlob", wintypes.DWORD), ("pbCredLargeBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("dwCredLargeBlobStatus", wintypes.DWORD), ("pHmacSecret", ctypes.c_void_p),
                ("dwUsedTransport", wintypes.DWORD),
                ("cbUnsignedExtensionOutputs", wintypes.DWORD),
                ("pbUnsignedExtensionOutputs", ctypes.POINTER(ctypes.c_ubyte)),
                ("cbClientDataJSON", wintypes.DWORD),
                ("pbClientDataJSON", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsWebAuthnApi:
    """Thin, version-gated Win32 binding; no command or filesystem operations."""

    def __init__(self):
        if sys.platform != "win32":
            raise WindowsWebAuthnError("windows_platform_required")
        try:
            self.dll = ctypes.WinDLL("webauthn.dll", use_last_error=True)
            self.dll.WebAuthNGetApiVersionNumber.restype = wintypes.DWORD
            self.version = self.dll.WebAuthNGetApiVersionNumber()
        except (AttributeError, OSError) as exc:
            raise WindowsWebAuthnError("windows_webauthn_api_unavailable") from exc
        if self.version < 1:
            raise WindowsWebAuthnError("windows_webauthn_api_version_unsupported")
        self.dll.WebAuthNIsUserVerifyingPlatformAuthenticatorAvailable.argtypes = [ctypes.POINTER(wintypes.BOOL)]
        self.dll.WebAuthNIsUserVerifyingPlatformAuthenticatorAvailable.restype = ctypes.c_long
        self.dll.WebAuthNAuthenticatorMakeCredential.restype = ctypes.c_long
        self.dll.WebAuthNAuthenticatorMakeCredential.argtypes = [
            wintypes.HWND, ctypes.POINTER(_Rp), ctypes.POINTER(_User),
            ctypes.POINTER(_CoseParameters), ctypes.POINTER(_ClientData),
            ctypes.POINTER(_MakeOptionsV3), ctypes.POINTER(ctypes.POINTER(_AttestationV1))]
        self.dll.WebAuthNAuthenticatorGetAssertion.restype = ctypes.c_long
        self.dll.WebAuthNAuthenticatorGetAssertion.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, ctypes.POINTER(_ClientData),
            ctypes.POINTER(_GetOptionsV1), ctypes.POINTER(ctypes.POINTER(_Assertion))]
        self.dll.WebAuthNFreeCredentialAttestation.argtypes = [ctypes.POINTER(_AttestationV1)]
        self.dll.WebAuthNFreeAssertion.argtypes = [ctypes.POINTER(_Assertion)]

    def platform_uv_available(self) -> bool:
        available = wintypes.BOOL()
        result = self.dll.WebAuthNIsUserVerifyingPlatformAuthenticatorAvailable(ctypes.byref(available))
        return result == 0 and bool(available.value)

    @staticmethod
    def _owner_window():
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleWindow.argtypes = []
        kernel.GetConsoleWindow.restype = wintypes.HWND
        hwnd = kernel.GetConsoleWindow()
        return hwnd or None

    @staticmethod
    def _client_data(kind: str, challenge: str):
        raw = json.dumps({"type": kind, "challenge": challenge, "origin": ORIGIN},
                         separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        buffer = _pointer(raw)
        return raw, buffer, _ClientData(1, len(raw), buffer, "SHA256")

    def make_credential(self, principal: str, challenge: str, *, timeout_ms: int = 120000):
        if not self.platform_uv_available():
            raise WindowsWebAuthnError("windows_platform_uv_authenticator_unavailable")
        raw, data_buffer, client_data = self._client_data("webauthn.create", challenge)
        rp = _Rp(1, RP_ID, "AgentFlow Local", None)
        user_id = hashlib.sha256(principal.encode("utf-8")).digest()
        user_buffer = _pointer(user_id)
        user = _User(1, len(user_id), user_buffer, principal, None, "AgentFlow local operator")
        parameter = _CoseParameter(1, "public-key", -7)
        parameters = _CoseParameters(1, ctypes.pointer(parameter))
        options = _MakeOptionsV3(3, timeout_ms, _Credentials(0, None), _Extensions(0, None),
                                 1, False, 1, 1, 0, None, None)
        output = ctypes.POINTER(_AttestationV1)()
        result = self.dll.WebAuthNAuthenticatorMakeCredential(
            self._owner_window(), ctypes.byref(rp), ctypes.byref(user), ctypes.byref(parameters),
            ctypes.byref(client_data), ctypes.byref(options), ctypes.byref(output))
        if result != 0 or not output:
            raise _native_error("enrollment", result)
        try:
            value = output.contents
            if not value.cbAttestationObject or value.cbAttestationObject > _MAX_ATTESTATION:
                raise WindowsWebAuthnError("webauthn_attestation_size_invalid")
            if (not value.cbClientDataJSON or value.cbClientDataJSON > 8192
                    or not value.cbCredentialId or value.cbCredentialId > 1024):
                raise WindowsWebAuthnError("webauthn_registration_client_data_missing")
            attestation = ctypes.string_at(value.pbAttestationObject, value.cbAttestationObject)
            returned_client_data = ctypes.string_at(value.pbClientDataJSON, value.cbClientDataJSON)
            credential_id = ctypes.string_at(value.pbCredentialId, value.cbCredentialId)
            return attestation, returned_client_data, credential_id
        finally:
            self.dll.WebAuthNFreeCredentialAttestation(output)

    def get_assertion(self, challenge: str, credential_ids: tuple[bytes, ...], *,
                      timeout_ms: int = 120000):
        from agentflow.approval.webauthn import WebAuthnAssertion, b64url_encode
        if not self.platform_uv_available():
            raise WindowsWebAuthnError("windows_platform_uv_authenticator_unavailable")
        if not credential_ids or len(credential_ids) > 32:
            raise WindowsWebAuthnError("webauthn_active_credential_required")
        raw, data_buffer, client_data = self._client_data("webauthn.get", challenge)
        id_buffers = [_pointer(item) for item in credential_ids]
        credentials = (_Credential * len(credential_ids))(*[
            _Credential(1, len(item), buffer, "public-key")
            for item, buffer in zip(credential_ids, id_buffers)])
        options = _GetOptionsV1(1, timeout_ms, _Credentials(len(credential_ids), credentials),
                                _Extensions(0, None), 1, 1, 0)
        output = ctypes.POINTER(_Assertion)()
        result = self.dll.WebAuthNAuthenticatorGetAssertion(
            self._owner_window(), RP_ID, ctypes.byref(client_data), ctypes.byref(options),
            ctypes.byref(output))
        if result != 0 or not output:
            raise _native_error("assertion", result)
        try:
            value = output.contents
            if (not 37 <= value.cbAuthenticatorData <= 8192 or not value.cbSignature
                    or value.cbSignature > 8192 or not value.Credential.cbId
                    or value.Credential.cbId > 1024 or not value.pbClientDataJSON
                    or not 1 <= value.cbClientDataJSON <= 8192):
                raise WindowsWebAuthnError("webauthn_assertion_output_invalid")
            auth_data = ctypes.string_at(value.pbAuthenticatorData, value.cbAuthenticatorData)
            signature = ctypes.string_at(value.pbSignature, value.cbSignature)
            credential_id = ctypes.string_at(value.Credential.pbId, value.Credential.cbId)
            returned_client_data = ctypes.string_at(value.pbClientDataJSON, value.cbClientDataJSON)
            challenge_id = "local-" + hashlib.sha256(challenge.encode("ascii")).hexdigest()[:32]
            return WebAuthnAssertion(1, challenge_id, b64url_encode(credential_id),
                                     returned_client_data, auth_data, signature)
        finally:
            self.dll.WebAuthNFreeAssertion(output)


class WindowsHelloCredentialEnrollment:
    """Explicit interactive bootstrap for the current Windows account."""

    def __init__(self, broker_store, api: WindowsWebAuthnApi | None = None):
        from agentflow.approval.persistence import BrokerStore
        if not isinstance(broker_store, BrokerStore):
            raise WindowsWebAuthnError("broker_store_required")
        self.store = broker_store
        self.api = api or WindowsWebAuthnApi()

    def enroll_interactive(self, *, input_stream=None, output_stream=None,
                           now: datetime | None = None):
        input_stream = input_stream or sys.stdin
        output_stream = output_stream or sys.stderr
        if not getattr(input_stream, "isatty", lambda: False)():
            raise WindowsWebAuthnError("interactive_terminal_required_for_enrollment")
        principal = windows_operator_principal()
        active = self.store.list_active_webauthn_credentials(principal, RP_ID)
        if active:
            raise WindowsWebAuthnError("active_credential_already_enrolled; revoke before replacement")
        output_stream.write(
            f"Enrolling Windows platform credential for local principal {principal}.\n"
            "The Windows security dialog will request user verification. No PIN or biometric is read by AgentFlow.\n"
            "To continue, type ENROLL WINDOWS HELLO: ")
        output_stream.flush()
        if input_stream.readline().strip() != "ENROLL WINDOWS HELLO":
            raise WindowsWebAuthnError("enrollment_not_confirmed")
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise WindowsWebAuthnError("aware_enrollment_clock_required")
        import secrets
        challenge_bytes = secrets.token_bytes(32)
        from agentflow.approval.webauthn import WebAuthnCredential, b64url_encode, build_credential
        challenge = b64url_encode(challenge_bytes)
        attestation, returned_client_data, returned_credential_id = self.api.make_credential(principal, challenge)
        expected_client_data, _buffer, _native_data = self.api._client_data("webauthn.create", challenge)
        if returned_client_data != expected_client_data:
            raise WindowsWebAuthnError("webauthn_registration_client_data_mismatch")
        credential_id, algorithm, public_key, flags = _registration_material(
            attestation, challenge=challenge, rp_id=RP_ID)
        from agentflow.approval.webauthn import b64url_decode
        if b64url_decode(credential_id, maximum=1024, label="credential_id") != returned_credential_id:
            raise WindowsWebAuthnError("webauthn_registration_credential_id_mismatch")
        credential = build_credential(
            WebAuthnCredential,
            protocol_version=1, credential_id=credential_id, principal_id=principal,
            rp_id=RP_ID, allowed_origins=(ORIGIN,), algorithm=algorithm,
            public_key_spki_b64url=public_key, created_at=_stamp(instant), expires_at=None)
        record = self.store._store_webauthn_credential(credential)
        return record, {"principal_id": principal, "rp_id": RP_ID, "credential_digest": credential.credential_digest,
                        "user_present": bool(flags & 1), "user_verified": bool(flags & 4),
                        "private_key_persisted": False}


class WindowsWebAuthnProofProvider:
    """Interactive Windows platform assertion provider for consent review."""

    def __init__(self, broker_store, api: WindowsWebAuthnApi | None = None):
        from agentflow.approval.persistence import BrokerStore
        if not isinstance(broker_store, BrokerStore):
            raise WindowsWebAuthnError("broker_store_required")
        self.store = broker_store
        self.api = api or WindowsWebAuthnApi()

    def prove(self, request: LocalConsentRequest, receipt: LocalConsentReceipt,
              challenge_nonce: str):
        if (not isinstance(request, LocalConsentRequest) or not isinstance(receipt, LocalConsentReceipt)
                or type(challenge_nonce) is not str or not 32 <= len(challenge_nonce) <= 256):
            raise WindowsWebAuthnError("typed_consent_and_persisted_challenge_required")
        principal = windows_operator_principal()
        if request.operator_identity != principal:
            raise WindowsWebAuthnError("requested_operator_is_not_current_windows_account")
        credentials = self.store.list_active_webauthn_credentials(principal, RP_ID)
        if not credentials:
            raise WindowsWebAuthnError("active_windows_hello_credential_required")
        raw_challenge = base64.urlsafe_b64decode(
            challenge_nonce + "=" * ((4 - len(challenge_nonce) % 4) % 4))
        if len(raw_challenge) != 32:
            raise WindowsWebAuthnError("persisted_challenge_format_invalid")
        now = datetime.now(timezone.utc)
        expiry = min(datetime.fromisoformat(request.expires_at.replace("Z", "+00:00")),
                     datetime.fromisoformat(receipt.expires_at.replace("Z", "+00:00")),
                     now + timedelta(seconds=120))
        if expiry <= now:
            raise WindowsWebAuthnError("consent_challenge_expired")
        assertion = self.api.get_assertion(challenge_nonce, tuple(
            base64.urlsafe_b64decode(item.credential.credential_id + "=" *
                                     ((4 - len(item.credential.credential_id) % 4) % 4))
            for item in credentials))
        return LocalWebAuthnAssertionProof(assertion, _stamp(expiry))


class WindowsWebAuthnOperatorAuthenticator:
    """Verify native assertions using the existing strict AgentFlow verifier."""

    def __init__(self, broker_store):
        from agentflow.approval.persistence import BrokerStore
        if not isinstance(broker_store, BrokerStore):
            raise WindowsWebAuthnError("broker_store_required")
        self.store = broker_store

    def authenticate(self, challenge_nonce: str, proof: object, *, now: datetime):
        from agentflow.approval.persistence import BrokerConflict, BrokerStoreError
        from agentflow.approval.webauthn import (
            WebAuthnError, WebAuthnVerificationError, b64url_decode,
            challenge_digest, verify_assertion_signature,
        )
        if (sys.platform != "win32" or not isinstance(proof, LocalWebAuthnAssertionProof)
                or not isinstance(proof.assertion, __import__(
                    "agentflow.approval.webauthn", fromlist=["WebAuthnAssertion"]).WebAuthnAssertion)):
            return None
        principal = windows_operator_principal()
        challenge_id = "local-" + hashlib.sha256(challenge_nonce.encode("ascii")).hexdigest()[:32]
        assertion = proof.assertion
        if assertion.challenge_id != challenge_id:
            return None
        try:
            raw_challenge = b64url_decode(challenge_nonce, maximum=64, label="challenge")
            if len(raw_challenge) != 32:
                return None
            rows = self.store.list_active_webauthn_credentials(principal, RP_ID)
            state = next((item for item in rows
                          if item.credential.credential_id == assertion.credential_id), None)
            if state is None:
                return None
            credential = state.credential
            if credential.principal_id != principal or credential.rp_id != RP_ID:
                return None
            expiry = _stamp(datetime.fromisoformat(proof.expires_at.replace("Z", "+00:00")))
            binding = _Binding(1, challenge_id, challenge_digest(raw_challenge), principal,
                               RP_ID, (ORIGIN,), expiry)
            counter, _ = verify_assertion_signature(assertion, binding, credential,
                                                    challenge_bytes=raw_challenge, now=now)
            if state.counter_mode == "monotonic" and counter <= state.sign_count:
                return None
            self.store.advance_webauthn_credential_counter(credential.credential_id, counter, now=now)
            proof_hash = hashlib.sha256(assertion.client_data_json + assertion.authenticator_data
                                        + assertion.signature + assertion.credential_id.encode("ascii")).hexdigest()
            return AuthenticatedOperator(principal, "sha256:" + proof_hash,
                                         "windows_webauthn_uv", _stamp(now))
        except (ValueError, WebAuthnError, WebAuthnVerificationError, OSError,
                BrokerConflict, BrokerStoreError):
            return None
