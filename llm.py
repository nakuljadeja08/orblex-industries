"""
llm.py — the one place the pipeline calls Claude, so model choice and each
model's request rules live together.

Two routes:
  decision  analyst, risk, allocator, report writer, watchdog — the calls that
            make or check trading decisions. CLAUDE_DECISION_MODEL
            (default claude-sonnet-5-5).
  fast      formulaic summaries — earnings commentary, options-flow signal,
            macro summary, newsletter summary. CLAUDE_FAST_MODEL
            (default claude-haiku-5-5).

Per-model request rules (Claude API):
  claude-sonnet-5-5  CLAUDE_DECISION_THINKING (default "low") runs adaptive
                     thinking at that effort (low|medium|high); "off" sends
                     {"type": "between_tools"} (no extended thinking) at
                     CLAUDE_DECISION_EFFORT. Chosen 2026-10-12 from a 25-ticker
                     analyst A/B on the same contexts: adaptive-low cost $0.32
                     vs $0.37 (off) vs $0.48 (Sonnet 4.6), median 6s vs 17s,
                     25/25 valid JSON, and agreed most with Sonnet 4.6.
                     Server-side refusal fallback is on (beta
                     server-side-fallback-2026-07-01, fallbacks="default").
  claude-haiku-5-5   thinking off at low effort — these are short, formulaic
                     replies; no server-side fallback exists for Haiku.
  anything else      sent as before (no thinking or effort fields), so setting a
                     model env var back to claude-sonnet-4-6 is a clean rollback.

Every reply is read by block type — on 5.5 models a response can start with a
thinking block — and a refusal raises ClaudeRefusal instead of returning text.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def decision_model() -> str:
    return os.getenv("CLAUDE_DECISION_MODEL", "claude-sonnet-5-5")


def fast_model() -> str:
    return os.getenv("CLAUDE_FAST_MODEL", "claude-haiku-5-5")


class ClaudeRefusal(RuntimeError):
    """The model declined (stop_reason == "refusal"); category from stop_details."""

    def __init__(self, model: str, category: Optional[str], explanation: Optional[str]):
        super().__init__(f"{model} declined the request (category: {category or 'unspecified'})")
        self.category, self.explanation = category, explanation


@dataclass
class Reply:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: Optional[str]


def request_params(model: str, route: str) -> dict:
    """The model-specific fields (thinking / effort / fallback) for one request."""
    if model.startswith("claude-sonnet-5-5"):
        mode = os.getenv("CLAUDE_DECISION_THINKING", "low").strip().lower()
        if mode == "off":
            params = {"thinking": {"type": "between_tools"},
                      "output_config": {"effort": os.getenv("CLAUDE_DECISION_EFFORT", "high")}}
        else:
            params = {"thinking": {"type": "adaptive"}, "output_config": {"effort": mode}}
        params["betas"] = [FALLBACK_BETA]
        params["extra_body"] = {"fallbacks": "default"}
        return params
    if model.startswith("claude-haiku-5-5"):
        return {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}
    return {}


def ask(route: str, system: Optional[str], prompt: str, max_tokens: int,
        client: Optional[anthropic.Anthropic] = None, model: Optional[str] = None) -> Reply:
    """One single-turn call. route is "decision" or "fast"."""
    model = model or (decision_model() if route == "decision" else fast_model())
    client = client or anthropic.Anthropic()
    params = request_params(model, route)
    kwargs = dict(model=model, max_tokens=max_tokens,
                  messages=[{"role": "user", "content": prompt}], **params)
    if system:
        kwargs["system"] = system
    msg = (client.beta.messages.create(**kwargs) if "betas" in kwargs
           else client.messages.create(**kwargs))

    if msg.stop_reason == "refusal":
        d = getattr(msg, "stop_details", None)
        raise ClaudeRefusal(msg.model, getattr(d, "category", None), getattr(d, "explanation", None))
    if msg.stop_reason == "max_tokens":
        logger.warning(f"  {msg.model}: reply hit max_tokens={max_tokens} — output may be truncated")
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    return Reply(text=text, model=msg.model, input_tokens=msg.usage.input_tokens,
                 output_tokens=msg.usage.output_tokens, stop_reason=msg.stop_reason)
