"""Extract fields from every frame, fill mapped values, highlight what needs a human."""
from __future__ import annotations

import difflib
import re
from pathlib import Path

FIELDS_JS = (Path(__file__).parent / "fields.js").read_text()

STYLE = {
    "filled": "2px solid #16a34a",
    "review": "3px solid #f59e0b",
    "missing": "3px solid #dc2626",
}


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def best_option(want, options: list[str], cutoff=0.55) -> int | None:
    """Index of the option that best matches `want` (exact > prefix/substring > fuzzy)."""
    if want is None or not options:
        return None
    w = _norm(want)
    normed = [_norm(o) for o in options]
    if w in normed:
        return normed.index(w)
    for i, o in enumerate(normed):
        if o and (o.startswith(w) or w.startswith(o)) and min(len(o), len(w)) >= 2:
            return i
    for i, o in enumerate(normed):
        if w and o and (w in o or o in w) and min(len(o), len(w)) >= 3:
            return i
    scores = [difflib.SequenceMatcher(None, w, o).ratio() for o in normed]
    i = max(range(len(scores)), key=scores.__getitem__)
    return i if scores[i] >= cutoff else None


def extract(page) -> tuple[list[dict], dict]:
    """Fields from all frames. Returns (fields, id->frame)."""
    fields, where = [], {}
    for fi, frame in enumerate(page.frames):
        try:
            got = frame.evaluate(FIELDS_JS, fi)
        except Exception:
            continue  # cross-origin frame that refuses scripts, detached frame, etc.
        for f in got:
            where[f["id"]] = frame
            fields.append(f)
    return fields, where


OUTLINE_JS = """(el, s) => {
  let t = el;
  const id = el.getAttribute('data-ja');
  const members = [...document.querySelectorAll(`[data-ja="${id}"]`)];
  if (members.length > 1) {            // radio / checkbox group → outline the whole group
    t = el.closest('fieldset,[role=radiogroup],[role=group]');
    if (!t) { t = el.parentElement; while (t && !members.every(m => t.contains(m))) t = t.parentElement; }
  } else if (el.type === 'radio' || el.type === 'checkbox') {
    t = el.closest('label') || el.parentElement;
  } else if (el.type === 'file') {
    t = el.parentElement;
  }
  t = t || el;
  t.style.outline = s; t.style.outlineOffset = '2px';
}"""


def _outline(loc, style):
    try:
        loc.evaluate(OUTLINE_JS, style)
    except Exception:
        pass


def _pick_open_option(frame, page, want) -> bool:
    """After opening a custom dropdown, click the best [role=option]."""
    for scope in (frame, page):
        try:
            scope.wait_for_selector("[role=option]", timeout=2500)
        except Exception:
            continue
        opts = scope.locator("[role=option]:visible")
        labels = [t.strip() for t in opts.all_inner_texts()]
        i = best_option(want, labels)
        if i is not None:
            opts.nth(i).click()
            return True
    return False


TICK_JS = """(el, on) => {
  if (el.checked === on) return;
  const lab = (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || el.closest('label');
  if (lab) lab.click(); else el.click();
  if (el.checked !== on) {            // last resort: set + fire events frameworks listen to
    el.checked = on;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
}"""


def _tick(loc, on=True):
    """Check/uncheck radios & checkboxes, including visually hidden custom-styled ones."""
    try:
        (loc.check if on else loc.uncheck)(timeout=2000)
    except Exception:
        loc.evaluate(TICK_JS, on)
    if loc.is_checked() != on:
        raise RuntimeError("could not toggle")


def fill_one(page, frame, field: dict, value, files: dict) -> str:
    """Fill a single field. Returns 'ok' | 'skip' | 'fail:<why>'."""
    kind = field["kind"]
    loc = frame.locator(f'[data-ja="{field["id"]}"]')
    if value is None:
        return "skip"
    try:
        if kind == "file":
            path = files.get(str(value).upper())
            if not path:
                return "fail:no file for " + str(value)
            loc.first.set_input_files(str(path))
        elif kind == "select":
            i = best_option(value, field.get("options", []))
            if i is None:
                return f"fail:no option ~ {value!r}"
            loc.select_option(label=field["options"][i])
        elif kind == "radio":
            i = best_option(value, field.get("options", []))
            if i is None:
                return f"fail:no option ~ {value!r}"
            _tick(frame.locator(f'[data-ja="{field["id"]}"][data-ja-opt="{i}"]'))
        elif kind == "checkbox":
            _tick(loc, value in (True, "true", "yes", "Yes"))
        elif kind == "checkbox_group":
            wanted = value if isinstance(value, list) else [value]
            for w in wanted:
                i = best_option(w, field.get("options", []))
                if i is not None:
                    _tick(frame.locator(f'[data-ja="{field["id"]}"][data-ja-opt="{i}"]'))
        elif kind == "listbox_button":
            loc.click()
            if not _pick_open_option(frame, page, value):
                page.keyboard.press("Escape")
                return f"fail:no option ~ {value!r}"
        elif kind == "combobox":
            loc.click()
            loc.fill(str(value))
            if not _pick_open_option(frame, page, value):
                loc.press("Enter")
        else:  # text-like
            loc.fill(str(value))
            loc.dispatch_event("change")
            loc.dispatch_event("blur")
        return "ok"
    except Exception as e:
        return f"fail:{type(e).__name__}: {str(e).splitlines()[0][:120]}"


