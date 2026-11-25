"""Communication Optimizer (L): deduplication, incremental cursors, and token budgeting."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable


def estimate_tokens(text: str) -> int:
    """Deterministic lightweight token estimate (~4 chars per token)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


class CommunicationOptimizer:
    """Deduplicates repeated messages while preserving essential evidence."""

    def __init__(self, tokenizer: Callable[[str], int] | None = None) -> None:
        self._seen_hashes: set[str] = set()
        self._tokenizer = tokenizer or estimate_tokens
        self.total_messages_in = 0
        self.total_messages_out = 0
        self.total_tokens_saved = 0

    def optimize_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter repeated non-essential messages; always preserve essential evidence."""
        out: list[dict[str, Any]] = []
        for msg in messages:
            self.total_messages_in += 1
            essential = bool(msg.get("essential") or msg.get("kind") in ("evidence", "finding", "error"))
            canonical = json.dumps(msg, sort_keys=True, ensure_ascii=False)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if not essential and digest in self._seen_hashes:
                self.total_tokens_saved += self._tokenizer(canonical)
                continue
            self._seen_hashes.add(digest)
            self.total_messages_out += 1
            out.append(msg)
        return out
