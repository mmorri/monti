"""Z.ai (GLM Coding Plan) provider: interactive plan-key login.

No portable OAuth flow exists for the GLM Coding Plan today — ZCode's
zcode.z.ai flow is a desktop deep-link flow whose token is not proven for
direct inference. Under the documented exception to the no-keys rule
(SPEC.md, "Plan-key exception"), this provider accepts the Coding Plan key
interactively via `monti login zai` only: never from config, never from
the environment. The key draws from the flat-rate subscription quota, not
per-token platform billing, and is stored exactly like an OAuth token
(mode 0600, same auth.json).

Chat transport: OpenAI-compatible POST
https://api.z.ai/api/coding/paas/v4/chat/completions (config-overridable;
BigModel CN accounts can point gateway_base_url at
https://open.bigmodel.cn/api/coding/paas/v4).
"""

from __future__ import annotations

import getpass

from ..errors import ReloginRequired
from ..store import Credentials
from .base import ChatRequest, ChatTransport, Provider
from .openai_compat import OpenAICompatTransport, list_openai_models

DEFAULT_GATEWAY_BASE_URL = "https://api.z.ai/api/coding/paas/v4"


class ZaiProvider(Provider):
    id = "zai"
    display = "Z.ai (GLM Coding Plan)"

    def __init__(self, progress=None,
                 gateway_base_url: str = DEFAULT_GATEWAY_BASE_URL):
        super().__init__(progress)
        self.gateway_base_url = gateway_base_url

    def login(self, **kwargs) -> Credentials:
        print("Z.ai has no portable OAuth login; Monti uses the GLM Coding Plan key")
        print("from your z.ai dashboard (subscription quota, not platform billing).")
        supplied = kwargs.get("plan_key")
        plan_key = supplied if isinstance(supplied, str) and supplied.strip() \
            else getpass.getpass("Paste your GLM Coding Plan key (input hidden): ")
        plan_key = plan_key.strip()
        if not plan_key or any(ch.isspace() for ch in plan_key):
            raise ValueError("plan key must be a nonempty single token")
        return Credentials(access=plan_key, extra={"plan": "glm_coding"})

    def refresh(self, creds: Credentials) -> Credentials:
        # Plan keys cannot be refreshed; a dead key means logging in again.
        raise ReloginRequired(self.id)

    def open_chat(self, creds: Credentials, request: ChatRequest, **kwargs) -> ChatTransport:
        return OpenAICompatTransport(
            base_url=self.gateway_base_url, access_token=creds.access, request=request)

    def list_models(self, creds: Credentials) -> list[str]:
        return list_openai_models(self.gateway_base_url, creds.access)
