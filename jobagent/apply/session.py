"""Interactive, human-in-the-loop application session.

The agent fills; YOU review and click Submit. Nothing is ever submitted automatically.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .. import config, db, scoring
from . import filler, mapper

APPLY_BUTTON = re.compile(r"^\s*(apply( now| for this job| to this job)?|i'?m interested|easy apply|start application)\s*$", re.I)

HELP = """
  [Enter] fill the current page (use after every 'Next' in multi-step forms)
  [a]     click the page's Apply button, then fill
  [o]     re-fill everything again (incl. re-uploading files)
  [l]     learn: save your manual answers on this page to the answer bank
  [d]     done — I clicked Submit (marks the job applied)
  [s]     skip this job      [q] stop for now (job stays in queue)
"""


def _out_dir(job) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", job["id"])[:120]
    d = config.OUTPUT_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _render_pdf(pw, text: str, path: Path, title: str):
    """Cover letter → PDF using a throwaway headless Chromium (page.pdf needs headless)."""
    import html
    paras = "".join(f"<p>{html.escape(p)}</p>" for p in text.split("\n\n") if p.strip())
    p = config.profile()["personal"]
    doc = f"""<html><head><meta charset="utf-8"><style>
      body{{font:11pt/1.5 -apple-system,Inter,Helvetica,Arial,sans-serif;margin:56px 64px;color:#111}}
      h1{{font-size:16pt;margin:0 0 2px}} .meta{{color:#555;font-size:9.5pt;margin-bottom:28px}}
    </style></head><body><h1>{html.escape(p['full_name'])}</h1>
    <div class="meta">{html.escape(p.get('email',''))} · {html.escape(p.get('phone',''))}</div>
    <div class="meta">Re: {html.escape(title)}</div>{paras}</body></html>"""
    b = pw.chromium.launch()
    pg = b.new_page()
    pg.set_content(doc)
    pg.pdf(path=str(path), format="A4")
    b.close()


def prepare(pw, job) -> dict:
    """Resume variant + cover letter (txt + pdf). Cached per job."""
    out = _out_dir(job)
    files = {}
    resume = config.resume_path(job["variant"])
    if resume:
        dst = out / resume.name
        if not dst.exists():
            shutil.copy(resume, dst)
        files["RESUME"] = dst
    else:
        print("  ⚠ no resume file found — add PDFs to resumes/ (see profile.yaml resume_variants)")
    cl_txt = out / "cover_letter.txt"
    if not cl_txt.exists():
        print("  … drafting cover letter")
        cl_txt.write_text(scoring.cover_letter(job))
    cl_pdf = out / "Cover_Letter.pdf"
    if not cl_pdf.exists():
        _render_pdf(pw, cl_txt.read_text(), cl_pdf, f"{job['title']} — {job['company']}")
    files["COVER_LETTER"] = cl_pdf
    files["COVER_LETTER_TEXT"] = cl_txt.read_text()
    return files


def launch(pw):
    config.BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    kw = dict(user_data_dir=str(config.BROWSER_PROFILE), headless=False,
              viewport=None, args=["--start-maximized"])
    try:   # real Chrome if installed (better with SSO/Google sign-in), else bundled Chromium
        return pw.chromium.launch_persistent_context(channel="chrome", **kw)
    except Exception:
        return pw.chromium.launch_persistent_context(**kw)


def click_apply(page) -> bool:
    for frame in page.frames:
        for role in ("button", "link"):
            loc = frame.get_by_role(role, name=APPLY_BUTTON)
            if loc.count():
                loc.first.click()
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1500)
                return True
    return False


def _print_report(r):
    print(f"\n  ✓ filled {len(r['filled'])} of {r['fields']} fields")
    for q, v, note in r["review"]:
        print(f"  🟧 REVIEW  {q[:70]}\n            → {str(v)[:160]}" + (f"  ({note})" if note else ""))
    for q, note in r["missing"]:
        print(f"  🟥 MISSING {q[:70]}" + (f"  ({note})" if note else ""))
    for q, v, why in r["failed"]:
        print(f"  ⚠ FAILED  {q[:70]} ← {str(v)[:60]}  [{why}]")


def run(conn, job, auto_fill=True):
    from playwright.sync_api import sync_playwright

    cap = config.criteria().get("daily_caps", {}).get(job["source"])
    if cap is not None and db.applied_today(conn, job["source"]) >= cap:
        print(f"Daily cap reached for {job['source']} ({cap}). Try tomorrow or raise daily_caps.")
        return "capped"

    print(f"\n━━ {job['title']} — {job['company']}  [{job['score'] or '–'}]  {job['location'] or ''}")
    if job["verdict"]:
        print(f"   {job['verdict']}")
    print(f"   {job['apply_url']}")
    db.update(conn, job["id"], status="in_progress")

    with sync_playwright() as pw:
        files = prepare(pw, job)
        out = _out_dir(job)
        ctx = launch(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(job["apply_url"], wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        def mapping_fn(fields):
            print(f"  … mapping {len(fields)} fields with Claude")
            res = mapper.map_fields(fields, job, job["source"])
            (out / f"mapping_{datetime.now():%H%M%S}.json").write_text(
                json.dumps({"fields": fields, "mapping": res}, indent=1, ensure_ascii=False))
            return res

        def do_fill(overwrite=False):
            r = filler.fill_page(page, mapping_fn, files, overwrite=overwrite)
            filler.banner(page, r)
            _print_report(r)
            page.screenshot(path=str(out / f"filled_{datetime.now():%H%M%S}.png"), full_page=True)

        if auto_fill:
            fields, _ = filler.extract(page)
            if len([f for f in fields if f["kind"] != "file"]) < 3:
                click_apply(page)
            do_fill()
        print(HELP)

        result = "later"
        while True:
            try:
                cmd = input("  jobagent> ").strip().lower()
            except EOFError:
                cmd = "q"
            if page.is_closed():
                page = ctx.pages[-1] if ctx.pages else ctx.new_page()
            else:
                page = ctx.pages[-1]  # follow new tabs opened by Apply buttons
            if cmd == "":
                do_fill()
            elif cmd == "a":
                print("  clicked Apply" if click_apply(page) else "  no Apply button found — click it yourself, then Enter")
                do_fill()
            elif cmd == "o":
                do_fill(overwrite=True)
            elif cmd == "l":
                pairs = filler.read_answers(page)
                mapper.save_learned(pairs)
                print(f"  saved {len(pairs)} answers to {mapper.LEARNED_PATH.name}")
            elif cmd == "d":
                db.update(conn, job["id"], status="applied",
                          applied_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                print("  ✔ marked applied")
                result = "applied"
                break
            elif cmd == "s":
                db.update(conn, job["id"], status="skipped")
                result = "skipped"
                break
            elif cmd == "q":
                db.update(conn, job["id"], status="shortlisted")
                break
            else:
                print(HELP)
        ctx.close()
    return result
