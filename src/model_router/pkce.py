"""PKCE helpers (RFC 7636, S256).

Port of opencodex src/oauth/pkce.ts (MIT, see THIRD_PARTY.md):
96 random bytes -> base64url verifier; SHA-256 -> base64url challenge.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class PKCE:
    verifier: str
    challenge: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce() -> PKCE:
    verifier = _b64url(secrets.token_bytes(96))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return PKCE(verifier=verifier, challenge=challenge)


def challenge_for(verifier: str) -> str:
    """S256 challenge for a given verifier (test seam / RFC vector check)."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
