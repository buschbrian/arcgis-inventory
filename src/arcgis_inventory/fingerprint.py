"""Stable finding identity.

A fingerprint is a hash of the rule, the resource it fired on, and the salient
evidence that distinguishes one instance of that rule on that resource from
another. It deliberately excludes run id, timestamps, counts, severities, and
anything cosmetic.

Get this wrong and every crawl resurrects findings someone already dismissed,
which is the failure mode that makes people stop running scanners.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["finding_fingerprint"]

_SEP = "\x1f"
_LENGTH = 32  # hex chars; 128 bits is far more than enough for one org's findings


def finding_fingerprint(
    rule_id: str,
    resource_identity: str,
    *,
    evidence: Mapping[str, Any] | None = None,
) -> str:
    """Return the stable fingerprint for one finding.

    ``resource_identity`` is the portal item id or the normalized endpoint URL
    --- never the autoincrement ``resource_id``, which is database-local and
    changes when the database is rebuilt.

    ``evidence`` holds only the fields that make two findings of the same rule
    on the same resource genuinely different (for example the specific offending
    layer URL). Anything that can change without the finding changing --- a
    count, a message, a timestamp --- must stay out of it.
    """
    if not rule_id:
        raise ValueError("rule_id is required for a fingerprint")
    if not resource_identity:
        raise ValueError("resource_identity is required for a fingerprint")

    payload = _SEP.join((rule_id, resource_identity, _canonical(evidence or {})))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_LENGTH]


def _canonical(value: Any) -> str:
    """Deterministic JSON: sorted keys, sorted lists of scalars, no whitespace.

    Lists are sorted because extraction order is an implementation detail; if
    order is genuinely meaningful for a rule, that rule should put the ordered
    value in a single string field instead.
    """
    return json.dumps(_sorted(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sorted(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _sorted(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (str, bytes)):
        return value if isinstance(value, str) else value.decode("utf-8", "replace")
    if isinstance(value, Sequence):
        items = [_sorted(v) for v in value]
        try:
            return sorted(items, key=_canonical)
        except TypeError:  # pragma: no cover - defensive
            return items
    return value