def fill_page(page, mapping_fn, files: dict, overwrite=False) -> dict:
    """Extract → map (LLM) → fill → highlight. Returns a report."""
    fields, where = extract(page)
    # Send pre-filled fields too (ATSs pre-fill from the parsed resume, often wrongly,
    # e.g. an old address). The mapper returns only values that need changing.
    # Already-uploaded files are left alone unless overwrite=True.
    todo = [f for f in fields if overwrite or not (f["kind"] == "file" and f["value"])]
    by_id = {f["id"]: f for f in fields}
    mapped = mapping_fn(todo) if todo else []
    decided = {m["id"]: m for m in mapped if m.get("id") in by_id}

    report = {"fields": len(fields), "filled": [], "review": [], "missing": [], "failed": []}
    for f in fields:
        frame = where[f["id"]]
        loc = frame.locator(f'[data-ja="{f["id"]}"]').first
        m = decided.get(f["id"])
        if m is not None and m.get("value") is not None:
            had = f["value"] not in ("", None, [], False)
            if had and str(m["value"]).strip().lower() == str(f["value"]).strip().lower():
                continue  # mapper echoed the existing value — nothing to do
            res = fill_one(page, frame, f, m["value"], files)
            if res == "ok":
                if had and f["kind"] != "file":
                    note = f"changed from {str(f['value'])[:40]!r}"
                    report["review"].append((f["question"], m["value"], note))
                    _outline(loc, STYLE["review"])
                elif m.get("needs_review"):
                    report["review"].append((f["question"], m["value"], m.get("note")))
                    _outline(loc, STYLE["review"])
                else:
                    report["filled"].append((f["question"], m["value"]))
                    _outline(loc, STYLE["filled"])
                continue
            report["failed"].append((f["question"], m["value"], res))
        already = f["value"] not in ("", None, [], False)
        if already and not overwrite:
            continue
        if f.get("required") or (m and m.get("needs_review")):
            report["missing"].append((f["question"], m.get("note") if m else None))
            _outline(loc, STYLE["missing"])
    return report


BANNER_JS = """(msg) => {
  let b = document.getElementById('__jobagent_banner');
  if (!b) { b = document.createElement('div'); b.id='__jobagent_banner'; b.setAttribute('data-ja-skip','1');
    b.style.cssText='position:fixed;z-index:2147483647;bottom:16px;right:16px;max-width:380px;padding:12px 14px;'+
      'background:#111827;color:#f9fafb;font:13px/1.4 -apple-system,system-ui,sans-serif;border-radius:10px;'+
      'box-shadow:0 8px 24px rgba(0,0,0,.3);white-space:pre-line;pointer-events:none';
    document.body.appendChild(b); }
  b.textContent = msg;
}"""


def banner(page, report):
    msg = (f"JobAgent — {len(report['filled'])} filled ✓\n"
           f"🟧 {len(report['review'])} to review   🟥 {len(report['missing'])} missing"
           f"{'   ⚠ ' + str(len(report['failed'])) + ' failed' if report['failed'] else ''}\n"
           "Check everything, then click Submit yourself.")
    try:
        page.evaluate(BANNER_JS, msg)
    except Exception:
        pass


def read_answers(page) -> list[dict]:
    """Current question→answer pairs on the page (for learning your manual edits)."""
    fields, _ = extract(page)
    out = []
    for f in fields:
        v = f.get("value")
        if f["kind"] in ("file",) or v in ("", None, [], False):
            continue
        if f["kind"] in ("email", "tel", "password"):
            continue
        if f["kind"] == "radio" and isinstance(v, list):
            v = v[0]
        out.append({"q": f["question"], "a": v})
    return out
