"""
make_demo_data.py
=================
Writes demonstration CSVs shaped exactly like a SurveyCTO wide export, so the
dashboard can be shown to a client before real fieldwork data lands.

The moment real exports are dropped into data_in/, delete these files (or just
overwrite them with the real ones) and run update_dashboard.py again -- nothing
downstream needs to change, because the ETL reads columns, not this script.

Shapes produced:
  data_in/awareness/awareness_survey.csv      one row per submission
  data_in/turmeric/turmeric_sampling.csv      one row per vendor visit
  data_in/turmeric/sample_type_details.csv    repeat level 1 (per turmeric type)
  data_in/turmeric/samples_detail.csv         repeat level 2 (per physical sample)

Usage:
    python scripts/make_demo_data.py [--seed 20260810]
"""

import argparse
import csv
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CB = json.loads((ROOT / "codebook" / "codebook.json").read_text(encoding="utf-8"))

AW_OUT = ROOT / "data_in" / "awareness" / "awareness_survey.csv"
TS_MAIN = ROOT / "data_in" / "turmeric" / "turmeric_sampling.csv"
TS_R1 = ROOT / "data_in" / "turmeric" / "sample_type_details.csv"
TS_R2 = ROOT / "data_in" / "turmeric" / "samples_detail.csv"

# Approximate city centroids -- demo GPS points are jittered around these so the
# map panel shows believable clustering per city.
CITY_GEO = {
    "1": (24.8607, 67.0011),   # Karachi
    "2": (25.3960, 68.3578),   # Hyderabad
    "3": (34.0151, 71.5249),   # Peshawar
    "4": (33.6844, 73.0479),   # Islamabad
    "5": (33.5651, 73.0169),   # Rawalpindi
    "6": (30.1798, 66.9750),   # Quetta
    "7": (31.5204, 74.3587),   # Lahore
    "8": (31.4504, 73.1350),   # Faisalabad
    "9": (30.1575, 71.5249),   # Multan
    "10": (32.1877, 74.1945),  # Gujranwala
}
AW_CITY_GEO = {"1": CITY_GEO["1"], "2": CITY_GEO["2"], "3": CITY_GEO["3"], "4": (34.1989, 72.0231)}

# Retail price bands in Rs/kg by turmeric type -- drives the price analytics panel.
PRICE_PER_KG = {
    "1": (560, 900),    # whole dried roots
    "2": (420, 700),    # loose powder
    "3": (650, 1000),   # packaged unbranded
    "4": (1050, 1850),  # packaged branded
}

# Awareness campaign intensity by city. Karachi/Hyderabad were the campaign
# districts, so recall and lead knowledge are deliberately higher there --
# this is what makes the demo dashboard show a real-looking contrast.
CITY_CAMPAIGN = {"1": 0.78, "2": 0.66, "3": 0.41, "4": 0.29}

AW_MARKET_BY_CITY = {
    "1": ["EMK", "BMK"],
    "2": ["FKPBH", "TJH"],
    "3": ["BBP", "PMP"],
    "4": ["BBP", "PMP"],
}


def choices(form, list_name):
    return [c["value"] for c in CB["forms"][form]["choices"].get(list_name, [])]


def pick(seq, weights=None):
    return random.choices(list(seq), weights=weights, k=1)[0]


def multi(seq, weights, lo=1, hi=3):
    """Pick a space-separated select_multiple value, SurveyCTO style."""
    seq = list(seq)
    n = min(random.randint(lo, hi), len(seq))
    out, pool, w = [], seq[:], list(weights)
    for _ in range(n):
        i = random.choices(range(len(pool)), weights=w, k=1)[0]
        out.append(pool.pop(i))
        w.pop(i)
    return " ".join(out)


def skewed(vals, weights):
    return random.choices(vals, weights=weights, k=1)[0]


def jitter(latlon, km=6.0):
    lat, lon = latlon
    return (
        round(lat + random.uniform(-km, km) / 111.0, 6),
        round(lon + random.uniform(-km, km) / (111.0 * 0.87), 6),
    )


def field_days(n_days, end):
    """Working days ending at `end`, Sundays dropped like real fieldwork."""
    days, d = [], end
    while len(days) < n_days:
        if d.weekday() != 6:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


