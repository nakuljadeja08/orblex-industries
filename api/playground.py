"""
playground.py — the server function behind the demo's PLAYGROUND tab: POST /api/playground.

It lives in deploy/vercel-api/ in the private repo, so the private dashboard's Vercel project never
deploys it, and scripts/export_public.py puts it at api/playground.py in the public repo, whose Vercel
project serves the demo.

  1. the caller must be signed in to the playground's own Supabase project (not the trading one)
  2. at most PLAYGROUND_DAILY_RUNS runs per account per 24 hours (default 20): Claude usage is on the
     visitor's key, but the server time is yours
  3. the visitor's Claude key arrives in the X-Claude-Key header, is used for this one run
     (../playground.py), and is never stored, logged or returned
  4. the scenario and result are saved to playground_runs, which only that account can read

Environment (the demo's Vercel project):
  PLAYGROUND_SUPABASE_URL           the playground project's URL (or VITE_PLAYGROUND_SUPABASE_URL)
  VITE_PLAYGROUND_SUPABASE_ANON_KEY its public key, also built into the page
  PLAYGROUND_SUPABASE_SERVICE_KEY   its service key: server only, never VITE_
  PLAYGROUND_DAILY_RUNS             optional, default 20
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MAX_BODY = 20_000
KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,}$")


def _config():
    url = (os.environ.get("PLAYGROUND_SUPABASE_URL") or os.environ.get("VITE_PLAYGROUND_SUPABASE_URL") or "").rstrip("/")
    return url, os.environ.get("VITE_PLAYGROUND_SUPABASE_ANON_KEY", ""), os.environ.get("PLAYGROUND_SUPABASE_SERVICE_KEY", ""), \
        int(os.environ.get("PLAYGROUND_DAILY_RUNS", "20"))


def serve(auth: str, claude_key: str, body: bytes, http=None, run=None) -> tuple[int, dict]:
    """(status, JSON body). http and run are injectable for tests."""
    import requests
    http = http or requests
    url, anon, service, daily = _config()
    if not (url and anon and service):
        return 503, {"error": "The playground isn't set up on this site yet."}

    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token:
        return 401, {"error": "Sign in to run the playground."}
    r = http.get(f"{url}/auth/v1/user", headers={"apikey": anon, "Authorization": f"Bearer {token}"}, timeout=10)
    if r.status_code != 200:
        return 401, {"error": "Your session expired. Sign in again."}
    uid = r.json().get("id")

    if not KEY_RE.match(claude_key or ""):
        return 400, {"error": "Enter a Claude API key. It starts with sk-ant-."}
    if len(body) > MAX_BODY:
        return 400, {"error": "That scenario is too large."}
    try:
        scenario = json.loads(body or b"{}")
        assert isinstance(scenario, dict)
    except (ValueError, AssertionError):
        return 400, {"error": "The scenario couldn't be read."}

    admin = {"apikey": service, "Authorization": f"Bearer {service}"}
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    used = http.get(f"{url}/rest/v1/playground_runs", headers=admin, timeout=10,
                    params={"select": "id", "user_id": f"eq.{uid}", "created_at": f"gte.{since}", "limit": str(daily)})
    if used.status_code != 200:
        return 503, {"error": "The playground's database isn't reachable. Try again in a minute."}
    if len(used.json()) >= daily:
        return 429, {"error": f"You've used today's {daily} runs. Try again tomorrow."}

    import anthropic
    import playground
    try:
        result = (run or (lambda s: playground.run(s, playground.claude_analyst(claude_key))))(scenario)
    except anthropic.AuthenticationError:
        return 400, {"error": "Claude didn't accept that key. Check it in the Claude Console."}
    except anthropic.PermissionDeniedError:
        return 400, {"error": "That key isn't allowed to use this model."}
    except anthropic.RateLimitError:
        return 429, {"error": "Your Claude account hit its rate limit. Wait a minute and try again."}
    except anthropic.BadRequestError:
        return 400, {"error": "Claude refused the request. Check your Claude account has credit."}
    except anthropic.APIError:
        return 502, {"error": "Claude didn't answer. Try again in a minute."}
    except playground.ScenarioError as e:
        return 422, {"error": str(e)}

    saved = http.post(f"{url}/rest/v1/playground_runs", timeout=10,
                      headers={**admin, "Content-Type": "application/json", "Prefer": "return=representation"},
                      data=json.dumps({"user_id": uid, "scenario": result["scenario"], "result": result}, default=str))
    run_id = saved.json()[0]["id"] if saved.status_code in (200, 201) else None
    return 200, {"id": run_id, "result": result}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(n, MAX_BODY + 1))
        status, payload = serve(self.headers.get("Authorization") or "", self.headers.get("X-Claude-Key") or "", body)
        out = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.end_headers()

    def log_message(self, *args):
        pass                                        # never write request details (or the key header) to logs
