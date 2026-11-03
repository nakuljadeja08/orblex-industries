"""
rh_client.py — phase 0 spike (EXECUTOR_DESIGN.md §1.2): can our own program sign
in to Robinhood's agentic trading MCP and read the agentic account?

READ-ONLY BY CONSTRUCTION. call() refuses every tool that is not in READ_TOOLS
and any account other than AGENTIC_ACCOUNT, so this module cannot place,
cancel or approve anything even if asked to. Order submission belongs to the
executor (a later phase) and will live in a separate module with its own limits.

Sign-in is OAuth 2.1 with PKCE against Robinhood's hosted server (public
client, dynamic registration, no secret). `login` opens the browser; YOU sign
in to Robinhood and confirm on your phone, and the redirect lands on a one-shot
listener on 127.0.0.1. Tokens and the registered client are kept in the Windows
Credential Manager (keyring), never in the repo, .env or logs.

Usage (from the project directory, with the venv's Python):
  python rh_client.py login     # one-time browser sign-in, then a read-only check
  python rh_client.py check     # read-only: account, buying power, approval setting, recent orders
  python rh_client.py tools     # list the server's tools and which ones we allow
  python rh_client.py logout    # delete the stored tokens and client registration
"""

import argparse
import asyncio
import json
import sys
import logging
import os
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

SERVER_URL      = "https://agent.robinhood.com/mcp/trading"
from accounts import AGENTIC_ACCOUNT, MAIN_ACCOUNT   # noqa: E402  (from the environment; "" refuses every account call)
CALLBACK_HOST   = "127.0.0.1"
CALLBACK_PORT   = 8765
REDIRECT_URI    = f"http://localhost:{CALLBACK_PORT}/callback"
LOGIN_TIMEOUT_S = 300
KEYRING_SERVICE = "orblex-industries/robinhood-mcp"
CHUNK           = 1000                 # Windows Credential Manager caps a secret at ~2.5 KB

# Tools this module may call. Everything else, in particular place_*, cancel_*,
# review_*, exercise_*, decline_trade_approval and every watchlist/alert writer, is refused.
READ_TOOLS = frozenset({
    "get_accounts", "get_portfolio", "get_equity_positions", "get_equity_orders",
    "get_equity_quotes", "get_trade_approval_setting", "get_trade_approvals",
})


class NotAllowed(Exception):
    """A call this read-only client refuses to make."""


class NotSignedIn(Exception):
    """No usable Robinhood session is stored."""


# The MAIN account may only be read for the weekly LONG TERM snapshot (weights only).
MAIN_READ_TOOLS = frozenset({"get_portfolio", "get_equity_positions"})


def guard_call(name: str, args: dict | None) -> None:
    """Raise NotAllowed unless the call is a read-only tool on the agentic account
    (or one of the two weight-snapshot reads on the main account)."""
    if name not in READ_TOOLS:
        raise NotAllowed(f"{name} is not a read-only tool; rh_client never calls it")
    acct = (args or {}).get("account_number")
    if acct is None or (AGENTIC_ACCOUNT and str(acct) == AGENTIC_ACCOUNT):
        return
    if MAIN_ACCOUNT and str(acct) == MAIN_ACCOUNT and name in MAIN_READ_TOOLS:
        return
    raise NotAllowed(f"account {mask(acct)} is not the agentic account")


def mask(acct) -> str:
    s = str(acct)
    return "••" + s[-4:] if len(s) > 4 else "••"


# ── Token storage: Supabase, encrypted (shared by your PC and GitHub Actions) ──

