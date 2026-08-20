"""
update_dashboard.py
===================
THE DAILY SCRIPT. Reads whatever CSVs are sitting in data_in/, decodes them
against codebook.json, and writes data/dashboard_data.js.

The dashboard does its own aggregation in the browser, so this stage ships
record-level microdata (as compact arrays) rather than pre-chewed chart series.
That is what lets the global filter bar re-cut every chart on the page without
a round trip.

It is deliberately forgiving about export shape, because SurveyCTO can be told
to export the same form several different ways:

  * select_multiple as "1 3 4", or as expanded Q2_1 / Q2_2 binary columns
  * geopoint as one "gps" column, or as gps-Latitude / gps-Longitude / ...
  * repeat groups as separate long CSVs (PARENT_KEY), or flattened wide
    (sample_type_details_1_total_samples_collect)

Any column it does not recognise is passed through untouched, so adding a
question to the instrument never breaks the build.

Usage:
    python scripts/update_dashboard.py
    python scripts/update_dashboard.py --check       # validate, write nothing
"""

import argparse
import base64
import csv
import difflib
import hashlib
import json
import math
import os
import re
import sys
import zlib
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
CB_PATH = ROOT / "codebook" / "codebook.json"
IN_AW = ROOT / "data_in" / "awareness"
IN_TS = ROOT / "data_in" / "turmeric"
OUT = ROOT / "data" / "dashboard_data.js"

MISSING = {"", ".", "na", "n/a", "null", "none", "-", "nan"}

# GitHub Pages serves every file in the repo to anyone who asks for it, so a
# login screen implemented in JavaScript protects nothing on its own. The
# payload carries vendor names, GPS fixes and record-level responses, so it is
# compressed and encrypted here and only ever decrypted in the browser, after
# the password is entered. Parameters must match the WebCrypto call in app.js.
PBKDF2_ITERATIONS = 250_000
DEFAULT_PASSWORD = "TS2026_RS"
# SurveyCTO writes these for don't-know / refused; they must never be counted
# as substantive answers but should still be visible in distributions.
SPECIAL = {"-999": "Don't know", "-888": "Refused", "999": "Don't know", "888": "Refused"}
# "Other (specify)" in every cascading market list. It is an escape hatch for
# the enumerator, not a market the programme planned to visit, so it never
# counts towards a target.
OTHER_CODE = "777"

# ---- fieldwork targets ----------------------------------------------------
# Read off the instruments rather than typed in here, so that adding a market
# to an XLSForm moves the target with it instead of leaving the dashboard
# measuring progress against a number nobody remembers setting.
AW_PER_CITY = 150          # consented awareness interviews per study city
SHOPS_PER_WHOLESALE = 25   # vendors sampled in each wholesale market
SHOPS_PER_RETAIL = 2       # vendors sampled in each retail locality


# ----------------------------------------------------------------------
#  small helpers
# ----------------------------------------------------------------------
def encrypt_payload(obj, password):
    """
    JSON -> deflate -> AES-256-GCM -> base64(salt | iv | ciphertext+tag).

    Compressing before encrypting matters: ciphertext is indistinguishable from
    random, so gzip on the wire buys nothing once it is encrypted. Deflating
    first takes the payload from megabytes to a couple of hundred kilobytes.
    """
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    packed = zlib.compress(raw, 9)
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS
    ).derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, packed, None)
    return base64.b64encode(salt + iv + ct).decode("ascii"), len(raw), len(packed)


def stamp_index():
    """
    Version every local asset in index.html by content hash.

    GitHub Pages serves with a ten-minute cache and browsers hold a file
    longer still on revalidation, so an unversioned URL means a returning
    viewer keeps whatever they downloaded last. Stamping only the payload was
    not enough and shipped a broken site once: index.html changed, assets/app.js
    changed too, but the browser kept its cached copy of the script and
    combined the new page with the old code -- every getElementById on an
    element the new page no longer had threw inside boot(), so the dashboard
    rendered its shell and not one chart.

    Hashing rather than timestamping means a file that actually changed is
    always re-fetched, and one that did not is still served from cache.
    """
    idx = ROOT / "index.html"
    if not idx.exists():
        return []
    html = idx.read_text(encoding="utf-8")
    stamped = []

    def sub(m):
        rel = m.group(2)
        f = ROOT / rel
        if not f.exists():
            return m.group(0)
        ver = hashlib.sha256(f.read_bytes()).hexdigest()[:10]
        stamped.append(f"{rel}?v={ver}")
        return f"{m.group(1)}{rel}?v={ver}{m.group(4)}"

    new = re.sub(
        r'((?:src|href)=")((?:assets|data)/[A-Za-z0-9_.\-]+\.(?:js|css))(\?v=[^"]*)?(")',
        sub, html)
    if new != html:
        idx.write_text(new, encoding="utf-8")
    return stamped


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def is_missing(v):
    return v is None or norm(v).lower() in MISSING


