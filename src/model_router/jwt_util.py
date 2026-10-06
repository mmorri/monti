"""JWT payload decoding for identity/expiry markers only.

Decoded locally as routing markers, never as authenticity proof.
"""

from __future__ import annotations

import base64
import json
from typing import Any


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def jwt_claim(token: str, *names: str) -> str | None:
    payload = decode_jwt_payload(token)
    if not payload:
        return None
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None
