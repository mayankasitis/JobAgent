"""Thin Claude wrapper that returns parsed JSON."""
from __future__ import annotations

import json
import os
import re

from . import config

_client = None

BASE_GUARDRAILS = """Hard rules about the candidate (never violate):
- The RESUME is the source of truth. Never claim skills, numbers, employers, titles, dates, degrees or
  certifications that are not in the resume, profile or answer bank. Don't inflate the resume's figures.
- Never type the candidate's minimum/walk-away salary into a form."""


def guardrails() -> str:
    """Base rules + the user's own rules from profile.yaml → guardrails: [...]"""
    extra = config.profile().get("guardrails") or []
    return BASE_GUARDRAILS + "".join(f"\n- {r}" for r in extra)


GUARDRAILS = BASE_GUARDRAILS  # kept for imports; prompts call guardrails()


def client():
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY missing — put it in job-agent/.env")
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _extract_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError(f"No JSON in model output: {text[:200]}")
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start:end + 1])


def ask_json(system: str, user: str, model: str | None = None, max_tokens=4000):
    model = model or config.SMART_MODEL
    msg = client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system + "\n\nRespond with JSON only, no prose.",
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    try:
        return _extract_json(text)
    except (ValueError, json.JSONDecodeError):
        # one repair attempt
        fix = client().messages.create(
            model=model, max_tokens=max_tokens,
            system="Return the user's content as strictly valid JSON. Output JSON only.",
            messages=[{"role": "user", "content": text}],
        )
        return _extract_json("".join(b.text for b in fix.content if getattr(b, "type", "") == "text"))


def ask_text(system: str, user: str, model: str | None = None, max_tokens=1500) -> str:
    msg = client().messages.create(
        model=model or config.SMART_MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
