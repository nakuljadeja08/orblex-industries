"""
playground.py — the server function behind the demo's PLAYGROUND tab: POST /api/playground.

It lives in deploy/vercel-api/ in the private repo, so the private dashboard's Vercel project never
deploys it, and scripts/export_public.py puts it at api/playground.py in the public repo, whose Vercel
project serves the demo.

No account and no database: the visitor's own Claude key pays for the run, and their recent runs
are kept in their own browser.

  1. the visitor's Claude key arrives in the X-Claude-Key header, is used for this one run
     (../playground.py), and is never stored, logged or returned
  2. the scenario is size-checked and clamped by playground.run, then the result goes straight back
"""

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MAX_BODY = 20_000
KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,}$")


def serve(claude_key: str, body: bytes, run=None) -> tuple[int, dict]:
    """(status, JSON body). run is injectable for tests."""
    if not KEY_RE.match(claude_key or ""):
        return 400, {"error": "Enter a Claude API key. It starts with sk-ant-."}
    if len(body) > MAX_BODY:
        return 400, {"error": "That scenario is too large."}
    try:
        scenario = json.loads(body or b"{}")
        assert isinstance(scenario, dict)
    except (ValueError, AssertionError):
        return 400, {"error": "The scenario couldn't be read."}

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
    return 200, {"result": result}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(n, MAX_BODY + 1))
        status, payload = serve(self.headers.get("X-Claude-Key") or "", body)
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