# ======================================================================
#  AWARENESS SURVEY
# ======================================================================
def gen_awareness(n_target, days):
    rows = []
    collectors = choices("awareness", "Data_Collector")
    scale = ["1", "2", "3", "4", "5", "6", "7"]          # commonality scale
    info = choices("awareness", "get_information")

    key_n = 0
    for day_i, d in enumerate(days):
        # a ramp-up at the start of fieldwork, then a steady rate
        ramp = min(1.0, 0.35 + 0.09 * day_i)
        n_today = max(6, int(random.gauss(n_target / len(days), 6) * ramp))
        for _ in range(n_today):
            key_n += 1
            city = pick(["1", "2", "3", "4"], [0.36, 0.24, 0.24, 0.16])
            camp = CITY_CAMPAIGN[city]
            collector = pick(collectors)
            start = datetime.combine(d, datetime.min.time()) + timedelta(
                hours=random.randint(9, 17), minutes=random.randint(0, 59)
            )
            dur = int(max(240, random.gauss(1500 if random.random() > 0.08 else 520, 420)))
            end = start + timedelta(seconds=dur)

            consent = "1" if random.random() < 0.91 else "0"
            r = {
                "KEY": f"uuid:aw-{key_n:05d}",
                "SubmissionDate": end.strftime("%b %d, %Y %I:%M:%S %p"),
                "starttime": start.strftime("%b %d, %Y %I:%M:%S %p"),
                "endtime": end.strftime("%b %d, %Y %I:%M:%S %p"),
                "duration": dur,
                "username": f"enum{collector.zfill(2)}",
                "deviceid": f"dev-{int(collector):02d}",
                "From_ID": 1000 + key_n,
                "Consent": consent,
                "Data_Collector": collector,
                "city": city,
                "market_name": pick(AW_MARKET_BY_CITY[city]),
            }
            if consent == "0":
                r["no_consent"] = pick(["Busy with customers", "Not interested", "Owner not present"])
                r["survey_status"] = pick(["3", "4", "5", "6"], [0.4, 0.3, 0.15, 0.15])
                rows.append(r)
                continue

            r["Q1"] = pick(["1", "0"], [0.34, 0.66])
            stype = pick(["RS", "CS"], [0.56, 0.44])
            r["Type_of_survey"] = stype

            # ---------------- lead & adulteration backbone -------------
            # One latent "exposure" score per respondent keeps the lead,
            # media and adulteration answers correlated the way they are
            # in real survey data.
            exposure = min(1.0, max(0.0, random.gauss(camp, 0.22)))
            knows_lead = random.random() < (0.22 + 0.55 * exposure)
            heard_lead_turmeric = knows_lead and random.random() < (0.3 + 0.55 * exposure)

            def commonality():
                """Higher exposure -> more likely to say adulteration is common."""
                if random.random() < 0.05:
                    return pick(["6", "7"])
                w = [0.10 - 0.07 * exposure, 0.13, 0.24, 0.30 + 0.12 * exposure,
                     0.14 + 0.16 * exposure, 0.05, 0.02]
                return skewed(scale, [max(0.01, x) for x in w])

            media_src = multi(info, [0.10, 0.12, 0.10, 0.20, 0.05, 0.18, 0.04, 0.05, 0.06, 0.08, 0.02], 1, 4)

            if stype == "RS":
                r["type_of_vendor"] = pick(["1", "2"], [0.28, 0.72])
                r["Q2"] = multi(["1", "2", "3", "4", "5"], [0.16, 0.26, 0.30, 0.16, 0.12], 2, 4)
                sold = r["Q2"].split()
                r["Q3"] = pick(sold)
                r["Q3_b"] = pick(sold)
                # sales mix -- five integers summing to 100
                parts = [random.random() * (1.6 if s in sold else 0.15) for s in ["1", "2", "3", "5", "4"]]
                tot = sum(parts) or 1
                shares = [int(round(100 * p / tot)) for p in parts]
                shares[0] += 100 - sum(shares)
                for fld, val in zip(
                    ["Fresh_Turmeric_Roots", "Dried_Turmeric_Roots", "Loose_Turmeric_Powder",
                     "Packaged_Branded_Turmeric_Powder", "Packaged_Unbranded_Turmeric_Powder"], shares):
                    r[fld] = max(0, val)
                r["Q9"] = pick(["1", "2"], [0.58, 0.42])
                r["Q10"] = pick(["1", "2", "3"], [0.46, 0.16, 0.38])
                r["Q11"] = pick(["1", "2", "3", "4"], [0.55, 0.13, 0.30, 0.02])
                r["Q12"] = pick(["1", "2", "3"], [0.14, 0.58, 0.28])
                if r["Q10"] == "2":
                    r["Q15"] = multi(["1", "2", "3", "4", "5"], [0.4, 0.2, 0.12, 0.22, 0.06], 1, 2)
                r["Q16"] = pick(["1", "2", "3", "4", "5", "6", "7"], [0.44, 0.16, 0.14, 0.05, 0.12, 0.07, 0.02])
                r["Q17"] = pick(["1", "2"], [0.63, 0.37])
                r["Q18"] = multi(["1", "2", "3", "4", "5", "6"], [0.34, 0.26, 0.09, 0.09, 0.19, 0.03], 1, 3)

                r["Q19"] = commonality()
                if r["Q19"] in ("3", "4", "5"):
                    r["Q20"] = commonality()
                    r["Q21"] = commonality()
                    r["Q22"] = commonality()
                    r["Q23"] = commonality()
                    r["Q24"] = media_src
                if r.get("Q20") in ("3", "4", "5"):
                    r["Q25"] = multi(["1", "2", "3", "4", "5", "6", "7"], [0.34, 0.2, 0.24, 0.05, 0.1, 0.02, 0.05], 1, 2)
                    r["Q26"] = multi(["1", "2", "3", "4", "5", "6", "7", "8"],
                                     [0.24, 0.12 + 0.30 * exposure, 0.14, 0.14, 0.04, 0.08, 0.02, 0.06], 1, 2)
                if r.get("Q22") in ("3", "4", "5"):
                    r["Q28"] = multi(["1", "2", "3", "4", "5", "6", "7"], [0.3, 0.2, 0.26, 0.05, 0.1, 0.02, 0.07], 1, 2)
                    r["Q29"] = multi(["1", "2", "3", "4", "5", "6", "7", "8"],
                                     [0.26, 0.10 + 0.28 * exposure, 0.16, 0.14, 0.04, 0.08, 0.02, 0.06], 1, 2)
                r["Q30"] = multi([str(i) for i in range(1, 9)], [0.2, 0.16, 0.14, 0.12, 0.1, 0.1, 0.1, 0.08], 1, 3)
                r["Q31"] = multi([str(i) for i in range(1, 8)], [0.2, 0.18, 0.16, 0.14, 0.12, 0.1, 0.1], 1, 2)
                if "7" not in r["Q31"].split():
                    r["Q32"] = multi(["1", "2"], [0.85, 0.15], 1, 1)

                r["Q33"] = "1" if knows_lead else "0"
                if knows_lead:
                    r["Q34"] = pick(["1", "2", "3", "4"], [0.05, 0.14, 0.31, 0.50])
                    r["Q35"] = multi(["1", "2", "3"], [0.42, 0.44, 0.14], 1, 2)
                    r["Q33_i"] = media_src
                lead_adult = "2" in (r.get("Q26", "") + " " + r.get("Q29", "")).split()
                if lead_adult:
                    r["Q33_ii"] = media_src
                    srcs = set(r["Q33_ii"].split())
                    if srcs & {"3", "4", "6", "8"}:
                        r["Q38"] = multi(["1", "2", "3", "4"], [0.36, 0.28, 0.30, 0.06], 1, 2)
                        r["Q40"] = pick(["1", "2", "3", "4"], [0.36, 0.31, 0.18, 0.15])
                        r["Q41"] = pick(["1", "2", "3", "4", "5"], [0.44, 0.29, 0.17, 0.06, 0.04])
                        r["Q42"] = pick(["1", "0"], [0.17, 0.83])
                    if srcs & {"4", "6"}:
                        r["Q39"] = multi(["1", "2", "3", "4", "5"], [0.24, 0.28, 0.18, 0.12, 0.18], 1, 2)
                    r["Q35_i"] = pick(["1", "0"], [0.34 + 0.25 * exposure, 0.66 - 0.25 * exposure])
                    r["Q35_iii"] = pick(["1", "0"], [0.21, 0.79])
                    if r["Q35_iii"] == "1":
                        r["Q35_iv"] = multi(["1", "2", "3"], [0.5, 0.3, 0.2], 1, 2)
                r["Q36"] = pick(["1", "2", "3", "4"], [0.04, 0.16, 0.30, 0.50])

            else:  # ---------------- consumer ----------------
                r["Q_1"] = pick(["1", "2", "3", "4"], [0.62, 0.14, 0.20, 0.04])
                if r["Q_1"] != "4":
                    r["Q_2"] = pick(["1", "2", "3"], [0.30, 0.52, 0.18])
                    r["Q_3"] = pick([str(i) for i in range(1, 8)],
                                    [0.10, 0.07, 0.05, 0.09, 0.08, 0.35, 0.26])
                    r["Q_4"] = pick(["1", "0"], [0.23, 0.77])
                    if r["Q_4"] == "1":
                        r["Q_5"] = pick([str(i) for i in range(1, 8)])
                    r["Q_6"] = pick(["male", "female", "other"], [0.53, 0.46, 0.01])
                    r["Q_7"] = pick(["<20", "20-40", "40-60", "60+"], [0.06, 0.49, 0.36, 0.09])
                    r["Q_8"] = pick(["1", "0"], [0.81, 0.19])
                    if r["Q_8"] == "1":
                        r["Q_9"] = pick(["1", "2", "3", "4", "5", "6"],
                                        [0.02, 0.05, 0.09, 0.19, 0.51, 0.14])
                        r["Q_10"] = multi(["1", "2", "3", "4", "5", "6", "7"],
                                          [0.03, 0.31, 0.29, 0.15, 0.10, 0.09, 0.03], 1, 3)
                        got = set(r["Q_10"].split())
                        if got & {"4", "5"}:
                            r["Q_11"] = multi(["1", "2", "3", "4", "5", "6", "7"],
                                              [0.2, 0.2, 0.16, 0.14, 0.12, 0.1, 0.08], 1, 3)
                            if len(r["Q_11"].split()) > 1:
                                r["Q_12"] = pick(r["Q_11"].split())
                            r["Q_16b"] = random.choice([250, 500, 750, 1000, 1500, 2000])
                            r["Q_16_b"] = pick(["gram", "Kg"], [0.6, 0.4])
                        if "4" in got:
                            r["Q_13"] = pick([str(i) for i in range(1, 6)])
                        if "5" in got:
                            r["Q_14"] = pick([str(i) for i in range(1, 6)])
                        if "6" in got:
                            r["Q_15"] = pick([str(i) for i in range(1, 6)])
                        r["Q_16"] = random.choice([100, 200, 250, 500, 750, 1000])
                        r["Q_16_unit"] = pick(["gram", "Kg", "Packets"], [0.62, 0.28, 0.10])
                        if r["Q_1"] != "2":
                            r["Q_17"] = pick(["1", "2", "3"], [0.80, 0.15, 0.05])
                        if r.get("Q_17") == "1":
                            r["Q_18"] = random.randint(3, 12)
                        else:
                            r["Q_19"] = pick(["1", "2", "3", "4", "5"], [0.44, 0.16, 0.24, 0.10, 0.06])
                            r["Q_20"] = multi([str(i) for i in range(1, 12)],
                                              [0.16, 0.14, 0.12, 0.10, 0.09, 0.08, 0.08, 0.07, 0.06, 0.06, 0.04], 1, 3)
                            r["Q_22"] = pick(["1", "2", "3"], [0.18, 0.55, 0.27])
                        r["Q_24"] = multi([str(i) for i in range(1, 6)], [0.3, 0.25, 0.2, 0.15, 0.1], 1, 2)
                        r["Q_26"] = multi(["1", "2", "3", "4", "5", "6", "7"],
                                          [0.26, 0.06, 0.14, 0.17, 0.19, 0.16, 0.02], 1, 3)

                        r["Q_28"] = pick(["1", "2", "3"],
                                         [0.48 + 0.30 * exposure, 0.30 - 0.15 * exposure, 0.22 - 0.15 * exposure])
                        if r["Q_28"] == "1":
                            r["Q_30"] = commonality()
                            r["Q_31"] = commonality()
                            r["Q_32"] = commonality()
                            r["Q_33"] = commonality()
                            r["Q_34"] = commonality()
                            r["Q_29"] = media_src
                            if r["Q_31"] in ("3", "4", "5"):
                                r["Q_35"] = multi(["1", "2", "3", "4", "5", "6", "7"],
                                                  [0.32, 0.2, 0.24, 0.05, 0.11, 0.02, 0.06], 1, 2)
                                r["Q_36"] = multi(["1", "2", "3", "4", "5", "6", "7", "8"],
                                                  [0.22, 0.12 + 0.30 * exposure, 0.15, 0.15, 0.04, 0.08, 0.02, 0.06], 1, 2)
                            if r["Q_33"] in ("3", "4", "5"):
                                r["Q_37"] = multi(["1", "2", "3", "4", "5", "6", "7"],
                                                  [0.3, 0.2, 0.25, 0.05, 0.11, 0.02, 0.07], 1, 2)
                                r["Q_38"] = multi(["1", "2", "3", "4", "5", "6", "7", "8"],
                                                  [0.24, 0.10 + 0.28 * exposure, 0.16, 0.15, 0.04, 0.08, 0.02, 0.06], 1, 2)
                        r["Q_40"] = multi([str(i) for i in range(1, 7)], [0.24, 0.2, 0.18, 0.16, 0.12, 0.1], 1, 3)
                        r["Q_41"] = multi([str(i) for i in range(1, 8)], [0.2, 0.18, 0.16, 0.14, 0.12, 0.1, 0.1], 1, 2)
                        r["Q_42"] = pick(["1", "2", "3", "4", "5", "6", "7"],
                                         [0.06, 0.13, 0.28, 0.44, 0.04, 0.03, 0.02])

                        r["Q_57a"] = "1" if knows_lead else "0"
                        if knows_lead:
                            r["Q_57c"] = pick(["1", "0"], [0.44, 0.56])
                            if r["Q_57c"] == "1":
                                r["Q_57c_i"] = multi(["1", "2", "3"], [0.42, 0.44, 0.14], 1, 2)
                            r["Q_57b"] = pick(["1", "2", "3", "4"], [0.04, 0.13, 0.30, 0.53])
                        r["Q_57_i"] = "1" if heard_lead_turmeric else "0"
                        if heard_lead_turmeric:
                            r["Q_57_ii"] = media_src
                            r["Q_57_iii"] = pick(["1", "2", "3", "4", "5"],
                                                 [0.10, 0.22, 0.34, 0.26, 0.08])
                            srcs = set(r["Q_57_ii"].split())
                            if srcs & {"3", "4", "6", "8"}:
                                r["Q_57d"] = multi(["1", "2", "3", "4"], [0.36, 0.28, 0.30, 0.06], 1, 2)
                                r["Q_57f"] = pick(["1", "2", "3", "4"], [0.34, 0.32, 0.19, 0.15])
                                r["Q_57g"] = pick(["1", "2", "3", "4", "5"], [0.46, 0.28, 0.16, 0.06, 0.04])
                                r["Q_57h"] = pick(["1", "0"], [0.15, 0.85])
                            if srcs & {"4", "6"}:
                                r["Q_57e"] = multi(["1", "2", "3", "4", "5"], [0.26, 0.27, 0.18, 0.11, 0.18], 1, 2)

            r["survey_status"] = pick(["1", "2"], [0.95, 0.05])
            rows.append(r)

    return rows[:int(n_target * 1.35)]


