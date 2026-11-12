"""Deterministic token estimation.

The prototype must run offline and reproducibly, so it uses a cheap
character-class estimator instead of a model tokenizer:

* CJK characters (and CJK punctuation / full-width forms) count as 1 token each;
* everything else is approximated at 4 characters per token.

The estimator is intentionally monotonic and documented, and every budget
decision in the system goes through this one function so accounting stays
consistent.  A real deployment would swap in the model's tokenizer.
"""

from __future__ import annotations

import re

_CJK_RE = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]"
)
_DEFAULT_RATIO = 4


def cjk_chars(text: str) -> int:
    return len(_CJK_RE.findall(text))


def estimate_tokens(text: str) -> int:
    """Estimate the token length of ``text`` (>= 0)."""
    if not text:
        return 0
    cjk = cjk_chars(text)
    other = len(text) - cjk
    return cjk + (other + _DEFAULT_RATIO - 1) // _DEFAULT_RATIO


def truncate_to_tokens(text: str, limit: int) -> tuple[str, int]:
    """Truncate ``text`` so that its estimated size is <= ``limit``.

    Returns ``(truncated_text, estimated_tokens)``.  Uses binary search over the
    character count so it works for mixed CJK/Latin text.
    """
    if limit <= 0:
        return "", 0
    if estimate_tokens(text) <= limit:
        return text, estimate_tokens(text)
    low, high = 0, len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid]
        if estimate_tokens(candidate) <= limit:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best, estimate_tokens(best)


def fits(text: str, limit: int) -> bool:
    return estimate_tokens(text) <= limit


__all__ = ["cjk_chars", "estimate_tokens", "fits", "truncate_to_tokens"]