def to_num(v):
    if is_missing(v):
        return None
    try:
        f = float(str(v).replace(",", "").strip())
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def to_int(v):
    f = to_num(v)
    return None if f is None else int(round(f))


DATE_FORMATS = [
    "%b %d, %Y %I:%M:%S %p", "%b %d, %Y %I:%M %p", "%b %d, %Y %H:%M:%S",
    "%B %d, %Y %I:%M:%S %p", "%B %d, %Y %I:%M %p",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%d-%b-%Y", "%d-%m-%Y %H:%M", "%d-%m-%Y",
]

SLASH_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})")
TIME_FORMATS = ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p")


def detect_month_first(values):
    """
    Decide whether a slash-dated column is d/m/Y or m/d/Y.

    A single value settles it: a first component above twelve cannot be a
    month, a second above twelve cannot be a day. Deciding once for the whole
    column rather than per value is the point -- guessing value by value is
    how 8/5 and 5/8 from one export end up in different months.

    Returns True for month-first, False for day-first, None if the column
    carries no value that distinguishes them.
    """
    first_big = second_big = False
    for v in values:
        m = SLASH_DATE.match(norm(v))
        if not m:
            continue
        first_big = first_big or int(m.group(1)) > 12
        second_big = second_big or int(m.group(2)) > 12
    if second_big and not first_big:
        return True
    if first_big and not second_big:
        return False
    return None


def parse_dt(v, month_first=None):
    if is_missing(v):
        return None
    s = norm(v).replace("Z", "")
    s = re.sub(r"([+-]\d{2}:?\d{2})$", "", s).strip()

    # Honour the column-wide verdict before falling back to the format list,
    # whose d/m entries would otherwise claim an ambiguous m/d value first.
    m = SLASH_DATE.match(s)
    if m and month_first is not None:
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        mo, day = (a, b) if month_first else (b, a)
        rest = s[m.end():].strip()
        hh = mi = ss = 0
        ok = not rest
        for tf in TIME_FORMATS:
            if ok:
                break
            try:
                t = datetime.strptime(rest, tf)
            except ValueError:
                continue
            hh, mi, ss, ok = t.hour, t.minute, t.second, True
        if ok:
            try:
                return datetime(year, mo, day, hh, mi, ss)
            except ValueError:
                pass

    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def day_of(v, month_first=None):
    d = parse_dt(v, month_first)
    return d.strftime("%Y-%m-%d") if d else None


DATE_COLUMNS = ("SubmissionDate", "date", "starttime", "endtime")


def date_order_of(rows):
    return detect_month_first(r.get(c) for r in rows for c in DATE_COLUMNS)


def read_csvs(folder, pattern="*.csv"):
    """Return {stem: [row dicts]} for every CSV in a folder."""
    out = {}
    if not folder.exists():
        return out
    for p in sorted(folder.glob(pattern)):
        if p.name.startswith("~$"):
            continue
        try:
            with p.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
        except UnicodeDecodeError:
            with p.open("r", encoding="latin-1", newline="") as f:
                rows = list(csv.DictReader(f))
        rows = [{norm(k): v for k, v in r.items() if k} for r in rows]
        if rows:
            out[p.stem] = rows
            print(f"    read {p.name}: {len(rows)} rows")
    return out


# ----------------------------------------------------------------------
#  export-shape normalisation
# ----------------------------------------------------------------------
def collect_multi(row, field, choice_values):
    """
    Rebuild a select_multiple answer as a list of codes, whichever way the
    export encoded it.
    """
    raw = row.get(field)
    if not is_missing(raw):
        parts = [p for p in re.split(r"[\s,;|]+", norm(raw)) if p]
        if parts:
            return parts
    # expanded binary columns: Q2_1, Q2_2, ...
    hits = []
    for v in choice_values:
        col = f"{field}_{v}"
        if col in row and to_num(row[col]) == 1:
            hits.append(v)
    return hits


def read_geo(row, field="gps"):
    """geopoint as split columns, or one space-separated cell."""
    for lat_k, lon_k in ((f"{field}-Latitude", f"{field}-Longitude"),
                         (f"{field}_Latitude", f"{field}_Longitude"),
                         (f"{field}Latitude", f"{field}Longitude")):
        if lat_k in row and lon_k in row:
            la, lo = to_num(row[lat_k]), to_num(row[lon_k])
            if la is not None and lo is not None:
                return la, lo, to_num(row.get(f"{field}-Accuracy") or row.get(f"{field}_Accuracy"))
    raw = row.get(field)
    if not is_missing(raw):
        parts = norm(raw).split()
        if len(parts) >= 2:
            la, lo = to_num(parts[0]), to_num(parts[1])
            if la is not None and lo is not None:
                acc = to_num(parts[3]) if len(parts) > 3 else None
                return la, lo, acc
    return None, None, None


