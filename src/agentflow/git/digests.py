"""Canonical digest helpers for persisted Git operations."""

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence


def _normalize(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {unicodedata.normalize("NFC", str(key)): _normalize(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, float):
        raise ValueError("floats não são aceitos em digests Git")
    return value


def operation_digest(value: Mapping[str, object]) -> str:
    payload = json.dumps(_normalize(value), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def pathset_digest(paths: Sequence[str]) -> str:
    normalized = [unicodedata.normalize("NFC", path) for path in paths]
    if len(normalized) != len(set(normalized)):
        raise ValueError("pathset duplicado após normalização Unicode")
    payload = b"".join(path.encode("utf-8") + b"\0" for path in sorted(normalized, key=lambda value: value.encode("utf-8")))
    return "sha256:" + hashlib.sha256(payload).hexdigest()