# ======================================================================
#  TURMERIC SAMPLING SURVEY
# ======================================================================
def gen_turmeric(n_vendors, days):
    main, r1, r2 = [], [], []
    enums = choices("turmeric", "enum_name")
    enums = [e for e in enums if e != "777"]
    mkt_place = CB["forms"]["turmeric"]["choices"]["market_place"]
    retail = CB["forms"]["turmeric"]["choices"]["retail_market"]
    by_city_ws, by_city_rt = {}, {}
    for c in mkt_place:
        by_city_ws.setdefault(c.get("city"), []).append(c["value"])
    for c in retail:
        by_city_rt.setdefault(c.get("city"), []).append(c["value"])

    vid = 1000
    per_day = max(1, n_vendors // len(days))
    for day_i, d in enumerate(days):
        ramp = min(1.0, 0.4 + 0.1 * day_i)
        for _ in range(max(2, int(random.gauss(per_day, 3) * ramp))):
            vid += 1
            city = pick(list(CITY_GEO.keys()),
                        [0.18, 0.08, 0.10, 0.09, 0.09, 0.07, 0.16, 0.09, 0.08, 0.06])
            mkt = pick(["1", "2"], [0.34, 0.66])
            en = pick(enums)
            start = datetime.combine(d, datetime.min.time()) + timedelta(
                hours=random.randint(10, 18), minutes=random.randint(0, 59))
            dur = int(max(180, random.gauss(900, 300)))
            lat, lon = jitter(CITY_GEO[city], 7)

            row = {
                "KEY": f"uuid:ts-{vid}",
                "SubmissionDate": (start + timedelta(seconds=dur)).strftime("%b %d, %Y %I:%M:%S %p"),
                "date": d.isoformat(),
                "time": start.strftime("%H:%M:%S"),
                "duration": dur,
                "username": f"ts{en}",
                "enum_name": en,
                "enum_label": next((c["label"] for c in CB["forms"]["turmeric"]["choices"]["enum_name"]
                                    if c["value"] == en), en),
                "vendor_id": str(vid),
                "sample_city": city,
                "market_name": mkt,
                "vendor_name": f"Vendor {vid}",
                "gps-Latitude": lat,
                "gps-Longitude": lon,
                "gps-Altitude": round(random.uniform(5, 520), 1),
                "gps-Accuracy": round(random.uniform(3, 18), 1),
                "size_of_shop": pick(["1", "2", "3"], [0.40, 0.42, 0.18]),
                "survey_status": pick(["1", "2"], [0.96, 0.04]),
            }
            if mkt == "1":
                pool = by_city_ws.get(city) or ["1"]
                row["wholesale_market"] = pick(pool)
            else:
                pool = by_city_rt.get(city) or ["1"]
                row["locality_retail"] = pick(pool)

            shop_types = multi(["1", "2", "3", "4"], [0.30, 0.34, 0.20, 0.16], 2, 4)
            row["shop_sample_type"] = shop_types
            avail = shop_types.split()
            n_col = random.randint(1, len(avail))
            collected = random.sample(avail, n_col)
            row["collected_sample_type"] = " ".join(sorted(collected))
            main.append(row)

            for ti, t in enumerate(sorted(collected), 1):
                n_samp = random.randint(1, 3)
                r1_key = f"{row['KEY']}/sample_type_details[{ti}]"
                r1.append({
                    "PARENT_KEY": row["KEY"],
                    "KEY": r1_key,
                    "current_sample_type": t,
                    "total_samples_collect": n_samp,
                })
                lo, hi = PRICE_PER_KG[t]
                # a city cost-of-living tilt keeps price-by-city meaningful
                city_mult = 1.0 + (0.10 if city in ("1", "4", "7") else -0.04 if city in ("6", "9") else 0.0)
                for si in range(1, n_samp + 1):
                    qty = random.choice([50, 100, 100, 150, 200, 250])
                    per_kg = random.uniform(lo, hi) * city_mult
                    basis = pick(["1", "2", "3", "4"], [0.34, 0.31, 0.28, 0.07])
                    # samples picked for brightness skew expensive -- the
                    # brightness/price link is one of the study's questions
                    if basis in ("2", "3"):
                        per_kg *= random.uniform(1.02, 1.18)
                    r2.append({
                        "PARENT_KEY": r1_key,
                        "KEY": f"{r1_key}/samples_detail[{si}]",
                        "sample_type_2": basis,
                        "price_sample": int(round(per_kg * qty / 1000)),
                        "quantity_smaple": qty,
                        "picture_sample": f"{vid}_{t}_{basis}_{si}.jpg",
                    })
    return main, r1, r2


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  {path.relative_to(ROOT)}  {len(rows)} rows x {len(cols)} cols")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20260810)
    ap.add_argument("--awareness", type=int, default=880)
    ap.add_argument("--vendors", type=int, default=430)
    ap.add_argument("--days", type=int, default=22)
    ap.add_argument("--end", default=None, help="last field day, YYYY-MM-DD")
    a = ap.parse_args()

    random.seed(a.seed)
    end = date.fromisoformat(a.end) if a.end else date.today() - timedelta(days=1)
    days = field_days(a.days, end)

    print("Demo data (SurveyCTO export shape)")
    aw = gen_awareness(a.awareness, days)
    write_csv(AW_OUT, aw)

    m, s1, s2 = gen_turmeric(a.vendors, days)
    write_csv(TS_MAIN, m)
    write_csv(TS_R1, s1)
    write_csv(TS_R2, s2)
    print(f"  field window: {days[0]} -> {days[-1]}  ({len(days)} days)")


if __name__ == "__main__":
    main()