def wide_repeats(row, repeat_name, inner_fields):
    """
    Pull a flattened repeat group out of a wide export, e.g.
    sample_type_details_1_total_samples_collect. Returns a list of dicts.
    """
    out, i = [], 1
    while True:
        got, found = {}, False
        for f in inner_fields:
            for pat in (f"{repeat_name}_{i}_{f}", f"{repeat_name}-{i}-{f}", f"{f}_{i}"):
                if pat in row and not is_missing(row[pat]):
                    got[f] = row[pat]
                    found = True
                    break
        if not found:
            break
        out.append(got)
        i += 1
        if i > 40:
            break
    return out


def wide_inner_repeats(row, outer, inner_fields, outer_name="sample_type_details",
                       inner_name="samples_detail"):
    """
    Pull the *inner* repeat of a nested repeat group out of a wide export.

    SurveyCTO flattens a nested repeat by suffixing both indices onto the bare
    field name -- price_sample_1_2 is the second sample of the first sample
    type -- rather than by qualifying it with the group names. Older exports do
    write the fully qualified form, so both are tried.
    """
    out, j = [], 1
    while True:
        got, found = {}, False
        for f in inner_fields:
            for pat in (f"{f}_{outer}_{j}",
                        f"{outer_name}_{outer}_{inner_name}_{j}_{f}",
                        f"{outer_name}-{outer}-{inner_name}-{j}-{f}"):
                if pat in row and not is_missing(row[pat]):
                    got[f] = row[pat]
                    found = True
                    break
        if not found:
            break
        out.append(got)
        j += 1
        if j > 40:
            break
    return out


def fold_other_specify(fields, rows, code_field, other_field, other_codes=("777", "99", "996", "9999")):
    """
    Fold an "Other (specify)" free-text answer back into the coded field.

    Most real market and locality answers come in as Other + free text, so
    left alone every one of them collapses into a single "Other" bar and the
    market filter stops being able to tell two cities apart. The text becomes
    a synthetic code ("777|raja bazar") whose label is the text itself, so the
    filter bar, the coverage table and every chart keep working unchanged.

    Returns (labels, order) to be merged into the choice map shipped to the
    browser.
    """
    if code_field not in fields or other_field not in fields:
        return {}, []
    ci, oi = fields.index(code_field), fields.index(other_field)
    labels, order = {}, []
    for r in rows:
        code = r[ci]
        if code is None or str(code) not in other_codes:
            continue
        txt = norm(r[oi] or "")
        if not txt:
            continue
        # Spelling and capitalisation of the same place drift between
        # enumerators; matching on a squashed key merges "Raja Bazar" with
        # "raja  bazar" without pretending to fix genuine typos.
        key = re.sub(r"[^a-z0-9]+", " ", txt.lower()).strip()
        syn = f"{code}|{key}"
        if syn not in labels:
            labels[syn] = txt[0].upper() + txt[1:]
            order.append(syn)
        r[ci] = syn
    return labels, order


# ----------------------------------------------------------------------
#  codebook access
# ----------------------------------------------------------------------
class Book:
    def __init__(self, cb, form):
        self.q = cb["forms"][form]["questions"]
        self.c = cb["forms"][form]["choices"]

    def kind(self, name):
        return (self.q.get(name) or {}).get("type", "")

    def list_of(self, name):
        return (self.q.get(name) or {}).get("list_name")

    def values(self, name):
        ln = self.list_of(name)
        return [c["value"] for c in self.c.get(ln, [])] if ln else []

    def label(self, name, fallback=None):
        q = self.q.get(name) or {}
        return q.get("label") or fallback or name

    def choice_map(self, name):
        ln = self.list_of(name)
        if not ln:
            return {}
        return {c["value"]: c["label"] for c in self.c.get(ln, [])}

    def choice_order(self, name):
        ln = self.list_of(name)
        return [c["value"] for c in self.c.get(ln, [])] if ln else []


# ----------------------------------------------------------------------
#  AWARENESS
# ----------------------------------------------------------------------
# resp_id replaced From_ID in the Jul-2026 instrument revision; market_name_other
# arrived with it, and carries the real market name whenever the enumerator
# picked "Other" -- which in this fieldwork is most of the time.
AW_META = ["resp_id", "Consent", "Data_Collector", "city", "market_name",
           "market_name_other", "Q1", "Type_of_survey", "survey_status"]

