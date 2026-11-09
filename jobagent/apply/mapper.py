"""Ask Claude to map extracted form fields to values from the profile + answer bank."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from .. import config, llm

LEARNED_PATH = config.DATA_DIR / "learned_answers.yaml"

MAP_SYSTEM = f"""You fill job application forms for one candidate. You receive the form's fields
(id, kind, question, options, current value, required) plus the candidate's PROFILE,
ANSWER BANK, LEARNED ANSWERS and the JOB.

For each field decide the value to enter. Output JSON:
{{"fields": [{{"id": "...", "value": ..., "source": "profile|answers|learned|generated|default",
              "needs_review": bool, "note": "optional, short"}}]}}

Value format by kind:
- text/email/tel/url/number/textarea/date/combobox/listbox_button: a string (date as YYYY-MM-DD unless
  the placeholder shows another format — then follow the placeholder).
- select / radio / listbox_button with options: copy one option label EXACTLY from the options list.
- checkbox: true or false.  checkbox_group: list of option labels to tick.
- file: "RESUME" for resume/CV uploads, "COVER_LETTER" for cover letter uploads, null for anything else.

Rules:
- Skip fields whose current value is already correct (omit them from the output).
- If the answer isn't supported by RESUME, PROFILE, ANSWER BANK or LEARNED ANSWERS, output value null and
  needs_review true. Never guess personal facts (dates, grades, salary, IDs, references).
- Open-ended motivation questions ("why us", "why this role", "anything else"): write a concise,
  specific answer (60-120 words) from the job + candidate; source "generated", needs_review true.
- Phone fields: if a separate country-code field exists, put the national number in the phone field.
- Salary fields: use expected CTC for "expected"; current CTC for "current". Never the minimum.
  Respect the units the form asks for (e.g. LPA vs annual INR — 1 LPA = 100000 INR; monthly vs annual).
- EEO/diversity/veteran/disability questions: choose the option closest to the profile's eeo.default_answer.
- Consent checkboxes required to submit (privacy policy, data processing, accuracy declaration): true,
  needs_review true. Marketing/newsletter/talent-community opt-ins: false.
- "How did you hear about us": use the answer bank; for LinkedIn/Naukri sources say that site.
- Location/city fields: profile current city unless the question asks about preference.
- Respect max_length."""


def load_learned() -> list[dict]:
    if LEARNED_PATH.exists():
        return yaml.safe_load(LEARNED_PATH.read_text()) or []
    return []


def save_learned(pairs: list[dict]):
    existing = {p["q"].strip().lower(): p for p in load_learned()}
    for p in pairs:
        if p.get("q") and p.get("a") not in (None, ""):
            existing[p["q"].strip().lower()] = {"q": p["q"].strip(), "a": p["a"]}
    LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEARNED_PATH.write_text(yaml.safe_dump(list(existing.values()), sort_keys=False, allow_unicode=True))


def _profile_for_forms() -> dict:
    p = json.loads(json.dumps(config.profile()))
    p.get("compensation", {}).pop("minimum", None)   # never leak into forms
    return p


def map_fields(fields: list[dict], job, source: str = "company") -> list[dict]:
    if not fields:
        return []
    user = f"""RESUME (source of truth for experience claims)
{config.resume_text()[:9000]}
PROFILE
{yaml.safe_dump(_profile_for_forms(), sort_keys=False, allow_unicode=True)}
ANSWER BANK
{yaml.safe_dump(config.answers(), sort_keys=False, allow_unicode=True)}
LEARNED ANSWERS (from past forms, verified by the candidate)
{yaml.safe_dump(load_learned()[-150:], sort_keys=False, allow_unicode=True)}
JOB
source: {source}
{job['title']} at {job['company']} ({job['location'] or ''})
{(job['description'] or '')[:5000]}

FORM FIELDS
{json.dumps(fields, ensure_ascii=False, indent=1)}"""
    res = llm.ask_json(MAP_SYSTEM + "\n" + llm.guardrails(), user, model=config.SMART_MODEL, max_tokens=8000)
    return res.get("fields", []) if isinstance(res, dict) else res
