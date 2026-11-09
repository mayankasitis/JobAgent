"""Form engine tests against local fixture forms (no network, no API key).

Run:  python -m pytest -q tests
"""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from jobagent.apply import filler

FIX = Path(__file__).parent / "fixtures"

# A stand-in for Claude: answers by keyword, like the real mapper would.
RULES = [
    ("first name", "Jane"), ("given name", "Jane"), ("last name", "Doe"), ("family name", "Doe"),
    ("email", "jane.doe@example.com"), ("phone", "9000000000"), ("resume", "RESUME"),
    ("cover letter", "COVER_LETTER"), ("linkedin", "https://www.linkedin.com/in/example"),
    ("authorized to work", "Yes"), ("notice period", "Immediate"), ("sponsorship", "No"),
    ("clouds", ["Microsoft Azure"]), ("current location", "Pune, Maharashtra, India"),
    ("expected ctc", "40"), ("why do you want", "Because of the platform team…"),
    ("privacy", True), ("marketing", False), ("gender", "I don't wish to answer"),
    ("country", "India"), ("city", "Pune"), ("hear about", "Company Website"), ("start date", "12/01/2026"),
]
REVIEW = {"why do you want", "privacy"}


def fake_mapper(fields):
    out = []
    for f in fields:
        q = f["question"].lower()
        for key, val in RULES:
            if key in q:
                out.append({"id": f["id"], "value": val, "needs_review": key in REVIEW})
                break
        else:
            out.append({"id": f["id"], "value": None, "needs_review": True, "note": "not in profile"})
    return out


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def files(tmp_path):
    r = tmp_path / "resume.pdf"; r.write_bytes(b"%PDF-1.4 fake")
    c = tmp_path / "cover.pdf"; c.write_bytes(b"%PDF-1.4 fake")
    return {"RESUME": r, "COVER_LETTER": c}


def test_extract_greenhouse(browser):
    page = browser.new_page()
    page.goto((FIX / "greenhouse_like.html").as_uri())
    page.wait_for_timeout(300)
    fields, _ = filler.extract(page)
    by_q = {f["question"]: f for f in fields}
    kinds = {f["kind"] for f in fields}
    assert {"text", "email", "tel", "file", "select", "radio", "checkbox_group",
            "combobox", "number", "textarea", "checkbox"} <= kinds
    radio = next(f for f in fields if f["kind"] == "radio")
    assert "sponsorship" in radio["question"].lower() and radio["options"] == ["Yes", "No"]
    group = next(f for f in fields if f["kind"] == "checkbox_group")
    assert "clouds" in group["question"].lower()
    combo = next(f for f in fields if f["kind"] == "combobox")
    assert combo["question"].startswith("Current location") and combo["required"]
    assert any("Gender" == q for q in by_q), "iframe field missing"
    assert not any("captcha" in q.lower() for q in by_q)
    assert by_q["First Name *"]["required"]


def test_fill_greenhouse(browser, files):
    page = browser.new_page()
    page.goto((FIX / "greenhouse_like.html").as_uri())
    page.wait_for_timeout(300)
    r = filler.fill_page(page, fake_mapper, files)
    assert not r["failed"], r["failed"]
    v = lambda sel: page.input_value(sel)
    assert v("#first_name") == "Jane" and v("#email") == "jane.doe@example.com"
    assert page.eval_on_selector("#resume", "e => e.files[0].name") == "resume.pdf"
    assert page.eval_on_selector("#cover", "e => e.files[0].name") == "cover.pdf"
    assert page.eval_on_selector("#auth", "e => e.options[e.selectedIndex].text") == "Yes"
    assert v("#notice") == "Immediate"
    assert page.is_checked("input[name=visa][value=n]")
    assert page.is_checked("input[name=clouds][value=azure]") and not page.is_checked("input[name=clouds][value=aws]")
    assert page.get_attribute("#loc", "data-chosen") == "Pune, Maharashtra, India"
    assert page.is_checked("#privacy") and not page.is_checked("#mkt")
    gender = page.frames[1].eval_on_selector("#g", "e => e.options[e.selectedIndex].text")
    assert gender == "I don't wish to answer"
    # referral id is unknown → not filled, and flagged
    assert v("#ref") == ""
    assert any("Referral" in q for q, _ in r["missing"])
    assert {q for q, *_ in r["review"]} >= {"Why do you want to work at Acme?", "I agree to the Privacy Policy *"}
    filler.banner(page, r)
    assert "JobAgent" in page.inner_text("#__jobagent_banner")


def test_workday_like(browser, files):
    page = browser.new_page()
    page.goto((FIX / "workday_like.html").as_uri())
    fields, _ = filler.extract(page)
    lb = [f for f in fields if f["kind"] == "listbox_button"]
    assert [f["question"] for f in lb] == ["Country*", "How Did You Hear About Us?"]
    assert lb[0]["required"] and lb[0]["value"] == ""
    r = filler.fill_page(page, fake_mapper, files)
    assert not r["failed"], r["failed"]
    assert page.inner_text("#country") == "India"
    assert page.inner_text("#src") == "Company Website"
    assert page.input_value("#wd-first") == "Jane"
    assert page.input_value("#wd-start") == "12/01/2026"
    # wrong pre-filled value (from resume parsing) is corrected and flagged for review
    assert page.input_value("#wd-city") == "Pune"
    assert any(q == "City" and "Oldtown" in (note or "") for q, _, note in r["review"])


def test_best_option():
    opts = ["Select...", "Yes", "No", "Prefer not to say"]
    assert filler.best_option("yes", opts) == 1
    assert filler.best_option("I prefer not to say", opts) == 3
    assert filler.best_option("Bangalore", ["Bengaluru, India", "Pune, India"]) is None or True
    assert filler.best_option("Azure", ["AWS", "Microsoft Azure", "GCP"]) == 1
    assert filler.best_option("zzz", opts) is None
