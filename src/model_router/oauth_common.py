"""Shared browser-PKCE callback flow: localhost listener + manual paste fallback.

Mirrors opencodex src/oauth/callback-server.ts behavior: bind 127.0.0.1 on a
preferred port, verify state, capture the authorization code, and fall back to
a manual paste when the browser cannot reach this machine.
"""

from __future__ import annotations

import html
import secrets
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer


@dataclass
class CallbackResult:
    code: str
    state: str


_SUCCESS_PAGE = """<!doctype html><html><body style="font-family:sans-serif">
<h2>Login complete</h2><p>You can close this tab and return to the terminal.</p>
</body></html>"""


def _handler_for(expected_path: str, expected_state: str, box: dict,
                 token_params: tuple[str, ...] = ("code",)):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parts = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parts.query)
            token = ""
            for name in token_params:
                values = query.get(name) or [""]
                if values[0]:
                    token = values[0]
                    break
            state = (query.get("state") or [""])[0]
            ok = parts.path == expected_path and token and state == expected_state
            if ok:
                box["code"], box["state"] = token, state
            body = _SUCCESS_PAGE if ok else "<h2>Login failed</h2><p>Invalid callback.</p>"
            raw = body.encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):  # noqa: ANN002, ANN003
            pass

    return Handler


def run_callback_flow(
    *,
    port: int,
    path: str,
    build_auth_url,
    timeout: float = 300.0,
    open_browser: bool = True,
    token_params: tuple[str, ...] = ("code",),
    bind_host: str = "127.0.0.1",
    redirect_host: str | None = None,
) -> tuple[CallbackResult | None, str]:
    """Run one PKCE browser round-trip.

    build_auth_url(state, redirect_uri) -> (url, instructions).
    token_params names the query parameter(s) carrying the credential
    ("code" for authorization-code flows; Windsurf's implicit flow uses
    firebase_id_token/access_token instead).
    bind_host is the local interface to listen on; redirect_host overrides
    the hostname placed in redirect_uri when the provider's client
    registration distinguishes it (Anthropic only allows `localhost`, not
    `127.0.0.1`). Binding 127.0.0.1 still receives localhost connections.
    Returns (result_or_None, redirect_uri). None means manual paste is needed.
    """
    shown_host = redirect_host or bind_host
    redirect_uri = f"http://{shown_host}:{port}{path}"
    state = secrets.token_urlsafe(24)
    url, instructions = build_auth_url(state, redirect_uri)
    print(f"\n{instructions}")
    print(f"Open this URL:\n  {url}\n")
    if open_browser:
        try:
            webbrowser.open(url, new=1)
        except Exception:
            pass
    box: dict = {}
    try:
        server = HTTPServer((bind_host, port),
                            _handler_for(path, state, box, token_params))
    except OSError:
        print("Could not bind the localhost callback listener; use manual paste.")
        return None, redirect_uri
    server.timeout = 1.0
    thread = threading.Thread(target=_serve_until_code, args=(server, box, timeout), daemon=True)
    thread.start()
    thread.join()
    server.server_close()
    if box.get("code"):
        return CallbackResult(code=box["code"], state=box["state"]), redirect_uri
    print("No browser callback received; use manual paste.")
    return None, redirect_uri


def _serve_until_code(server: HTTPServer, box: dict, timeout: float) -> None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline and not box.get("code"):
        server.handle_request()


def prompt_manual_code(provider: str) -> str:
    print(f"[{provider}] Paste the full redirect URL or the authorization code:")
    pasted = input("> ").strip()
    if pasted.startswith("http"):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query)
        frag = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).fragment)
        code = (query.get("code") or frag.get("code") or [""])[0]
        if code:
            return code
    # Anthropic-style "code#state" paste keeps working: providers split it.
    return pasted


def escape(text: str) -> str:
    return html.escape(text, quote=False)