AW_RS = ["type_of_vendor", "Q2", "Q3", "Q3_b",
         "Fresh_Turmeric_Roots", "Dried_Turmeric_Roots", "Loose_Turmeric_Powder",
         "Packaged_Branded_Turmeric_Powder", "Packaged_Unbranded_Turmeric_Powder",
         "Q9", "Q10", "Q11", "Q12", "Q15", "Q16", "Q17", "Q18",
         "Q19", "Q20", "Q21", "Q22", "Q23", "Q24", "Q25", "Q26", "Q28", "Q29",
         "Q30", "Q31", "Q32", "Q33", "Q34", "Q35", "Q33_i", "Q33_ii",
         "Q38", "Q39", "Q40", "Q41", "Q42", "Q35_i", "Q35_iii", "Q35_iv", "Q36"]

AW_CS = ["Q_1", "Q_2", "Q_3", "Q_4", "Q_5", "Q_6", "Q_7", "Q_8", "Q_9", "Q_10",
         "Q_11", "Q_12", "Q_13", "Q_14", "Q_15", "Q_16", "Q_16_unit", "Q_16b", "Q_16_b",
         "Q_17", "Q_18", "Q_19", "Q_20", "Q_22", "Q_23", "Q_24", "Q_25", "Q_26",
         "Q_28", "Q_29", "Q_30", "Q_31", "Q_32", "Q_33", "Q_34", "Q_35", "Q_36",
         "Q_37", "Q_38", "Q_40", "Q_41", "Q_42",
         "Q_57a", "Q_57b", "Q_57c", "Q_57c_i", "Q_57_i", "Q_57_ii", "Q_57_iii",
         "Q_57d", "Q_57e", "Q_57f", "Q_57g", "Q_57h"]


def build_awareness(book, tables):
    rows_in = []
    for name, rows in tables.items():
        # the main awareness table is the one carrying Consent / Type_of_survey
        if rows and ("Consent" in rows[0] or "Type_of_survey" in rows[0]):
            rows_in.extend(rows)
    if not rows_in:
        for rows in tables.values():
            rows_in.extend(rows)

    # geo_2 is the instrument's GPS question for the awareness interview
    # itself (separate from, and named differently to, the sampling side's
    # "gps" vendor-visit field). Not every export carries it yet -- read_geo
    # returns (None, None, None) when the column is absent, same as it does
    # for a skipped fix, so this stays harmless until real coordinates land.
    fields = ["key", "date", "dur", "lat", "lon", "acc"] + AW_META + AW_RS + AW_CS
    order = date_order_of(rows_in)
    out = []
    for r in rows_in:
        rec = []
        d = day_of(r.get("SubmissionDate") or r.get("endtime") or r.get("starttime") or r.get("date"), order)
        dur = to_int(r.get("duration"))
        if dur is None:
            st, en = parse_dt(r.get("starttime"), order), parse_dt(r.get("endtime"), order)
            dur = int((en - st).total_seconds()) if st and en else None
        lat, lon, acc = read_geo(r, field="geo_2")
        rec.append(norm(r.get("KEY") or r.get("instanceID") or ""))
        rec.append(d)
        rec.append(dur)
        rec.append(lat)
        rec.append(lon)
        rec.append(acc)
        for f in fields[6:]:
            k = book.kind(f)
            if k == "select_multiple":
                rec.append(collect_multi(r, f, book.values(f)) or None)
            elif k in ("integer", "decimal"):
                rec.append(to_num(r.get(f)))
            else:
                v = r.get(f)
                rec.append(None if is_missing(v) else norm(v))
        out.append(rec)

    out.sort(key=lambda x: (x[1] or "9999", x[0]))
    return fields, out


# ----------------------------------------------------------------------
#  TURMERIC SAMPLING
# ----------------------------------------------------------------------
TS_MAIN_FIELDS = ["enum_name", "vendor_id", "sample_city", "market_name",
                  "locality_retail", "locality_retail_other",
                  "wholesale_market", "wholesale_market_other", "vendor_name",
                  "shop_sample_type", "collected_sample_type", "size_of_shop",
                  "whole_root_display", "survey_status"]


