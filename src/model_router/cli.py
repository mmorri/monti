"""Monti terminal coding assistant and subscription management commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import RouterConfig
from .errors import NoSubscriptionAuth, RouterError
from .http import HttpStatusError
from .providers import REGISTRY, create
from .store import TokenStore

DEFAULT_CONFIG = Path("config.yaml")


def _store() -> TokenStore:
    return TokenStore()


def cmd_login(args) -> int:
    provider = create(args.provider)
    try:
        creds = provider.login(open_browser=not args.no_browser)
    except (RouterError, HttpStatusError) as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1
    path = _store().save(args.provider, creds)
    print(f"[{args.provider}] saved to {path} (mode 0600)")
    if creds.account_id or creds.email:
        print(f"[{args.provider}] account: {creds.email or creds.account_id}")
    return 0


def cmd_logout(args) -> int:
    if _store().clear(args.provider):
        print(f"[{args.provider}] logged out")
        return 0
    print(f"[{args.provider}] no stored credentials", file=sys.stderr)
    return 1


def cmd_status(_args) -> int:
    store = _store()
    providers = store.providers()
    if not providers:
        print("no subscription auth stored")
        return 1
    for provider_id in providers:
        creds = store.load(provider_id)
        identity = ""
        if creds:
            identity = creds.email or creds.account_id or ""
        print(f"{provider_id}: logged in{f' ({identity})' if identity else ''}")
    return 0


def cmd_serve(args) -> int:
    from .proxy import ProxyApp, serve

    config_path = Path(args.config) if args.config else (
        DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None)
    try:
        config = RouterConfig.load(config_path)
    except (ValueError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    if args.port:
        config.port = args.port
    app = ProxyApp(config)
    try:
        server = serve(app, port=config.port)
    except NoSubscriptionAuth as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


def load_config(path: str | None) -> RouterConfig:
    return RouterConfig.load(Path(path) if path else (
        DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None))


def _print_catalog(gateway, store, provider_id: str, indent: str = "  ") -> None:
    """Best-effort live model listing for a logged-in provider."""
    creds = store.load(provider_id)
    if not creds:
        return
    try:
        models = gateway.provider_for(provider_id).list_models(creds)
    except Exception:  # noqa: BLE001 — catalog is best-effort
        models = []
    if models:
        shown = ", ".join(models[:8])
        more = f" (+{len(models) - 8} more)" if len(models) > 8 else ""
        print(f"{indent}models: {shown}{more}")
    else:
        print(f"{indent}models: catalog unavailable for this provider")


def cmd_models(args) -> int:
    from .gateway import Gateway
    try:
        gateway = Gateway(load_config(args.config), _store())
        store = gateway.store
        for tier, entry in gateway.tier_status().items():
            print(f"{tier}: {entry['provider']}/{entry['model']} · {entry['transport']}")
            if entry["auth"]:
                _print_catalog(gateway, store, entry["provider"])
        others = [p for p in store.providers()
                  if p not in {gateway.config.fast.provider, gateway.config.strong.provider}]
        for provider_id in others:
            print(f"logged in: {provider_id}")
            _print_catalog(gateway, store, provider_id)
        print("Modes: auto, fast, strong, weak-first-escalate")
        return 0
    except (ValueError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1


def cmd_chat(args) -> int:
    from contextlib import redirect_stdout
    from .agent import TerminalAgent
    from .config import Tier
    from .proxy import ProxyApp

    try:
        config = load_config(args.config)
        workspace = Path(args.workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace}")
        if args.max_steps < 1:
            raise ValueError("--max-steps must be positive")
        if args.model:
            provider, separator, model = args.model.partition("/")
            if not separator or provider not in REGISTRY or not model:
                raise ValueError("--model must be provider/model")
            setattr(config, args.tier, Tier(provider, model))
        mode = {"fast": "fast-only", "strong": "strong-only"}.get(args.mode, args.mode)
        mode = mode or config.default_mode
        app = ProxyApp(config)
        # Pinned sessions only require the selected tier; automatic routing follows startup policy.
        with redirect_stdout(sys.stderr):
            if mode in ("fast-only", "strong-only"):
                tier = "fast" if mode == "fast-only" else "strong"
                entry = app.gateway.tier_status()[tier]
                if not entry["ok"]:
                    raise NoSubscriptionAuth(f"{tier}: {entry['transport']}; "
                                             f"run `monti login {entry['provider']}`")
            else:
                app.check_auth()
        agent = TerminalAgent(app, workspace, mode=mode, yes=args.yes, max_steps=args.max_steps)
        prompt = " ".join(args.prompt)
        if not sys.stdin.isatty():
            piped = sys.stdin.read().strip()
            prompt = "\n\n".join(part for part in (prompt, piped) if part)
            if not prompt:
                raise ValueError("provide a task as an argument or on stdin")
        if prompt:
            agent.turn(prompt)
            return 0
        return agent.run()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (RouterError, HttpStatusError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="monti",
                                     description="Terminal coding assistant with subscription model routing",
                                     epilog="Run monti with no arguments for a session, or monti \"your task\".")
    # Existing model-router commands remain available.
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="interactive subscription login")
    login.add_argument("provider", choices=sorted(REGISTRY))
    login.add_argument("--no-browser", action="store_true",
                       help="print the URL instead of opening a browser")
    login.set_defaults(func=cmd_login)
    logout = sub.add_parser("logout", help="delete stored subscription tokens")
    logout.add_argument("provider", choices=sorted(REGISTRY))
    logout.set_defaults(func=cmd_logout)
    status = sub.add_parser("status", help="show stored subscription logins")
    status.set_defaults(func=cmd_status)
    serve_p = sub.add_parser("serve", help="run the OpenAI-compatible proxy")
    serve_p.add_argument("--config", default=None, help="path to config.yaml")
    serve_p.add_argument("--port", type=int, default=None, help="listen port")
    serve_p.set_defaults(func=cmd_serve)
    models = sub.add_parser("models", help="show configured models and availability")
    models.add_argument("--config")
    models.set_defaults(func=cmd_models)
    chat = sub.add_parser("chat", help="interactive session or one-shot coding task")
    chat.add_argument("prompt", nargs="*", help="task; omit for an interactive session")
    chat.add_argument("--config", help="path to config.yaml")
    chat.add_argument("--workspace", default=".", help="workspace directory (default: current directory)")
    chat.add_argument("--mode", choices=("auto", "fast", "strong", "fast-only", "strong-only", "weak-first-escalate"))
    chat.add_argument("--model", help="override a tier with provider/model")
    chat.add_argument("--tier", choices=("fast", "strong"), default="fast", help="tier to override with --model")
    chat.add_argument("--yes", "-y", action="store_true", help="authorize file writes and shell commands without prompts")
    chat.add_argument("--max-steps", type=int, default=20, help="maximum model calls per task")
    chat.set_defaults(func=cmd_chat)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"login", "logout", "status", "serve", "models", "chat"}
    if not argv or (argv[0] not in commands and argv[0] not in ("-h", "--help")):
        argv.insert(0, "chat")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
