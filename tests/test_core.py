"""Discovery parsing, filters, DB, mapper prompt hygiene — offline."""
import json

import pytest

from jobagent import config, db, discovery
from jobagent.apply import mapper


def test_detect_ats():
    d = discovery.detect_ats
    assert d("https://boards.greenhouse.io/acme/jobs/123") == ("greenhouse", "acme")
    assert d("https://job-boards.greenhouse.io/acme/jobs/1") == ("greenhouse", "acme")
    assert d("https://jobs.lever.co/acme/abc-123") == ("lever", "acme")
    assert d("https://jobs.ashbyhq.com/acme.io/xyz") == ("ashby", "acme.io")
    assert d("https://acme.wd5.myworkdayjobs.com/en-US/External/job/Pune/SRE_R1") == \
        ("workday", "acme.wd5.myworkdayjobs.com/en-US/External")
    assert d("https://acme.darwinbox.in/ms/candidate/careers") == ("darwinbox", "acme")
    assert d("https://www.linkedin.com/jobs/view/4012345678/") == ("linkedin", "4012345678")
    assert d("https://example.com/careers") is None


def test_prefilter():
    base = {"company": "Acme", "location": "Pune, India", "posted_at": None}
    assert discovery.prefilter({**base, "title": "Senior Platform Engineer"}) is None
    assert discovery.prefilter({**base, "title": "Senior SRE"}) is None
    assert discovery.prefilter({**base, "title": "Junior DevOps Engineer"}) == "title excluded"
    assert discovery.prefilter({**base, "title": "Account Executive"}) == "title not in include list"
    assert discovery.prefilter({**base, "title": "DevOps Engineer", "location": "Berlin"}).startswith("location")
    assert discovery.prefilter({**base, "title": "Platform Engineer", "company": "Example Current Employer Ltd"}) == "company blacklisted"
    assert discovery.prefilter({**base, "title": "Platform Engineer", "posted_at": "2020-01-01T00:00:00Z"}) == "too old"
    for loc in ["Remote - India", "India (Remote)", "bangalore, in", "Bengaluru", "Remote", "APAC Remote", "India, Pune"]:
        assert discovery.prefilter({**base, "title": "SRE", "location": loc}) is None, loc
    for loc in ["Remote - USA", "UK, Remote", "US, CA, Remote", "Remote US NC", "Seattle, WA (Remote)",
                "Remote, Canada; Remote, United Kingdom; Remote, United States", "CAN-Ontario Remote Location",
                "Hyderabad, in", "Mumbai, Maharashtra, in", "Remote - Hyderabad"]:
        assert discovery.prefilter({**base, "title": "SRE", "location": loc}), loc
    # "sre" must not match inside other words
    assert discovery.prefilter({**base, "title": "Treasurer"}) == "title not in include list"


def test_lever_parser(monkeypatch):
    sample = [{"id": "abc", "text": "Site Reliability Engineer", "categories": {"location": "Pune, India"},
               "createdAt": 1786469891368, "descriptionPlain": "Run k8s.", "lists": [{"text": "Req", "content": "<li>Terraform</li>"}],
               "additionalPlain": "Benefits", "hostedUrl": "https://jobs.lever.co/x/abc", "applyUrl": "https://jobs.lever.co/x/abc/apply",
               "workplaceType": "hybrid"}]

    class R:
        def raise_for_status(self): pass
        def json(self): return sample
    monkeypatch.setattr(discovery.requests, "get", lambda *a, **k: R())
    [j] = discovery.fetch_lever({"name": "X Corp", "slug": "x"})
    assert j["id"] == "lever:x-corp:abc" and j["apply_url"].endswith("/apply")
    assert "Terraform" in j["description"] and "Benefits" in j["description"]
    assert j["posted_at"].startswith("2026")


def test_db_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    j = {"id": "greenhouse:acme:1", "source": "company", "company": "Acme", "title": "SRE", "url": "u"}
    assert db.upsert_job(conn, j) and not db.upsert_job(conn, j)
    db.update(conn, "greenhouse:acme:1", status="applied", applied_at=db.now())
    assert db.applied_today(conn, "company") == 1
    assert db.get(conn, "acme:1")["title"] == "SRE"
    with pytest.raises(ValueError):
        db.update(conn, "greenhouse:acme:1", status="nonsense")


def test_mapper_never_sends_min_ctc(monkeypatch):
    seen = {}
    monkeypatch.setattr(mapper.llm, "ask_json", lambda s, u, **k: seen.update(sys=s, user=u) or {"fields": []})
    monkeypatch.setattr(mapper, "load_learned", lambda: [])
    monkeypatch.setattr(mapper.config, "resume_text", lambda: "RESUME-MARKER")
    job = {"title": "SRE", "company": "Acme", "location": "Pune", "description": "k8s"}
    mapper.map_fields([{"id": "f0_0", "kind": "text", "question": "Expected CTC", "value": ""}], job)
    assert "minimum:" not in seen["user"]
    assert "source of truth" in seen["sys"]  # guardrails present
    assert "RESUME-MARKER" in seen["user"]  # resume text included
    assert "minimum" in json.dumps(config.profile())  # still in config for scoring


def test_learned_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(mapper, "LEARNED_PATH", tmp_path / "l.yaml")
    mapper.save_learned([{"q": "Years with Helm?", "a": "6"}])
    mapper.save_learned([{"q": "years with helm?", "a": "7"}, {"q": "Blank", "a": ""}])
    assert mapper.load_learned() == [{"q": "years with helm?", "a": "7"}]