class SupabaseStore:
    """mcp TokenStorage in the broker_secrets table (service key only; no RLS policy),
    encrypted with Fernet using RH_TOKEN_KEY, which lives only in GitHub Secrets and
    your local .env. The database never sees a usable token. One store for every
    machine, so a refresh on one side never strands the other."""

    TABLE = "broker_secrets"

    def __init__(self, client=None, key: str | None = None):
        from cryptography.fernet import Fernet
        from dotenv import load_dotenv
        load_dotenv()                                  # the CLI path doesn't go through config.py first
        key = key or os.environ.get("RH_TOKEN_KEY")
        if not key:
            raise NotSignedIn("RH_TOKEN_KEY is not set")
        self.f = Fernet(key.encode() if isinstance(key, str) else key)
        if client is None:
            from config import load_config
            from loader import get_client
            cfg = load_config()
            client = get_client(cfg.supabase_url, cfg.supabase_key)
        self.sb = client

    def _get(self, name: str) -> str | None:
        r = self.sb.table(self.TABLE).select("ciphertext").eq("name", name).limit(1).execute().data
        return self.f.decrypt(r[0]["ciphertext"].encode()).decode() if r else None

    def _put(self, name: str, value: str) -> None:
        self.sb.table(self.TABLE).upsert({"name": name, "ciphertext": self.f.encrypt(value.encode()).decode(),
                                          "updated_at": datetime.now(timezone.utc).isoformat()},
                                         on_conflict="name").execute()

    def clear(self) -> None:
        self.sb.table(self.TABLE).delete().in_("name", ["robinhood.tokens", "robinhood.client"]).execute()

    expires_at: float | None = None      # of the tokens last read; see _dump_tokens

    async def get_tokens(self):
        tokens, self.expires_at = _load_tokens(self._get("robinhood.tokens"))
        return tokens

    async def set_tokens(self, tokens, expires_at: float | None = None) -> None:
        self._put("robinhood.tokens", _dump_tokens(tokens, expires_at))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        raw = self._get("robinhood.client")
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        self._put("robinhood.client", client_info.model_dump_json(exclude_none=True))


def default_store():
    """RH_TOKEN_STORE=supabase → the shared encrypted store; otherwise this PC's keyring."""
    from dotenv import load_dotenv
    load_dotenv()
    return SupabaseStore() if os.environ.get("RH_TOKEN_STORE", "").lower() == "supabase" else KeyringStore()


def _dump_tokens(tokens, expires_at: float | None = None) -> str:
    """Token JSON plus the absolute expiry. mcp only remembers expiry in memory, so a
    fresh process would otherwise treat a dead access token as good forever."""
    d = json.loads(tokens.model_dump_json(exclude_none=True))
    if expires_at is None and tokens.expires_in:
        expires_at = time.time() + tokens.expires_in
    if expires_at is not None:
        d["expires_at"] = expires_at
    return json.dumps(d)


def _load_tokens(raw: str | None):
    """(OAuthToken, expires_at or None). OAuthToken ignores the extra key, so tokens
    written before expires_at existed still load."""
    from mcp.shared.auth import OAuthToken
    if not raw:
        return None, None
    return OAuthToken.model_validate_json(raw), json.loads(raw).get("expires_at")


# ── Token storage: Windows Credential Manager, chunked ─────────────────────────

