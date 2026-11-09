"""jobagent CLI.  Run:  python -m jobagent <command>"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, db, discovery, scoring


def cmd_doctor(a, conn):
    todos = config.todo_fields()
    print(f"config dir : {config.CONFIG_DIR}")
    print(f"database   : {config.DB_PATH}")
    print(f"browser    : {config.BROWSER_PROFILE}")
    import os
    print(f"API key    : {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'MISSING (.env)'}")
    print(f"models     : fast={config.FAST_MODEL} smart={config.SMART_MODEL}")
    p = config.profile()
    for k, rel in (p.get("resume_variants") or {}).items():
        print(f"resume {k:<8}: {'ok' if (config.ROOT / rel).exists() else 'MISSING'}  {rel}")
    print(f"companies  : {len(config.companies())} enabled")
    if str(p.get("personal", {}).get("email", "")).endswith("example.com"):
        print("\n⚠ config still holds the example profile — edit config/profile.yaml")
    if not config.resume_text() or config.resume_text().startswith("Paste the plain text"):
        print("⚠ config/resume.txt is empty — paste your resume text into it")
    if todos:
        print(f"\n{len(todos)} config values still TODO:")
        for t in todos:
            print("  -", t)


def cmd_detect(a, conn):
    hit = discovery.detect_ats_from_page(a.url)
    if not hit:
        print("Couldn't detect the ATS. Open a single job on their careers page and try that URL.")
        return
    ats, slug = hit
    print(f"ATS: {ats}   id: {slug}\n\nAdd to config/companies.yaml:")
    if ats in discovery.FETCHERS:
        key = "url" if ats == "workday" else "slug"
        val = f"https://{slug}" if ats == "workday" else slug
        print(f"  - name: <Company>\n    ats: {ats}\n    {key}: {val}")
    else:
        print(f"  - name: <Company>\n    ats: manual\n    url: {a.url}\n"
              f"  ({ats} has no public feed — add jobs with `add-url`; form filling still works)")


def cmd_verify(a, conn):
    if not config.companies():
        print("No enabled companies in config/companies.yaml — add some (see detect-ats).")
        return
    failed = []
    for name, status, detail in discovery.verify_companies():
        icon = {"ok": "✓", "error": "✗", "skip": "–"}[status]
        print(f"  {icon} {name:<32} {detail}", flush=True)
        if status == "error":
            failed.append(name)
    if failed and getattr(a, "disable_failed", False):
        n = _disable_companies(failed)
        print(f"\ndisabled {n} failing companies in companies.yaml (fix the slug/url and set enabled: true)")


def _disable_companies(names) -> int:
    import re
    path = config.CONFIG_DIR / "companies.yaml"
    lines, n = path.read_text().splitlines(keepends=True), 0
    for i, line in enumerate(lines):
        m = re.match(r"(\s*-\s*\{)\s*name:\s*([^,}]+)", line)
        if m and m.group(2).strip() in names and "enabled:" not in line:
            lines[i] = line.replace(m.group(1), m.group(1) + "enabled: false, ", 1)
            n += 1
    path.write_text("".join(lines))
    return n


def cmd_discover(a, conn):
    s = discovery.run(conn, only=a.company)
    print(f"\n{s['companies']} companies, {s['fetched']} jobs fetched, {s['new']} new, {s['kept']} passed filters")


def cmd_refilter(a, conn):
    """Re-apply criteria.yaml to already-fetched jobs (no re-download)."""
    moved_in = moved_out = 0
    for r in conn.execute("SELECT * FROM jobs WHERE status IN ('found','filtered_out')").fetchall():
        reason = discovery.prefilter(dict(r))
        new = "filtered_out" if reason else "found"
        if new != r["status"]:
            conn.execute("UPDATE jobs SET status=? WHERE id=?", (new, r["id"]))
            moved_in += new == "found"
            moved_out += new == "filtered_out"
    conn.commit()
    print(f"{moved_in} jobs now pass filters, {moved_out} newly filtered out. Run `score` next.")


def cmd_score(a, conn):
    s = scoring.run(conn, limit=a.limit)
    print(f"\nscored {s['scored']}, shortlisted {s['shortlisted']}")


def cmd_daily(a, conn):
    cmd_discover(argparse.Namespace(company=None), conn)
    cmd_score(argparse.Namespace(limit=200), conn)
    q = db.by_status(conn, "shortlisted")
    print(f"\n{len(q)} jobs in your apply queue — run: python -m jobagent apply --next")
    _notify(f"{len(q)} jobs waiting in your apply queue")


def _notify(msg):
    import platform
    import subprocess
    if platform.system() == "Darwin":
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "JobAgent"'], check=False)


def cmd_list(a, conn):
    statuses = a.status.split(",") if a.status else ["shortlisted"]
    rows = db.by_status(conn, *statuses, limit=a.limit)
    if not rows:
        print("nothing here")
        return
    for r in rows:
        short = r["id"].rsplit(":", 1)[-1][:12]
        print(f"{(r['score'] or 0):>3}  {r['status']:<12} {r['title'][:46]:<46} {r['company'][:22]:<22} "
              f"{(r['location'] or '')[:22]:<22} {short}")
        if a.verbose and r["verdict"]:
            print(f"     {r['verdict']}\n     {r['url']}")
    print(f"\n{len(rows)} jobs  (id column = last part, any unique fragment works)")


def cmd_show(a, conn):
    r = db.get(conn, a.id)
    if not r:
        sys.exit("not found")
    for k in r.keys():
        if k == "description":
            continue
        v = r[k]
        if k == "reasons" and v:
            v = json.dumps(json.loads(v), indent=1)
        print(f"{k:>10}: {v}")
    print("\n" + (r["description"] or "")[:3000])


def cmd_add_url(a, conn):
    j = discovery.job_from_url(a.url, title=a.title or "", company=a.company or "")
    if a.description_file:
        j["description"] = Path(a.description_file).read_text()
    j["status"] = "shortlisted" if a.no_score else "found"
    if not db.upsert_job(conn, j):
        print("already tracked:", j["id"])
        return
    conn.commit()
    print(f"added {j['id']}  (source={j['source']}, ats={j['ats']})")
    if not a.no_score and j["description"]:
        scoring.run(conn, limit=1)
    elif not j["description"]:
        db.update(conn, j["id"], status="shortlisted")
        print("no description given → skipped scoring, put straight in the queue")


def cmd_apply(a, conn):
    from .apply import session
    if a.id:
        job = db.get(conn, a.id)
        if not job:
            sys.exit("job not found")
        session.run(conn, job)
        return
    while True:
        q = db.by_status(conn, "shortlisted", limit=1)
        if not q:
            print("queue empty 🎉")
            return
        res = session.run(conn, q[0])
        if res in ("capped",) or res == "later":
            return
        if input("\n  next job? [Y/n] ").strip().lower() == "n":
            return


def cmd_mark(a, conn):
    r = db.get(conn, a.id)
    if not r:
        sys.exit("not found")
    extra = {}
    if a.status == "applied" and not r["applied_at"]:
        extra["applied_at"] = db.now()
    db.update(conn, r["id"], status=a.status, notes=a.note or r["notes"], **extra)
    print(f"{r['title']} @ {r['company']} → {a.status}")


def cmd_stats(a, conn):
    for row in conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status ORDER BY n DESC"):
        print(f"  {row['status']:<18} {row['n']}")
    for src in ("company", "linkedin", "naukri"):
        print(f"  applied today ({src}): {db.applied_today(conn, src)}")


def cmd_export(a, conn):
    n = db.export_csv(conn, Path(a.path))
    print(f"wrote {n} rows to {a.path}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="jobagent", description="Career-page job application agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check config, API key, resumes")
    s = sub.add_parser("detect-ats", help="detect a careers page's ATS"); s.add_argument("url")
    s = sub.add_parser("verify-companies", help="ping every company feed in companies.yaml")
    s.add_argument("--disable-failed", action="store_true", help="set enabled: false on failing entries")
    s = sub.add_parser("discover", help="fetch new jobs"); s.add_argument("--company")
    s = sub.add_parser("score", help="score new jobs with Claude"); s.add_argument("--limit", type=int, default=100)
    sub.add_parser("refilter", help="re-apply criteria.yaml to fetched jobs")
    sub.add_parser("daily", help="discover + score + notify (used by the schedule)")
    s = sub.add_parser("list", help="list jobs (default: shortlisted queue)")
    s.add_argument("--status", help="comma list, e.g. applied,interview"); s.add_argument("--limit", type=int)
    s.add_argument("-v", "--verbose", action="store_true")
    s = sub.add_parser("show", help="show one job"); s.add_argument("id")
    s = sub.add_parser("add-url", help="add a job by URL (LinkedIn, Naukri, Darwinbox, any page)")
    s.add_argument("url"); s.add_argument("--title"); s.add_argument("--company")
    s.add_argument("--description-file", help="text file with the JD (enables scoring)")
    s.add_argument("--no-score", action="store_true")
    s = sub.add_parser("apply", help="fill application forms (you submit)")
    s.add_argument("id", nargs="?"); s.add_argument("--next", action="store_true", help="work through the queue")
    s = sub.add_parser("mark", help="set status"); s.add_argument("id")
    s.add_argument("status", choices=sorted(db.VALID_STATUSES)); s.add_argument("--note")
    sub.add_parser("stats", help="pipeline counts")
    s = sub.add_parser("export", help="export tracker to CSV"); s.add_argument("path", nargs="?", default="jobs.csv")

    a = p.parse_args(argv)
    conn = db.connect()
    handler = {
        "doctor": cmd_doctor, "detect-ats": cmd_detect, "verify-companies": cmd_verify,
        "discover": cmd_discover, "refilter": cmd_refilter, "score": cmd_score, "daily": cmd_daily, "list": cmd_list,
        "show": cmd_show, "add-url": cmd_add_url, "apply": cmd_apply, "mark": cmd_mark,
        "stats": cmd_stats, "export": cmd_export,
    }[a.cmd]
    handler(a, conn)


if __name__ == "__main__":
    main()
