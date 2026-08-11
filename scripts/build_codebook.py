"""
build_codebook.py
=================
Parses the two XLSForm instruments into a single codebook.json.

The codebook is the one place that knows what a question is called, what it
says in English, and what its coded answers mean. Both the demo-data
generator and the daily ETL read it, so re-running this after an instrument
revision is all that is needed to keep them in step.

Usage:
    python scripts/build_codebook.py
"""

import json
import re
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
INSTRUMENTS = ROOT / "instruments"
OUT = ROOT / "codebook" / "codebook.json"

FORMS = {
    "awareness": "Awareness Survey Follow-up 202607.xlsx",
    "turmeric": "turmeric_sampling_survey.xlsx",
}

# XLSForm header names drift between authors ("label: eng" vs "label:eng").
# Normalising to a bare key lets one parser read both instruments.
LABEL_KEYS = ("label:eng", "label: eng", "label::english", "label:english")
FALLBACK_LABEL_KEYS = ("label",)


def norm_header(h):
    if h is None:
        return ""
    return re.sub(r"\s+", " ", str(h)).strip().lower()


def sheet_rows(wb, name):
    """Yield each row of a sheet as a dict keyed by normalised header."""
    if name not in wb.sheetnames:
        return []
    ws = wb[name]
    rows = list(ws.values)
    if not rows:
        return []
    hdr = [norm_header(h) for h in rows[0]]
    out = []
    for r in rows[1:]:
        d = {}
        for i, key in enumerate(hdr):
            if not key or i >= len(r):
                continue
            v = r[i]
            if v is None:
                continue
            if isinstance(v, str):
                v = v.strip()
                if not v:
                    continue
            d[key] = v
        if d:
            out.append(d)
    return out


def pick_label(d):
    """English label if the instrument carries one, else the default label."""
    for k in LABEL_KEYS:
        if d.get(k):
            return str(d[k]).strip()
    for k in FALLBACK_LABEL_KEYS:
        if d.get(k):
            return str(d[k]).strip()
    return ""


def clean_label(s):
    """Strip the 'Question 12.' prefixes so chart titles stay short."""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    s = re.sub(r"^(Question|Q|Ques)\s*[\.\-]?\s*\d+[a-zA-Z]?\s*[\.\)\-:]?\s*", "", s, flags=re.I)
    s = re.sub(r"^\d+[a-zA-Z]?\s*[\.\)]\s*", "", s)
    return s.strip()


def parse_type(t):
    """Split an XLSForm type cell into (kind, list_name)."""
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    m = re.match(r"^(select_one|select_multiple|select one|select multiple)\s+(\S+)", t, flags=re.I)
    if m:
        kind = m.group(1).lower().replace(" ", "_")
        return kind, m.group(2)
    return t.lower(), None


def build_form(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

    # ---- choices ------------------------------------------------------
    choices = {}
    for d in sheet_rows(wb, "choices"):
        ln = d.get("list_name") or d.get("list name")
        if not ln:
            continue
        ln = str(ln).strip()
        val = d.get("value") or d.get("name")
        if val is None:
            continue
        val = str(val).strip()
        entry = {
            "value": val,
            "label": clean_label(pick_label(d)) or val,
        }
        # choice_filter columns travel with the choice and drive cascades
        for extra in ("city", "filter"):
            if d.get(extra) is not None:
                entry[extra] = str(d[extra]).strip()
        choices.setdefault(ln, []).append(entry)

    # ---- questions ----------------------------------------------------
    questions = {}
    order = []
    group_stack = []
    for d in sheet_rows(wb, "survey"):
        raw_type = d.get("type")
        if not raw_type:
            continue
        kind, list_name = parse_type(raw_type)
        name = str(d.get("name") or "").strip()

        if kind in ("begin group", "begin_group", "begin repeat", "begin_repeat"):
            group_stack.append(name or kind)
            continue
        if kind in ("end group", "end_group", "end repeat", "end_repeat"):
            if group_stack:
                group_stack.pop()
            continue
        if not name:
            continue

        q = {
            "name": name,
            "type": kind,
            "label": clean_label(pick_label(d)),
            "raw_label": re.sub(r"\s+", " ", str(pick_label(d))).strip(),
            "group": list(group_stack),
        }
        if list_name:
            q["list_name"] = list_name
        if d.get("relevance"):
            q["relevance"] = re.sub(r"\s+", " ", str(d["relevance"])).strip()
        if d.get("required"):
            q["required"] = True
        if d.get("constraint"):
            q["constraint"] = re.sub(r"\s+", " ", str(d["constraint"])).strip()
        if d.get("appearance"):
            q["appearance"] = str(d["appearance"]).strip()
        questions[name] = q
        order.append(name)

    wb.close()
    return {"questions": questions, "order": order, "choices": choices}


def main():
    book = {"forms": {}}
    for key, fname in FORMS.items():
        path = INSTRUMENTS / fname
        if not path.exists():
            print(f"  ! missing instrument: {path}", file=sys.stderr)
            continue
        form = build_form(path)
        form["source_file"] = fname
        book["forms"][key] = form
        n_sel = sum(1 for q in form["questions"].values() if q["type"].startswith("select"))
        print(
            f"  {key:10s} {len(form['questions']):4d} fields "
            f"({n_sel} coded)  ·  {len(form['choices'])} choice lists"
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(book, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  -> {OUT.relative_to(ROOT)}  ({OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
