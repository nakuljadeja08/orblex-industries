import asyncio
import time

import pytest

from rh_client import AGENTIC_ACCOUNT, READ_TOOLS, KeyringStore, NotAllowed, guard_call, mask


class FakeKeyring:
    def __init__(self):
        self.d = {}

    def set_password(self, svc, key, val):
        self.d[(svc, key)] = val

    def get_password(self, svc, key):
        return self.d.get((svc, key))

    def delete_password(self, svc, key):
        del self.d[(svc, key)]


@pytest.mark.parametrize("tool", [
    "place_equity_order", "cancel_equity_order", "review_equity_order", "place_option_order",
    "place_crypto_order", "exercise_option", "decline_trade_approval", "place_advanced_order",
    "add_to_watchlist", "create_alert",
])
def test_write_tools_refused(tool):
    with pytest.raises(NotAllowed):
        guard_call(tool, {"account_number": AGENTIC_ACCOUNT})


def test_read_tools_allowed_on_agentic_account():
    for tool in READ_TOOLS:
        guard_call(tool, {"account_number": AGENTIC_ACCOUNT})
    guard_call("get_accounts", None)


def test_no_write_tool_in_allowlist():
    assert not any(t.startswith(("place_", "cancel_", "review_", "exercise_", "decline_", "create_",
                                 "update_", "delete_", "add_", "remove_")) for t in READ_TOOLS)


def test_other_accounts_refused_and_main_only_for_weight_reads():
    guard_call("get_portfolio", {"account_number": "900000002"})          # LONG TERM weight snapshot
    guard_call("get_equity_positions", {"account_number": "900000002"})
    for tool in ("get_equity_orders", "get_trade_approvals", "get_trade_approval_setting"):
        with pytest.raises(NotAllowed):
            guard_call(tool, {"account_number": "900000002"})
    with pytest.raises(NotAllowed):
        guard_call("place_equity_order", {"account_number": "900000002"})
    with pytest.raises(NotAllowed):
        guard_call("get_portfolio", {"account_number": "123456789"})


def test_supabase_store_roundtrip_encrypted():
    import asyncio
    from cryptography.fernet import Fernet
    from mcp.shared.auth import OAuthToken
    from rh_client import SupabaseStore

    class Table:
        def __init__(self, db): self.db, self.q = db, {}
        def select(self, *_): return self
        def eq(self, k, v): self.q[k] = v; return self
        def limit(self, _): return self
        def in_(self, k, vs): self.q[k] = vs; return self
        def upsert(self, row, on_conflict=None): self.db[row["name"]] = row; return self
        def delete(self): self.q["_del"] = True; return self
        def execute(self):
            class R: pass
            r = R()
            if self.q.get("_del"):
                for n in self.q.get("name", []): self.db.pop(n, None)
                r.data = []
            else:
                n = self.q.get("name")
                r.data = [self.db[n]] if n in self.db else []
            return r

    class Client:
        def __init__(self): self.db = {}
        def table(self, _): return Table(self.db)

    c = Client()
    st = SupabaseStore(client=c, key=Fernet.generate_key().decode())
    asyncio.run(st.set_tokens(OAuthToken(access_token="secret-access", token_type="Bearer", refresh_token="secret-refresh")))
    stored = c.db["robinhood.tokens"]["ciphertext"]
    assert "secret" not in stored                                         # never stored in the clear
    assert asyncio.run(st.get_tokens()).refresh_token == "secret-refresh"
    other = SupabaseStore(client=c, key=Fernet.generate_key().decode())   # wrong key cannot read it
    with pytest.raises(Exception):
        asyncio.run(other.get_tokens())
    st.clear()
    assert asyncio.run(st.get_tokens()) is None


def test_mask():
    assert mask("900000001") == "••0001"


def test_store_roundtrip_long_values_and_clear():
    from mcp.shared.auth import OAuthToken
    kr = FakeKeyring()
    store = KeyringStore(backend=kr, service="test")
    tok = OAuthToken(access_token="a" * 4500, token_type="Bearer", expires_in=3600, refresh_token="r" * 300)

    asyncio.run(store.set_tokens(tok))
    assert all(len(v) <= 1000 for v in kr.d.values())          # chunked under the vault limit
    back = asyncio.run(store.get_tokens())
    assert back.access_token == tok.access_token and back.refresh_token == tok.refresh_token

    asyncio.run(store.set_tokens(OAuthToken(access_token="short", token_type="Bearer")))
    assert asyncio.run(store.get_tokens()).access_token == "short"
    assert ("test", "tokens.4") not in kr.d                    # old chunks removed on overwrite

    store.clear()
    assert kr.d == {}
    assert asyncio.run(store.get_tokens()) is None


# ── Token refresh (the 2026-10-22 CI outage: a dead access token, never refreshed) ──

def _provider(store):
    from mcp.shared.auth import OAuthClientMetadata
    from rh_client import REDIRECT_URI, SERVER_URL, refreshing_provider_class

    async def no_browser(_url):
        raise AssertionError("must not start a browser sign-in")

    return refreshing_provider_class()(
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata(redirect_uris=[REDIRECT_URI], token_endpoint_auth_method="none"),
        storage=store, redirect_handler=no_browser, callback_handler=None)