class KeyringStore:
    """mcp TokenStorage backed by keyring. Values are JSON, split into chunks
    because a single Windows credential holds only ~2.5 KB."""

    def __init__(self, backend=None, service: str = KEYRING_SERVICE):
        if backend is None:
            import keyring
            backend = keyring
        self.kr, self.service = backend, service

    def _put(self, name: str, value: str | None) -> None:
        self._delete(name)
        if value is None:
            return
        parts = [value[i:i + CHUNK] for i in range(0, len(value), CHUNK)] or [""]
        for i, part in enumerate(parts):
            self.kr.set_password(self.service, f"{name}.{i}", part)
        self.kr.set_password(self.service, f"{name}.n", str(len(parts)))

    def _get(self, name: str) -> str | None:
        n = self.kr.get_password(self.service, f"{name}.n")
        if n is None:
            return None
        parts = [self.kr.get_password(self.service, f"{name}.{i}") for i in range(int(n))]
        return None if any(p is None for p in parts) else "".join(parts)

    def _delete(self, name: str) -> None:
        n = self.kr.get_password(self.service, f"{name}.n")
        if n is None:
            return
        for i in range(int(n)):
            try:
                self.kr.delete_password(self.service, f"{name}.{i}")
            except Exception:
                pass
        try:
            self.kr.delete_password(self.service, f"{name}.n")
        except Exception:
            pass

    def clear(self) -> None:
        self._delete("tokens")
        self._delete("client")

    # mcp.client.auth.TokenStorage protocol
    expires_at: float | None = None      # of the tokens last read; see _dump_tokens

    async def get_tokens(self):
        tokens, self.expires_at = _load_tokens(self._get("tokens"))
        return tokens

    async def set_tokens(self, tokens, expires_at: float | None = None) -> None:
        self._put("tokens", _dump_tokens(tokens, expires_at))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        raw = self._get("client")
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        self._put("client", client_info.model_dump_json(exclude_none=True))


# ── Browser sign-in ────────────────────────────────────────────────────────────

def _wait_for_redirect(timeout: float) -> dict:
    """Serve exactly one request on 127.0.0.1:CALLBACK_PORT/callback and return its query."""
    result: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            if url.path != "/callback":
                self.send_response(404); self.end_headers(); return
            result.update({k: v[0] for k, v in parse_qs(url.query).items()})
            ok = "code" in result
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = ("Signed in. You can close this tab and go back to the terminal."
                   if ok else f"Sign-in failed: {result.get('error_description') or result.get('error') or 'no code'}")
            self.wfile.write(f"<p style='font-family:sans-serif'>{msg}</p>".encode())
            done.set()

        def log_message(self, *a):   # keep the code out of the console
            pass

    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), Handler)
    server.timeout = 1
    try:
        waited = 0.0
        while not done.is_set() and waited < timeout:
            server.handle_request()
            waited += server.timeout
    finally:
        server.server_close()
    if not done.is_set():
        raise TimeoutError(f"no sign-in redirect within {int(timeout)}s")
    return result


async def _open_browser(auth_url: str) -> None:
    print("\nOpening Robinhood sign-in in your browser. If it doesn't open, paste this URL:\n")
    print(f"  {auth_url}\n")
    print("Sign in, approve on your phone if asked, and wait for 'Signed in'.")
    webbrowser.open(auth_url)


async def _receive_code():
    from mcp.shared.auth import AuthorizationCodeResult
    q = await asyncio.to_thread(_wait_for_redirect, LOGIN_TIMEOUT_S)
    if "code" not in q:
        raise RuntimeError(f"sign-in failed: {q.get('error_description') or q.get('error') or 'no code returned'}")
    return AuthorizationCodeResult(code=q["code"], state=q.get("state"), iss=q.get("iss"))


async def _no_browser(_url: str) -> None:
    raise NotSignedIn("the stored session expired or was revoked: run `python rh_client.py login` again")


async def _no_code():
    raise NotSignedIn("not signed in: run `python rh_client.py login` first")


# ── Session ────────────────────────────────────────────────────────────────────

class ReadOnlyRobinhood:
    """Read-only handle on an open session. Get one from open_robinhood()."""

    def __init__(self, session):
        self.session = session

    async def call(self, name: str, args: dict | None = None):
        guard_call(name, args)
        res = await self.session.call_tool(name, args or {})
        if getattr(res, "isError", False):
            raise RuntimeError(f"{name} failed: {_text(res)[:300]}")
        if getattr(res, "structuredContent", None):
            return res.structuredContent
        txt = _text(res)
        try:
            return json.loads(txt)
        except ValueError:
            return txt

    async def tool_names(self) -> list[str]:
        names, cursor = [], None
        while True:
            from mcp import types
            r = await self.session.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
            names += [t.name for t in r.tools]
            cursor = getattr(r, "nextCursor", None)
            if not cursor:
                return sorted(names)