def build_turmeric(book, tables):
    main_rows, r1_rows, r2_rows = [], [], []
    for name, rows in tables.items():
        if not rows:
            continue
        head = rows[0]
        if "vendor_id" in head or "sample_city" in head:
            main_rows.extend(rows)
        elif "total_samples_collect" in head or "current_sample_type" in head:
            r1_rows.extend(rows)
        elif "price_sample" in head or "quantity_smaple" in head:
            r2_rows.extend(rows)

    # ---- vendor level ----
    v_fields = ["key", "date", "dur", "lat", "lon", "acc", "n_types", "n_samples"] + TS_MAIN_FIELDS
    order = date_order_of(main_rows)
    vendors, by_key = [], {}
    for r in main_rows:
        key = norm(r.get("KEY") or r.get("instanceID") or r.get("vendor_id") or "")
        lat, lon, acc = read_geo(r)
        rec = {
            "key": key,
            "date": day_of(r.get("date") or r.get("SubmissionDate") or r.get("starttime"), order),
            "dur": to_int(r.get("duration")),
            "lat": lat, "lon": lon, "acc": acc,
            "n_types": 0, "n_samples": 0,
        }
        for f in TS_MAIN_FIELDS:
            if book.kind(f) == "select_multiple":
                rec[f] = collect_multi(r, f, book.values(f)) or None
            else:
                v = r.get(f)
                rec[f] = None if is_missing(v) else norm(v)
        vendors.append(rec)
        by_key[key] = rec

    # ---- sample level (long repeats preferred, wide as fallback) ----
    s_fields = ["vkey", "date", "city", "market", "shop_size", "enum",
                "type", "basis", "price", "qty", "price_per_kg"]
    samples = []

    r1_by_key = {norm(r.get("KEY", "")): r for r in r1_rows}

    def emit(vrec, ttype, basis, price, qty):
        ppk = None
        if price is not None and qty:
            ppk = round(price / (qty / 1000.0), 1)
        samples.append([
            vrec["key"], vrec["date"], vrec["sample_city"], vrec["market_name"],
            vrec["size_of_shop"], vrec["enum_name"],
            ttype, basis, price, qty, ppk,
        ])

    if r2_rows:
        for r in r2_rows:
            parent = norm(r.get("PARENT_KEY", ""))
            p1 = r1_by_key.get(parent)
            vkey = norm(p1.get("PARENT_KEY", "")) if p1 else parent.split("/")[0]
            vrec = by_key.get(vkey)
            if not vrec:
                continue
            ttype = norm(p1.get("current_sample_type")) if p1 else None
            emit(vrec, ttype or None, norm(r.get("sample_type_2")) or None,
                 to_int(r.get("price_sample")), to_int(r.get("quantity_smaple")))
    else:
        # wide export fallback
        for r in main_rows:
            vrec = by_key.get(norm(r.get("KEY") or r.get("vendor_id") or ""))
            if not vrec:
                continue
            for i, blk in enumerate(wide_repeats(r, "sample_type_details",
                                                 ["current_sample_type", "total_samples_collect"]), 1):
                ttype = norm(blk.get("current_sample_type")) or None
                inner = wide_inner_repeats(r, i,
                                           ["sample_type_2", "price_sample", "quantity_smaple"])
                for s in inner:
                    emit(vrec, ttype, norm(s.get("sample_type_2")) or None,
                         to_int(s.get("price_sample")), to_int(s.get("quantity_smaple")))

    for s in samples:
        v = by_key.get(s[0])
        if v:
            v["n_samples"] += 1
    for v in vendors:
        v["n_types"] = len(v.get("collected_sample_type") or [])

    vendors.sort(key=lambda x: (x["date"] or "9999", x["key"]))
    v_rows = [[v.get(f) for f in v_fields] for v in vendors]
    return (v_fields, v_rows), (s_fields, samples)


def awareness_targets(book):
    """150 consented interviews in each city the awareness instrument lists."""
    per = {label: AW_PER_CITY for label in book.choice_map("city").values()}
    return per, sum(per.values())


def sampling_targets(book):
    """
    Vendor visits per city: 25 in each of its wholesale markets, plus 2 in
    each of its retail localities.

    The cascading market lists carry the city they belong to, which is what
    makes this countable: Karachi lists two wholesale markets and Quetta
    three, the other eight cities one each -- thirteen in all -- and every
    city lists four retail localities. So Karachi targets 25*2 + 2*4 = 58,
    Quetta 25*3 + 2*4 = 83, and a single-market city 25 + 8 = 33.
    """
    cities = book.choice_map("sample_city")
    per = {}
    basis = {}
    for field, shops in (("wholesale_market", SHOPS_PER_WHOLESALE),
                         ("locality_retail", SHOPS_PER_RETAIL)):
        ln = book.list_of(field)
        n_markets = 0
        for ch in book.c.get(ln, []):
            if ch["value"] == OTHER_CODE:
                continue
            city = cities.get(ch.get("city"))
            if not city:
                continue
            per[city] = per.get(city, 0) + shops
            n_markets += 1
        basis[field] = {"markets": n_markets, "vendors_each": shops}
    return per, sum(per.values()), basis


# ----------------------------------------------------------------------
#  label packs shipped to the browser
# ----------------------------------------------------------------------
def label_pack(book, names, extra=None):
    pack = {}
    for n in names:
        q = book.q.get(n)
        if not q:
            continue
        entry = {"t": book.label(n), "k": q.get("type", "")}
        cm = book.choice_map(n)
        if cm:
            entry["c"] = cm
            entry["o"] = book.choice_order(n)
        pack[n] = entry
    # synthetic "Other" codes minted by fold_other_specify, so the browser can
    # label them like any other choice
    for n, (labels, order) in (extra or {}).items():
        if not labels or n not in pack:
            continue
        pack[n].setdefault("c", {}).update(labels)
        pack[n]["o"] = [v for v in pack[n].get("o", []) if v not in labels] + order
    return pack