def _store(expires_at=None):
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
    from rh_client import REDIRECT_URI
    store = KeyringStore(backend=FakeKeyring(), service="test")
    asyncio.run(store.set_tokens(OAuthToken(access_token="old", token_type="Bearer", expires_in=600,
                                            refresh_token="r1"), expires_at=expires_at))
    asyncio.run(store.set_client_info(OAuthClientInformationFull(client_id="cid", redirect_uris=[REDIRECT_URI])))
    return store


TOKEN_URL = "https://api.robinhood.com/oauth2/token/"     # what Robinhood's metadata advertises
METADATA = {"issuer": "https://agent.robinhood.com/mcp/trading", "token_endpoint": TOKEN_URL,
            "authorization_endpoint": "https://robinhood.com/oauth", "response_types_supported": ["code"]}


def _kind(r):
    if "/.well-known/oauth-authorization-server" in str(r.url):
        return "meta"
    return "refresh" if _is_refresh(r) else "call"


def _drive(provider, replies):
    """Run one request through the auth flow, as the server. Discovery is answered with
    METADATA; everything else by replies(sent_request) -> (status, json). Returns
    (kind, authorization) per request sent, captured at send time: the flow re-sends
    the same Request object with new headers."""
    import httpx2
    from rh_client import SERVER_URL
    req = httpx2.Request("POST", SERVER_URL, content=b"{}")
    sent = []

    async def go():
        flow = provider.async_auth_flow(req)
        out = await flow.__anext__()
        try:
            while True:
                sent.append((_kind(out), out.headers.get("authorization")))
                status, body = (200, METADATA) if _kind(out) == "meta" else replies(out)
                out = await flow.asend(httpx2.Response(status, json=body, request=out))
        except StopAsyncIteration:
            pass

    asyncio.run(go())
    return sent


def _is_refresh(r):
    return str(r.url) == TOKEN_URL and b"grant_type=refresh_token" in r.content


def test_401_on_stored_token_refreshes_and_retries():
    store = _store(expires_at=time.time() + 3600)                   # we think it is still good
    def replies(r):
        if _is_refresh(r):
            return 200, {"access_token": "new", "token_type": "Bearer", "expires_in": 600, "refresh_token": "r2"}
        return (200, {}) if r.headers.get("authorization") == "Bearer new" else (401, {})

    sent = _drive(_provider(store), replies)
    assert sent == [("call", "Bearer old"), ("meta", None), ("refresh", None), ("call", "Bearer new")]
    back = asyncio.run(store.get_tokens())
    assert (back.access_token, back.refresh_token) == ("new", "r2")
    assert store.expires_at and store.expires_at > time.time()       # persisted for the next process


def test_token_without_stored_expiry_still_refreshes_on_401():
    store = _store()
    store._put("tokens", '{"access_token": "old", "token_type": "Bearer", "refresh_token": "r1"}')  # pre-fix format
    def replies(r):
        if _is_refresh(r):
            return 200, {"access_token": "new", "token_type": "Bearer", "expires_in": 600}
        return (200, {}) if r.headers.get("authorization") == "Bearer new" else (401, {})

    _drive(_provider(store), replies)
    back = asyncio.run(store.get_tokens())
    assert (back.access_token, back.refresh_token) == ("new", "r1")   # refresh token carried forward


def test_expired_stored_token_refreshes_before_the_request():
    store = _store(expires_at=time.time() - 60)
    def replies(r):
        if _is_refresh(r):
            return 200, {"access_token": "new", "token_type": "Bearer", "expires_in": 600}
        return 200, {}

    sent = _drive(_provider(store), replies)
    assert sent == [("meta", None), ("refresh", None), ("call", "Bearer new")]   # dead token never sent


def test_valid_token_makes_one_request():
    sent = _drive(_provider(_store(expires_at=time.time() + 3600)), lambda r: (200, {}))
    assert sent == [("call", "Bearer old")]


def test_failed_refresh_ends_in_sign_in_not_a_loop():
    store = _store(expires_at=time.time() + 3600)
    import httpx2
    from rh_client import SERVER_URL
    req = httpx2.Request("POST", SERVER_URL, content=b"{}")

    async def go():
        flow = _provider(store).async_auth_flow(req)
        r = await flow.__anext__()
        r = await flow.asend(httpx2.Response(401, request=r))               # old token rejected
        assert _kind(r) == "meta"
        r = await flow.asend(httpx2.Response(200, json=METADATA, request=r))
        assert _is_refresh(r)
        r = await flow.asend(httpx2.Response(400, json={"error": "invalid_grant"}, request=r))
        assert r is req and "authorization" not in r.headers                # stock flow takes over: re-auth path
        await flow.aclose()

    asyncio.run(go())


def test_export_keeps_real_expiry():
    from mcp.shared.auth import OAuthToken
    store = KeyringStore(backend=FakeKeyring(), service="test")
    asyncio.run(store.set_tokens(OAuthToken(access_token="a", token_type="Bearer", expires_in=600), expires_at=123.0))
    asyncio.run(store.get_tokens())
    assert store.expires_at == 123.0
