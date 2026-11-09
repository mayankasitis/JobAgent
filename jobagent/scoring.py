"""Score jobs against the candidate profile with Claude, and draft cover notes."""
from __future__ import annotations

import json

import yaml

from . import config, db, llm

SCORE_SYSTEM = """You are a sharp technical recruiter screening jobs for one specific candidate.
Score how well the job fits the candidate AND the candidate's targeting criteria, 0-100.
90+ = near-perfect fit, apply today. 70-89 = good fit. 50-69 = stretch/partial. <50 = skip.
Penalise: roles below the candidate's seniority, roles outside their field, contract roles, roles
needing a stack the candidate lacks as a hard requirement, locations outside the criteria, red flags.
Reward: matches with the candidate's strengths, must-have skills and seniority hints.

Return JSON:
{"score": int, "verdict": "one sentence", "reasons": ["max 4 short bullets"],
 "variant": "<one of the candidate's resume variants>", "red_flags": ["..."], "is_lead_role": bool}"""


def _candidate_brief() -> str:
    p, a = config.profile(), config.answers()
    brief = {
        "experience_years": p.get("employment", {}).get("total_experience_years"),
        "last_role": p.get("employment", {}).get("last_title"),
        "skills": p.get("skills"),
        "summary": a.get("summary"),
        "preferred_locations": p.get("location", {}).get("preferred_locations"),
        "min_salary": p.get("compensation", {}).get("minimum"),
    }
    return yaml.safe_dump(brief, sort_keys=False, allow_unicode=True)


def score_job(job) -> dict:
    cr = config.criteria()
    user = f"""CANDIDATE
{_candidate_brief()}
RESUME
{config.resume_text()[:9000]}
RESUME VARIANTS (pick one): {config.variants()}
CRITERIA
must_have_any: {cr.get('must_have_any')}
red_flags: {cr.get('red_flags')}
seniority_hint: {cr.get('seniority_hint')}
resume_strengths (boost when the job asks for these): {cr.get('resume_strengths')}

JOB
Company: {job['company']}
Title: {job['title']}
Location: {job['location']}
Description:
{(job['description'] or '')[:12000]}"""
    return llm.ask_json(SCORE_SYSTEM, user, model=config.FAST_MODEL, max_tokens=800)


def run(conn, limit=50, verbose=True) -> dict:
    threshold = config.criteria().get("min_score_to_shortlist", 70)
    jobs = db.by_status(conn, "found", order="found_at DESC", limit=limit)
    stats = {"scored": 0, "shortlisted": 0}
    for job in jobs:
        try:
            res = score_job(job)
        except Exception as e:
            if verbose:
                print(f"  ✗ {job['title']} @ {job['company']}: {e}")
            continue
        score = int(res.get("score", 0))
        status = "shortlisted" if score >= threshold else "rejected_by_score"
        db.update(conn, job["id"], score=score, verdict=res.get("verdict"),
                  reasons=json.dumps({k: res.get(k) for k in ("reasons", "red_flags", "is_lead_role")}),
                  variant=res.get("variant") if res.get("variant") in config.variants()
                  else config.profile().get("default_variant"),
                  status=status)
        stats["scored"] += 1
        stats["shortlisted"] += status == "shortlisted"
        if verbose:
            mark = "★" if status == "shortlisted" else "·"
            print(f"  {mark} {score:>3}  {job['title'][:50]:<50} {job['company'][:25]}")
    return stats


COVER_SYSTEM = f"""You write short, specific cover notes for the candidate.
150-220 words, plain text, no headers, no "I am writing to express my interest".
Open with why this role/company specifically, then 2-3 concrete matches between the
job's needs and the candidate's real experience, then one line on availability."""


def cover_letter(job) -> str:
    p, a = config.profile(), config.answers()
    lead = False
    try:
        lead = bool(json.loads(job["reasons"] or "{}").get("is_lead_role"))
    except Exception:
        pass
    user = f"""CANDIDATE
Name: {p['personal']['full_name']}
Resume:
{config.resume_text()[:9000]}
Availability: {a.get('notice_period_text')}
Leadership (use only if lead role = True): {a.get('leadership') if lead else 'n/a'}
Lead role: {lead}

JOB
{job['title']} at {job['company']} ({job['location']})
{(job['description'] or '')[:8000]}"""
    return llm.ask_text(COVER_SYSTEM + "\n" + llm.guardrails(), user)