# ----------------------------------------------------------------------
#  quality flags
# ----------------------------------------------------------------------
def near_duplicate_names(rows, idx):
    """
    Free-text place names that differ only by spelling.

    Market and locality names typed by hand drift between enumerators --
    "Bakhsho pul" and "Bakhsho pull" are one market, and counted apart they
    quietly inflate the market count and split every market-level chart. This
    only reports them; merging is a field-team decision, not the ETL's.
    """
    names = sorted({str(r[idx]).split("|", 1)[1] for r in rows
                    if r[idx] and "|" in str(r[idx])})
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if difflib.SequenceMatcher(None, a, b).ratio() >= 0.82:
                pairs.append((a, b))
    return pairs


def quality_flags(aw_fields, aw_rows, ts_v_fields, ts_v_rows, ts_s_rows):
    fi = {f: i for i, f in enumerate(aw_fields)}
    vi = {f: i for i, f in enumerate(ts_v_fields)}
    flags = []

    durs = [r[fi["dur"]] for r in aw_rows if r[fi["dur"]]]
    durs.sort()
    if durs:
        p10 = durs[max(0, int(0.10 * len(durs)) - 1)]
        short = sum(1 for d in durs if d < max(300, p10 * 0.6))
        if short:
            flags.append({"sev": "warn", "area": "Awareness",
                          "msg": f"{short} interviews ran unusually short (below {max(300, int(p10*0.6))}s)",
                          "n": short})

    for area, rows, idx in (("Awareness", aw_rows, fi.get("date")),
                            ("Sampling", ts_v_rows, vi.get("date"))):
        if idx is None or not rows:
            continue
        undated = sum(1 for r in rows if not r[idx])
        if undated:
            flags.append({"sev": "warn", "area": area,
                          "msg": f"{undated} of {len(rows)} records carry no readable date — "
                                 f"check the export's date format", "n": undated})

    aw_no_gps = sum(1 for r in aw_rows if r[fi["lat"]] is None)
    if aw_no_gps:
        flags.append({"sev": "warn", "area": "Awareness",
                      "msg": f"{aw_no_gps} awareness interviews recorded without a GPS fix",
                      "n": aw_no_gps})

    for area, rows, idx in (("Awareness", aw_rows, fi.get("market_name")),
                            ("Sampling", ts_v_rows, vi.get("locality_retail"))):
        if idx is None:
            continue
        pairs = near_duplicate_names(rows, idx)
        if pairs:
            eg = " / ".join(pairs[0])
            flags.append({"sev": "info", "area": area,
                          "msg": f"{len(pairs)} market names differ only by spelling "
                                 f"and are counted separately (e.g. {eg})",
                          "n": len(pairs)})

    no_gps = sum(1 for r in ts_v_rows if r[vi["lat"]] is None)
    if no_gps:
        flags.append({"sev": "warn", "area": "Sampling",
                      "msg": f"{no_gps} vendor visits recorded without a usable GPS fix", "n": no_gps})

    bad_acc = sum(1 for r in ts_v_rows if (r[vi["acc"]] or 0) > 50)
    if bad_acc:
        flags.append({"sev": "info", "area": "Sampling",
                      "msg": f"{bad_acc} GPS fixes with accuracy worse than 50 m", "n": bad_acc})

    no_samp = sum(1 for r in ts_v_rows if not r[vi["n_samples"]])
    if no_samp:
        flags.append({"sev": "warn", "area": "Sampling",
                      "msg": f"{no_samp} vendor visits closed without any sample recorded", "n": no_samp})

    ppk = [s[10] for s in ts_s_rows if s[10]]
    if ppk:
        ppk.sort()
        q1 = ppk[len(ppk) // 4]
        q3 = ppk[3 * len(ppk) // 4]
        iqr = q3 - q1
        hi, lo = q3 + 3 * iqr, q1 - 3 * iqr
        out = sum(1 for p in ppk if p > hi or p < lo)
        if out:
            flags.append({"sev": "warn", "area": "Price",
                          "msg": f"{out} samples priced far outside the normal range (Rs {int(lo)}–{int(hi)}/kg)",
                          "n": out})

    dup = {}
    for r in ts_v_rows:
        v = r[vi["vendor_id"]]
        if v:
            dup[v] = dup.get(v, 0) + 1
    ndup = sum(1 for v, c in dup.items() if c > 1)
    if ndup:
        flags.append({"sev": "warn", "area": "Sampling",
                      "msg": f"{ndup} vendor IDs appear on more than one visit", "n": ndup})

    consent_i = fi["Consent"]
    refused = sum(1 for r in aw_rows if r[consent_i] == "0")
    if refused:
        flags.append({"sev": "info", "area": "Awareness",
                      "msg": f"{refused} approaches ended at the consent stage", "n": refused})

    if not flags:
        flags.append({"sev": "ok", "area": "All", "msg": "No data-quality exceptions detected", "n": 0})
    return flags


# ----------------------------------------------------------------------
#  main
# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="validate only, write nothing")
    ap.add_argument("--password", default=None,
                    help="access password used to encrypt the payload "
                         "(default: $TQ_DASHBOARD_PASSWORD, else the built-in)")
    ap.add_argument("--plaintext", action="store_true",
                    help="write an UNENCRYPTED payload — local debugging only, never publish")
    a = ap.parse_args()

    if not CB_PATH.exists():
        sys.exit("codebook/codebook.json missing — run scripts/build_codebook.py first")
    cb = json.loads(CB_PATH.read_text(encoding="utf-8"))

    print("Reading awareness data")
    aw_tables = read_csvs(IN_AW)
    print("Reading sampling data")
    ts_tables = read_csvs(IN_TS)

    if not aw_tables and not ts_tables:
        sys.exit("No CSVs found in data_in/ — drop the SurveyCTO exports there first")

    aw_book = Book(cb, "awareness")
    ts_book = Book(cb, "turmeric")

    aw_fields, aw_rows = build_awareness(aw_book, aw_tables) if aw_tables else ([], [])
    (v_fields, v_rows), (s_fields, s_rows) = (
        build_turmeric(ts_book, ts_tables) if ts_tables else (([], []), ([], []))
    )

    aw_extra = {"market_name": fold_other_specify(aw_fields, aw_rows,
                                                  "market_name", "market_name_other")}
    ts_extra = {
        "locality_retail": fold_other_specify(v_fields, v_rows,
                                              "locality_retail", "locality_retail_other"),
        "wholesale_market": fold_other_specify(v_fields, v_rows,
                                               "wholesale_market", "wholesale_market_other"),
    }
    n_folded = sum(len(lbls) for lbls, _ in list(aw_extra.values()) + list(ts_extra.values()))
    if n_folded:
        print(f"    resolved {n_folded} 'Other' market/locality names from the specify fields")

    # ---- meta -------------------------------------------------------
    fi = {f: i for i, f in enumerate(aw_fields)}
    vi = {f: i for i, f in enumerate(v_fields)}

    aw_days = sorted({r[fi["date"]] for r in aw_rows if r[fi["date"]]}) if aw_rows else []
    ts_days = sorted({r[vi["date"]] for r in v_rows if r[vi["date"]]}) if v_rows else []
    all_days = sorted(set(aw_days) | set(ts_days))

    consented = [r for r in aw_rows if r[fi["Consent"]] == "1"] if aw_rows else []
    aw_durs = sorted(r[fi["dur"]] for r in consented if r[fi["dur"]])
    ts_durs = sorted(r[vi["dur"]] for r in v_rows if r[vi["dur"]])

    def median(xs):
        return xs[len(xs) // 2] if xs else None

    aw_per_city, aw_target = awareness_targets(aw_book)
    ts_per_city, ts_target, ts_basis = sampling_targets(ts_book)

    grams = sum(s[9] for s in s_rows if s[9]) or 0
    ppks = sorted(s[10] for s in s_rows if s[10])

    meta = {
        "generated_at": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "generated_iso": datetime.now().isoformat(timespec="seconds"),
        "data_through": all_days[-1] if all_days else None,
        "first_day": all_days[0] if all_days else None,
        "field_days": len(all_days),
        "aw": {
            "n_submissions": len(aw_rows),
            "n_consented": len(consented),
            "n_refused": len(aw_rows) - len(consented),
            "consent_rate": round(100 * len(consented) / len(aw_rows), 1) if aw_rows else 0,
            "n_rs": sum(1 for r in consented if r[fi["Type_of_survey"]] == "RS"),
            "n_cs": sum(1 for r in consented if r[fi["Type_of_survey"]] == "CS"),
            "median_duration": round((median(aw_durs) or 0) / 60, 1),
            "n_enums": len({r[fi["Data_Collector"]] for r in aw_rows if r[fi["Data_Collector"]]}),
            "n_cities": len({r[fi["city"]] for r in aw_rows if r[fi["city"]]}),
            "n_markets": len({r[fi["market_name"]] for r in aw_rows if r[fi["market_name"]]}),
            "target": aw_target,
            "target_per_city": aw_per_city,
            "target_note": f"{AW_PER_CITY} consented interviews in each of "
                           f"{len(aw_per_city)} study cities",
            "scope_cities": len(aw_per_city),
            "field_days": len(aw_days),
        },
        "ts": {
            "n_vendors": len(v_rows),
            "n_samples": len(s_rows),
            "n_cities": len({r[vi["sample_city"]] for r in v_rows if r[vi["sample_city"]]}),
            "median_duration": round((median(ts_durs) or 0) / 60, 1),
            "n_enums": len({r[vi["enum_name"]] for r in v_rows if r[vi["enum_name"]]}),
            "target": ts_target,
            "target_per_city": ts_per_city,
            "target_basis": ts_basis,
            "target_note": f"{SHOPS_PER_WHOLESALE} vendors in each of "
                           f"{ts_basis['wholesale_market']['markets']} wholesale markets plus "
                           f"{SHOPS_PER_RETAIL} in each of "
                           f"{ts_basis['locality_retail']['markets']} retail localities, "
                           f"across {len(ts_per_city)} cities",
            "scope_cities": len(ts_per_city),
            "total_grams": int(grams),
            "median_price_per_kg": median(ppks),
            "field_days": len(ts_days),
            "gps_ok": sum(1 for r in v_rows if r[vi["lat"]] is not None),
        },
    }

    payload = {
        "meta": meta,
        "labels": {
            "aw": label_pack(aw_book, aw_fields[6:], aw_extra),
            "ts": label_pack(ts_book, TS_MAIN_FIELDS + ["sample_type_2"], ts_extra),
        },
        "aw": {"fields": aw_fields, "rows": aw_rows},
        "ts": {"fields": v_fields, "rows": v_rows},
        "samples": {"fields": s_fields, "rows": s_rows},
        "quality": quality_flags(aw_fields, aw_rows, v_fields, v_rows, s_rows),
    }

    print()
    print(f"  awareness : {len(aw_rows):5d} submissions  ({len(consented)} consented,"
          f" {meta['aw']['n_rs']} retailer / {meta['aw']['n_cs']} consumer)")
    print(f"  sampling  : {len(v_rows):5d} vendor visits, {len(s_rows)} physical samples")
    print(f"  field days: {len(all_days)}  ({meta['first_day']} -> {meta['data_through']})")
    print(f"  targets   : awareness {aw_target} ({meta['aw']['target_note']})")
    print(f"              sampling  {ts_target} ({meta['ts']['target_note']})")
    for city, n in sorted(ts_per_city.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"                {city:<14} {n}")
    for f in payload["quality"]:
        print(f"  [{f['sev']:4s}] {f['msg']}")

    if a.check:
        print("\n  --check: nothing written")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    password = a.password or os.environ.get("TQ_DASHBOARD_PASSWORD") or DEFAULT_PASSWORD

    # A small unencrypted header lets the login screen show how fresh the
    # build is before anyone has authenticated. It carries counts and dates
    # only — no respondent or vendor data.
    public = {
        "generated_at": meta["generated_at"],
        "data_through": meta["data_through"],
        "n_interviews": meta["aw"]["n_submissions"],
        "n_samples": meta["ts"]["n_samples"],
    }

    if a.plaintext:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        OUT.write_text(
            "/* Generated by scripts/update_dashboard.py — PLAINTEXT, do not publish. */\n"
            "window.DASHBOARD_PUBLIC = " + json.dumps(public) + ";\n"
            "window.DASHBOARD_DATA = " + body + ";\n",
            encoding="utf-8",
        )
        print(f"\n  -> {OUT.relative_to(ROOT)}  ({OUT.stat().st_size/1024:.0f} KB)  [PLAINTEXT — local use only]")
        return

    blob, n_raw, n_packed = encrypt_payload(payload, password)
    OUT.write_text(
        "/* Generated by scripts/update_dashboard.py — do not edit by hand.\n"
        f"   Built {meta['generated_at']} · data through {meta['data_through']}\n"
        "   Payload is deflate-compressed and AES-256-GCM encrypted; it is\n"
        "   decrypted in the browser with the access password. */\n"
        "window.DASHBOARD_PUBLIC = " + json.dumps(public) + ";\n"
        'window.DASHBOARD_ENC = "' + blob + '";\n',
        encoding="utf-8",
    )
    print(
        f"\n  -> {OUT.relative_to(ROOT)}  ({OUT.stat().st_size/1024:.0f} KB encrypted"
        f"  ·  {n_raw/1024:.0f} KB json -> {n_packed/1024:.0f} KB deflated)"
    )
    print(f"  encryption: AES-256-GCM, PBKDF2-SHA256 x{PBKDF2_ITERATIONS:,}")

    for stamp in stamp_index():
        print(f"  cache stamp: {stamp}")


if __name__ == "__main__":
    main()