_PROVIDER = None


def refreshing_provider_class():
    """OAuthClientProvider that uses the refresh token before giving up.

    Stock mcp (2.3) forgets token expiry across processes, so a stored access token
    always looks valid; and on a 401 it goes straight to a full browser sign-in,
    never trying the refresh token. In CI that meant every job died the moment
    Robinhood dropped the access token (2026-10-22). This subclass restores the
    stored expiry (so an expired token is refreshed up front, by the stock flow) and,
    when a token we believed valid gets a 401, marks it expired and lets the stock
    flow refresh and retry. Either way it first discovers the token endpoint, which
    the stock refresh would otherwise guess wrong. Only if the refresh itself fails does it reach the
    sign-in, which raises NotSignedIn outside `login`."""
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.auth.utils import (build_oauth_authorization_server_metadata_discovery_urls,
                                       create_oauth_metadata_request, handle_auth_metadata_response)

    class RefreshingProvider(OAuthClientProvider):
        async def _initialize(self) -> None:
            await super()._initialize()
            self.context.token_expiry_time = getattr(self.context.storage, "expires_at", None)

        async def _auth_flow(self, request):
            async with self.context.lock:
                if not self._initialized:
                    await self._initialize()
                probe = self.context.is_token_valid() and self.context.can_refresh_token()
                if probe:
                    self._add_auth_header(request)
            if probe:
                response = yield request
                if response.status_code not in (401, 403):
                    return
                request.headers.pop("Authorization", None)  # the stock flow sets it again if a token survives
                if response.status_code == 401:
                    self.context.token_expiry_time = 1.0    # rejected: expired as far as the stock flow knows
            if (not self.context.is_token_valid() and self.context.can_refresh_token()
                    and self.context.oauth_metadata is None):
                # A fresh process has no metadata, and the stock refresh then guesses
                # <server>/token, which 404s: Robinhood's is api.robinhood.com/oauth2/token/.
                issuer = getattr(self.context.client_info, "issuer", None) or self.context.server_url
                for url in build_oauth_authorization_server_metadata_discovery_urls(issuer, self.context.server_url):
                    ok, asm = await handle_auth_metadata_response((yield create_oauth_metadata_request(url)))
                    if asm or not ok:
                        self.context.oauth_metadata = asm
                        break
            flow = super()._auth_flow(request)                # refreshes, then retries the request
            try:
                outgoing = await flow.__anext__()
                while True:
                    outgoing = await flow.asend((yield outgoing))
            except StopAsyncIteration:
                return
            finally:
                await flow.aclose()

    _PROVIDER = RefreshingProvider
    return _PROVIDER


