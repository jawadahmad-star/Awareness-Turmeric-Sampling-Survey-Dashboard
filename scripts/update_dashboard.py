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
    python scripts/update_dashboard.py --demo        # mark payload as demo
    python scripts/update_dashboard.py --check       # validate, write nothing
"""

import argparse
import base64
import csv
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


def stamp_index(version):
    """
    Pin a build stamp onto the payload's <script src> in index.html.

    GitHub Pages serves assets with a ten-minute cache, and browsers hold the
    file longer still on revalidation. Without a changing URL a returning
    viewer keeps yesterday's figures after a daily rebuild, which defeats the
    point of rebuilding daily.
    """
    idx = ROOT / "index.html"
    if not idx.exists():
        return False
    html = idx.read_text(encoding="utf-8")
    new, n = re.subn(
        r'(<script src="data/dashboard_data\.js)(\?v=[^"]*)?(")',
        lambda m: f"{m.group(1)}?v={version}{m.group(3)}",
        html,
    )
    if n and new != html:
        idx.write_text(new, encoding="utf-8")
        return True
    return False


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
    "%b %d, %Y %I:%M:%S %p", "%b %d, %Y %H:%M:%S", "%B %d, %Y %I:%M:%S %p",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y",
    "%d-%b-%Y", "%d-%m-%Y",
]


def parse_dt(v):
    if is_missing(v):
        return None
    s = norm(v).replace("Z", "")
    s = re.sub(r"([+-]\d{2}:?\d{2})$", "", s).strip()
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


def day_of(v):
    d = parse_dt(v)
    return d.strftime("%Y-%m-%d") if d else None


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
AW_META = ["From_ID", "Consent", "Data_Collector", "city", "market_name",
           "Q1", "Type_of_survey", "survey_status"]

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
    out = []
    for r in rows_in:
        rec = []
        d = day_of(r.get("SubmissionDate") or r.get("endtime") or r.get("starttime") or r.get("date"))
        dur = to_int(r.get("duration"))
        if dur is None:
            st, en = parse_dt(r.get("starttime")), parse_dt(r.get("endtime"))
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
                  "locality_retail", "wholesale_market", "vendor_name",
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
    vendors, by_key = [], {}
    for r in main_rows:
        key = norm(r.get("KEY") or r.get("instanceID") or r.get("vendor_id") or "")
        lat, lon, acc = read_geo(r)
        rec = {
            "key": key,
            "date": day_of(r.get("date") or r.get("SubmissionDate") or r.get("starttime")),
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
                inner = wide_repeats(r, f"sample_type_details_{i}_samples_detail",
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


# ----------------------------------------------------------------------
#  label packs shipped to the browser
# ----------------------------------------------------------------------
def label_pack(book, names):
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
    return pack


# ----------------------------------------------------------------------
#  quality flags
# ----------------------------------------------------------------------
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
    ap.add_argument("--demo", action="store_true", help="tag the payload as demonstration data")
    ap.add_argument("--check", action="store_true", help="validate only, write nothing")
    ap.add_argument("--aw-target", type=int, default=1000)
    ap.add_argument("--ts-target", type=int, default=500)
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

    grams = sum(s[9] for s in s_rows if s[9]) or 0
    ppks = sorted(s[10] for s in s_rows if s[10])

    meta = {
        "generated_at": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "generated_iso": datetime.now().isoformat(timespec="seconds"),
        "data_through": all_days[-1] if all_days else None,
        "first_day": all_days[0] if all_days else None,
        "field_days": len(all_days),
        "is_demo": bool(a.demo),
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
            "target": a.aw_target,
            "field_days": len(aw_days),
        },
        "ts": {
            "n_vendors": len(v_rows),
            "n_samples": len(s_rows),
            "n_cities": len({r[vi["sample_city"]] for r in v_rows if r[vi["sample_city"]]}),
            "median_duration": round((median(ts_durs) or 0) / 60, 1),
            "n_enums": len({r[vi["enum_name"]] for r in v_rows if r[vi["enum_name"]]}),
            "target": a.ts_target,
            "total_grams": int(grams),
            "median_price_per_kg": median(ppks),
            "field_days": len(ts_days),
            "gps_ok": sum(1 for r in v_rows if r[vi["lat"]] is not None),
        },
    }

    payload = {
        "meta": meta,
        "labels": {
            "aw": label_pack(aw_book, aw_fields[6:]),
            "ts": label_pack(ts_book, TS_MAIN_FIELDS + ["sample_type_2"]),
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
    for f in payload["quality"]:
        print(f"  [{f['sev']:4s}] {f['msg']}")

    if a.check:
        print("\n  --check: nothing written")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    password = a.password or os.environ.get("TQ_DASHBOARD_PASSWORD") or DEFAULT_PASSWORD

    # A small unencrypted header lets the page show freshness and the demo
    # badge on the login screen, before anyone has authenticated. It carries
    # counts and dates only — no respondent or vendor data.
    public = {
        "generated_at": meta["generated_at"],
        "data_through": meta["data_through"],
        "is_demo": meta["is_demo"],
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

    version = datetime.now().strftime("%Y%m%d%H%M%S")
    if stamp_index(version):
        print(f"  cache stamp: index.html -> dashboard_data.js?v={version}")


if __name__ == "__main__":
    main()
