"""Job discovery from company career pages via the ATS public job-board APIs.

These are the same unauthenticated JSON endpoints the companies' own
careers pages call — no scraping, no login, no ToS grey zone.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from . import config, db

_QUICK = False   # set by verify_companies: one search page, no detail fetches

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) jobagent/0.1"}
TIMEOUT = 25


# ─────────────────────────── helpers ───────────────────────────

def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def _iso(ts) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):  # epoch ms
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    return str(ts)


def _job(company, ats, ext_id, title, location, url, apply_url, desc, posted):
    return {
        "id": f"{ats}:{re.sub(r'[^a-z0-9]+', '-', company.lower()).strip('-')}:{ext_id}",
        "source": "company",
        "ats": ats,
        "company": company,
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "url": url,
        "apply_url": apply_url or url,
        "description": desc,
        "posted_at": _iso(posted),
    }


def _terms():
    terms = config.criteria().get("search_terms", [""])
    return terms[:1] if _QUICK else terms


# ─────────────────────────── fetchers ───────────────────────────

def fetch_greenhouse(c: dict) -> list[dict]:
    r = requests.get(
        f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs",
        params={"content": "true"}, headers=UA, timeout=TIMEOUT,
    )
    r.raise_for_status()
    return [
        _job(c["name"], "greenhouse", j["id"], j["title"], (j.get("location") or {}).get("name"),
             j["absolute_url"], j["absolute_url"], strip_html(j.get("content")),
             j.get("first_published") or j.get("updated_at"))
        for j in r.json().get("jobs", [])
    ]


def fetch_lever(c: dict) -> list[dict]:
    r = requests.get(f"https://api.lever.co/v0/postings/{c['slug']}",
                     params={"mode": "json"}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        loc = cat.get("location") or ", ".join(cat.get("allLocations") or [])
        desc = j.get("descriptionPlain", "") + "\n\n" + "\n".join(
            f"{l.get('text','')}\n{strip_html(l.get('content'))}" for l in j.get("lists", [])
        ) + "\n\n" + (j.get("additionalPlain") or "")
        if j.get("workplaceType") == "remote":
            loc = f"{loc} (Remote)"
        out.append(_job(c["name"], "lever", j["id"], j.get("text"), loc,
                        j.get("hostedUrl"), j.get("applyUrl"), desc.strip(), j.get("createdAt")))
    return out


def fetch_ashby(c: dict) -> list[dict]:
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}",
                     params={"includeCompensation": "true"}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = j.get("location") or ""
        if j.get("isRemote"):
            loc = f"{loc} (Remote)".strip()
        secondary = ", ".join(s.get("location", "") for s in j.get("secondaryLocations") or [])
        if secondary:
            loc = f"{loc}; {secondary}"
        out.append(_job(c["name"], "ashby", j["id"], j.get("title"), loc, j.get("jobUrl"),
                        j.get("applyUrl"), j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
                        j.get("publishedAt")))
    return out


def fetch_smartrecruiters(c: dict) -> list[dict]:
    out, seen = [], set()
    for term in _terms():
        r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings",
                         params={"q": term, "limit": 100, "country": "in"}, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("content", []):
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            loc = j.get("location") or {}
            loc_s = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
            if loc.get("remote"):
                loc_s += " (Remote)"
            url = f"https://jobs.smartrecruiters.com/{c['slug']}/{j['id']}"
            desc = ""
            stub = _job(c["name"], "smartrecruiters", j["id"], j.get("name"), loc_s, url, url, "", None)
            if prefilter(stub) or _QUICK:
                out.append(stub)
                continue
            try:
                d = requests.get(j["ref"], headers=UA, timeout=TIMEOUT).json()
                secs = (d.get("jobAd") or {}).get("sections") or {}
                desc = "\n\n".join(strip_html((secs.get(k) or {}).get("text"))
                                   for k in ("jobDescription", "qualifications", "additionalInformation"))
            except Exception:
                pass
            out.append(_job(c["name"], "smartrecruiters", j["id"], j.get("name"), loc_s,
                            url, url, desc, j.get("releasedDate")))
    return out


_WD = re.compile(r"https?://(?P<host>[^/]+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/?#]+)")


def fetch_workday(c: dict) -> list[dict]:
    m = _WD.match(c["url"])
    if not m:
        raise ValueError(f"Not a Workday site URL: {c['url']}")
    host, site = m["host"], m["site"]
    tenant = host.split(".")[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    out, seen = [], set()
    for term in _terms():
        offset = 0
        while offset < (20 if _QUICK else 100):  # first 100 hits per term is plenty
            r = requests.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                                   "searchText": term},
                              headers={**UA, "Accept": "application/json"}, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            posts = data.get("jobPostings", [])
            for j in posts:
                path = j.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                desc, posted = "", None
                url = f"https://{host}/{site}{path}"
                stub = _job(c["name"], "workday", path.rsplit("_", 1)[-1], j.get("title"),
                            j.get("locationsText"), url, url + "/apply", "", None)
                if prefilter(stub) or _QUICK:
                    out.append(stub)
                    continue
                try:
                    d = requests.get(f"{api}{path}", headers={**UA, "Accept": "application/json"},
                                     timeout=TIMEOUT).json().get("jobPostingInfo", {})
                    desc, posted = strip_html(d.get("jobDescription")), d.get("startDate")
                except Exception:
                    pass
                out.append(_job(c["name"], "workday", path.rsplit("_", 1)[-1], j.get("title"),
                                j.get("locationsText"), url, url + "/apply", desc, posted))
            offset += 20
            if len(posts) < 20:
                break
            time.sleep(0.5)
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
}


# ─────────────────────────── ATS detection ───────────────────────────

ATS_PATTERNS = [
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)"),
    ("lever", r"jobs\.(?:eu\.)?lever\.co/([\w-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([\w.-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)"),
    ("workday", r"([\w-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[\w-]+)"),
    ("darwinbox", r"([\w-]+)\.darwinbox\.in"),
    ("keka", r"([\w-]+)\.keka\.com/careers"),
    ("zoho_recruit", r"([\w-]+)\.zohorecruit\.(?:com|in)"),
    ("successfactors", r"(career\d*\.successfactors\.(?:com|eu)[^\s]*)"),
    ("icims", r"([\w-]+)\.icims\.com"),
    ("taleo", r"([\w-]+)\.taleo\.net"),
    ("linkedin", r"linkedin\.com/jobs/view/(\d+)"),
    ("naukri", r"naukri\.com/job-listings-([\w-]+)"),
]


def detect_ats(url_or_html: str) -> tuple[str, str] | None:
    for ats, pat in ATS_PATTERNS:
        m = re.search(pat, url_or_html)
        if m:
            return ats, m.group(1)
    return None


def detect_ats_from_page(url: str) -> tuple[str, str] | None:
    """Try the URL itself, then look inside the careers page HTML for an embedded board."""
    hit = detect_ats(url)
    if hit:
        return hit
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        return detect_ats(r.text)
    except Exception:
        return None


# ─────────────────────────── filtering ───────────────────────────

def _has_any(text: str, words) -> bool:
    t = (text or "").lower()
    return any(re.search(rf"(?<![a-z]){re.escape(w.lower())}(?![a-z])", t) for w in words)


FOREIGN = ["us", "usa", "united states", "canada", "uk", "united kingdom", "europe", "emea", "germany",
           "australia", "singapore", "japan", "brazil", "mexico", "ireland", "netherlands", "france",
           "spain", "poland", "israel", "americas", "latam", "can", "hyderabad", "mumbai", "chennai", "gurgaon", "gurugram", "noida", "delhi", "kolkata", "ontario", "london", "berlin", "nc", "ca", "tx", "ny", "wa", "co", "md", "dc"]


def location_ok(loc: str, cr: dict) -> bool:
    """India locations pass. 'Remote' passes only if it is India/APAC-remote or unrestricted."""
    india = [w for w in cr.get("locations_include", []) if w.lower() != "remote"]
    if _has_any(loc, india) or loc.strip().lower() in ("india", "in", "ind"):
        return True
    if "remote" in [w.lower() for w in cr.get("locations_include", [])] and _has_any(loc, ["remote", "anywhere"]):
        if _has_any(loc, ["apac", "asia"]):
            return True
        return not _has_any(loc, FOREIGN)
    return False


def prefilter(job: dict) -> str | None:
    """Return a reason string if the job should be filtered out, else None."""
    cr = config.criteria()
    title = job.get("title", "")
    if cr.get("title_include") and not _has_any(title, cr["title_include"]):
        return "title not in include list"
    if _has_any(title, cr.get("title_exclude", [])):
        return "title excluded"
    loc = job.get("location", "")
    if loc and not location_ok(loc, cr):
        return f"location '{loc}'"
    if _has_any(loc, cr.get("locations_exclude", [])):
        return "location excluded"
    if _has_any(job.get("company", ""), cr.get("company_blacklist", [])):
        return "company blacklisted"
    max_age = cr.get("max_age_days")
    if max_age and job.get("posted_at"):
        try:
            posted = datetime.fromisoformat(job["posted_at"].replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - posted > timedelta(days=max_age):
                return "too old"
        except ValueError:
            pass
    return None


# ─────────────────────────── main entry ───────────────────────────

def run(conn, only: str | None = None, verbose=True) -> dict:
    stats = {"companies": 0, "fetched": 0, "new": 0, "kept": 0, "errors": []}
    for c in config.companies():
        if only and only.lower() not in c["name"].lower():
            continue
        fetch = FETCHERS.get(c.get("ats"))
        if not fetch:
            continue  # manual / unsupported
        stats["companies"] += 1
        try:
            jobs = fetch(c)
        except Exception as e:  # keep going on one bad company
            stats["errors"].append(f"{c['name']}: {e}")
            if verbose:
                print(f"  ✗ {c['name']}: {e}")
            continue
        new = kept = 0
        for j in jobs:
            reason = prefilter(j)
            j["status"] = "filtered_out" if reason else "found"
            if db.upsert_job(conn, j):
                new += 1
                if not reason:
                    kept += 1
        conn.commit()
        stats["fetched"] += len(jobs)
        stats["new"] += new
        stats["kept"] += kept
        if verbose:
            print(f"  ✓ {c['name']:<30} {len(jobs):>4} jobs, {new:>3} new, {kept:>3} match filters")
    return stats


def verify_companies() -> list[tuple[str, str, str]]:
    """Ping each enabled company's feed. Returns (name, ok|error, detail)."""
    global _QUICK
    _QUICK = True
    results = []
    for c in config.companies():
        fetch = FETCHERS.get(c.get("ats"))
        if not fetch:
            results.append((c["name"], "skip", f"ats={c.get('ats')} (manual)"))
            continue
        try:
            jobs = fetch(c)
            india = sum(1 for j in jobs if _has_any(j["location"], ["india", "pune", "bangalore", "bengaluru", "remote"]))
            results.append((c["name"], "ok", f"{len(jobs)} jobs ({india} India/remote)"))
        except Exception as e:
            results.append((c["name"], "error", str(e)[:120]))
    _QUICK = False
    return results


def job_from_url(url: str, title="", company="", description="") -> dict:
    """Manual add (LinkedIn, Naukri, Darwinbox, any careers page)."""
    host = urlparse(url).netloc.lower()
    source = "linkedin" if "linkedin." in host else "naukri" if "naukri." in host else "company"
    hit = detect_ats(url)
    ats = hit[0] if hit else "manual"
    ext = re.sub(r"[^a-zA-Z0-9]+", "-", urlparse(url).path).strip("-")[-60:] or host
    j = _job(company or host, ats, ext, title, "", url, url, description, None)
    j["source"] = source
    return j