@asynccontextmanager
async def open_robinhood(interactive: bool = False, store=None, handle=None):
    """async with open_robinhood() as rh: await rh.call("get_accounts")

    Non-interactive callers fail fast when nothing is stored instead of
    starting a browser sign-in nobody is there to finish. handle: the class that
    wraps the session (default ReadOnlyRobinhood; rh_trader passes its own)."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    from mcp.shared.auth import OAuthClientMetadata

    store = store or default_store()
    if not interactive and await store.get_tokens() is None:
        raise NotSignedIn("not signed in: run `python rh_client.py login` first")
    provider = refreshing_provider_class()(
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Orblex Industries (read-only spike)",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope="internal",
        ),
        storage=store,
        redirect_handler=_open_browser if interactive else _no_browser,
        callback_handler=_receive_code if interactive else _no_code,
    )
    async with create_mcp_http_client(auth=provider) as http:
        async with streamable_http_client(SERVER_URL, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield (handle or ReadOnlyRobinhood)(session)


def _text(res) -> str:
    return "".join(getattr(c, "text", "") for c in (res.content or []))


# ── Commands ───────────────────────────────────────────────────────────────────

def _data(x) -> dict:
    return x.get("data", x) if isinstance(x, dict) else {}


async def cmd_check(rh: ReadOnlyRobinhood) -> int:
    accts = _data(await rh.call("get_accounts")).get("accounts", [])
    ours = [a for a in accts if str(a.get("account_number")) == AGENTIC_ACCOUNT]
    if not ours or not ours[0].get("agentic_allowed"):
        print(f"FAIL: agentic account {mask(AGENTIC_ACCOUNT)} not visible as tradable to this client")
        return 1
    print(f"OK  account {mask(AGENTIC_ACCOUNT)} ({ours[0].get('nickname') or 'agentic'}, {ours[0].get('type')}) visible")
    pf = _data(await rh.call("get_portfolio", {"account_number": AGENTIC_ACCOUNT}))
    flat = json.dumps(pf)
    for key in ("equity", "total_equity", "portfolio_equity", "buying_power"):
        if f'"{key}"' in flat:
            print(f"OK  portfolio read ({key} present)")
            break
    else:
        print("OK  portfolio read")
    st = _data(await rh.call("get_trade_approval_setting", {"account_number": AGENTIC_ACCOUNT})).get("setting", {})
    print(f"OK  approval setting: human_must_approve_trades={st.get('human_must_approve_trades')}")
    since = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    od = _data(await rh.call("get_equity_orders", {"account_number": AGENTIC_ACCOUNT, "created_at_gte": since}))
    orders = od.get("orders") or od.get("results") or []
    print(f"OK  orders read ({len(orders)} in the last 3 days)")
    print("\nPhase 0 spike PASSED: our own program can sign in and read the agentic account.")
    return 0


async def cmd_tools(rh: ReadOnlyRobinhood) -> int:
    names = await rh.tool_names()
    for n in names:
        print(f"  {'read (allowed)' if n in READ_TOOLS else 'refused       '}  {n}")
    print(f"\n{len(names)} tools on the server; rh_client allows {len(READ_TOOLS & set(names))}.")
    return 0


async def main_async(argv=None) -> int:
    p = argparse.ArgumentParser(description="Robinhood agentic MCP: read-only sign-in spike")
    p.add_argument("command", choices=["login", "check", "tools", "logout", "export-token"])
    p.add_argument("--verbose", action="store_true", help="show the MCP SDK's own logging")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.CRITICAL)

    if args.command == "export-token":
        # Copy this PC's keyring session into the encrypted Supabase store (needs RH_TOKEN_KEY).
        src, dst = KeyringStore(), SupabaseStore()
        tokens, client = await src.get_tokens(), await src.get_client_info()
        if not tokens or not client:
            raise NotSignedIn("no session in this PC's keyring: run `rh_client.py login` first")
        await dst.set_client_info(client)
        await dst.set_tokens(tokens, expires_at=src.expires_at)   # keep the real expiry, not now + expires_in
        print("Session copied to the encrypted Supabase store. Set RH_TOKEN_STORE=supabase to use it.")
        return 0
    if args.command == "logout":
        KeyringStore().clear()
        print("Stored Robinhood tokens and client registration deleted.")
        return 0
    async with open_robinhood(interactive=args.command == "login") as rh:
        if args.command == "tools":
            return await cmd_tools(rh)
        return await cmd_check(rh)


def main() -> int:
    try:
        return asyncio.run(main_async())
    except BaseException as e:   # surface the root cause plainly; never print tokens
        root = e
        while isinstance(root, BaseExceptionGroup) and root.exceptions:
            root = next((x for x in root.exceptions if not isinstance(x, GeneratorExit)), root.exceptions[0])
        if isinstance(root, KeyboardInterrupt):
            return 130
        if isinstance(root, NotAllowed):
            print(f"REFUSED: {root}")
            return 2
        if isinstance(root, NotSignedIn):
            print(f"NOT SIGNED IN: {root}")
        else:
            print(f"FAILED: {type(root).__name__}: {root}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
