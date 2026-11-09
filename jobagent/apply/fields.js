// Extract every fillable field on the page (one frame) in a form an LLM can map.
// Tags each element with data-ja="<id>" so Python can find it again.
// Called with a numeric prefix so ids stay unique across frames.
(prefix) => {
  const txt = (s) => (s || "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (s.visibility === "hidden" || s.display === "none") return false;
    // custom-styled radios/checkboxes/files are often 0x0 but their label is visible
    if (["radio", "checkbox", "file"].includes(el.type)) return true;
    return r.width > 0 && r.height > 0;
  };
  const byId = (id) => (id ? document.getElementById(id) : null);

  const questionText = (el) => {
    let t = "";
    if (el.getAttribute("aria-labelledby"))
      t = el.getAttribute("aria-labelledby").split(/\s+/).map((i) => byId(i)?.innerText || "").join(" ");
    if (!t && el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) t = l.innerText;
    }
    if (!t && el.getAttribute("aria-label")) t = el.getAttribute("aria-label");
    if (!t) { const l = el.closest("label"); if (l) t = l.innerText; }
    if (!t) {
      let c = el.parentElement;
      for (let i = 0; i < 5 && c && !t; i++) {
        const lab = c.querySelector("label, legend, [class*='label' i], [class*='question' i], [data-automation-id*='label' i]");
        if (lab && !lab.contains(el)) t = lab.innerText;
        c = c.parentElement;
      }
    }
    if (!t) t = el.placeholder || el.name || el.id || "";
    return txt(t).slice(0, 400);
  };

  const groupQuestion = (inputs) => {
    const first = inputs[0];
    const fs = first.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg) return txt(lg.innerText).slice(0, 400); }
    const grp = first.closest("[role=radiogroup],[role=group]");
    if (grp) {
      const lb = grp.getAttribute("aria-labelledby");
      if (lb) return txt(lb.split(/\s+/).map((i) => byId(i)?.innerText || "").join(" "));
      if (grp.getAttribute("aria-label")) return txt(grp.getAttribute("aria-label"));
    }
    // walk up until the container holds text beyond the option labels
    let c = first.parentElement;
    const optText = new Set(inputs.map(optionLabel));
    for (let i = 0; i < 6 && c; i++) {
      const lab = c.querySelector("label:not(:has(input)), legend, [class*='label' i], [class*='question' i]");
      if (lab && !optText.has(txt(lab.innerText)) && !inputs.some((x) => lab.contains(x)))
        return txt(lab.innerText).slice(0, 400);
      c = c.parentElement;
    }
    return first.name || "";
  };

  const optionLabel = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return txt(l.innerText);
    }
    const l = el.closest("label");
    if (l) return txt(l.innerText);
    if (el.getAttribute("aria-label")) return txt(el.getAttribute("aria-label"));
    const sib = el.nextElementSibling;
    if (sib) return txt(sib.innerText);
    return el.value;
  };

  const isRequired = (el, q) =>
    el.required || el.getAttribute("aria-required") === "true" || /\*\s*$|\(required\)/i.test(q);

  const out = [];
  let n = 0;
  const nextId = () => `f${prefix}_${n++}`;
  const seenGroups = new Set();

  const SKIP = new Set(["hidden", "submit", "button", "reset", "image", "search"]);
  const els = document.querySelectorAll(
    "input, select, textarea, [role=combobox]:not(input), button[aria-haspopup=listbox]"
  );

  for (const el of els) {
    if (el.closest(".g-recaptcha, [class*='captcha' i], [data-ja-skip]")) continue;
    if (el.disabled || el.readOnly && el.tagName === "INPUT" && el.type !== "file" && el.getAttribute("role") !== "combobox") continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || (tag === "input" ? "text" : tag)).toLowerCase();
    if (tag === "input" && SKIP.has(type)) continue;
    if (!visible(el)) continue;

    // radio / checkbox groups
    if (tag === "input" && (type === "radio" || type === "checkbox")) {
      const name = el.name;
      const members = name
        ? [...document.querySelectorAll(`input[type=${type}][name="${CSS.escape(name)}"]`)]
        : [el];
      const key = type + ":" + (name || Math.random());
      if (seenGroups.has(key)) continue;
      seenGroups.add(key);
      const id = nextId();
      const options = members.map((m, i) => {
        m.setAttribute("data-ja", id);
        m.setAttribute("data-ja-opt", String(i));
        return { index: i, label: optionLabel(m), checked: m.checked };
      });
      const single = type === "checkbox" && members.length === 1;
      const q = single ? optionLabel(el) : groupQuestion(members);
      out.push({
        id, kind: single ? "checkbox" : type === "radio" ? "radio" : "checkbox_group",
        question: q, options: single ? undefined : options.map((o) => o.label),
        value: single ? el.checked : options.filter((o) => o.checked).map((o) => o.label),
        required: members.some((m) => isRequired(m, q)),
      });
      continue;
    }

    const id = nextId();
    el.setAttribute("data-ja", id);
    const q = questionText(el);
    const field = { id, question: q, required: isRequired(el, q) };

    if (tag === "select") {
      field.kind = "select";
      field.options = [...el.options].filter((o) => o.value !== "" || o.text.trim()).map((o) => txt(o.text)).slice(0, 250);
      field.value = el.selectedIndex > 0 ? txt(el.options[el.selectedIndex].text) : "";
      field.multiple = el.multiple;
    } else if (tag === "textarea") {
      field.kind = "textarea";
      field.value = el.value;
    } else if (tag === "button") {
      field.kind = "listbox_button";      // Workday-style dropdown
      field.value = txt(el.innerText);
      if (/^select one$|^select$|^choose/i.test(field.value)) field.value = "";
    } else if (el.getAttribute("role") === "combobox") {
      field.kind = "combobox";            // react-select / autocomplete
      field.value = el.value || "";
      const shown = el.closest("[class*='container' i], [class*='select' i]")?.querySelector("[class*='singleValue' i], [class*='single-value' i]");
      if (shown) field.value = txt(shown.innerText);
    } else if (type === "file") {
      field.kind = "file";
      field.accept = el.accept || "";
      field.value = el.files && el.files.length ? el.files[0].name : "";
    } else {
      field.kind = type;                  // text, email, tel, url, number, date, ...
      field.value = el.value;
      if (el.maxLength > 0 && el.maxLength < 100000) field.max_length = el.maxLength;
    }
    if (el.placeholder) field.placeholder = txt(el.placeholder).slice(0, 120);
    out.push(field);
  }
  return out;
}
