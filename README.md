# Turmeric Quality Dashboard

Fieldwork and analysis dashboard for the Turmeric Quality Programme — the
**Awareness Follow-up** survey (retailer and consumer instruments) and the
**Market Sampling** exercise collecting physical turmeric samples for
laboratory lead testing.

**Live:** https://turmericstudy.rs.org.pk

Built by Research Solutions (M&A Research Solutions LLC) · www.rs.org.pk

---

## Updating the dashboard each day

1. Export the two surveys from SurveyCTO as CSV.
2. Drop the files here:

   ```
   data_in/awareness/     awareness survey export
   data_in/turmeric/      sampling export + the two repeat-group exports
                          (sample_type_details, samples_detail)
   ```

   Replace whatever is already there — the build always reads the whole folder.

3. Double-click **`UPDATE DASHBOARD.bat`**.

That rebuilds the encrypted payload, commits it, and pushes to GitHub. The live
site refreshes about a minute later.

From a terminal, the same thing:

```bash
python scripts/daily_update.py             # build, commit, push
python scripts/daily_update.py --dry-run   # build only, change nothing
python scripts/daily_update.py --no-push   # build and commit, push later
```

### If the instrument changes

Put the new XLSForm in `instruments/` under the same filename. The next run
detects that it is newer than the codebook and rebuilds the question and
choice labels automatically — no code change needed.

---

## Security

The dashboard is served from GitHub Pages, which makes every file in this
repository publicly readable. A login screen written in JavaScript does not
change that, so:

- **The data payload is encrypted.** `data/dashboard_data.js` holds an
  AES-256-GCM ciphertext (PBKDF2-SHA256, 250,000 iterations). It is decrypted
  in the browser only after the correct password is entered. Downloading the
  file without the password yields nothing usable.
- **Raw exports are never committed.** `.gitignore` blocks every CSV/XLSX in
  `data_in/`. Vendor names, GPS fixes and respondent-level answers stay on the
  machine that runs the build.
- **The password is not in the repository.** It is only used at build time to
  derive the encryption key.

To change the access password, rebuild with the new one and republish:

```bash
python scripts/daily_update.py --password "NEW_PASSWORD"
```

Everyone must then be given the new password — old links keep working but the
old password stops decrypting.

> Note: encryption protects the data at rest on a public host. Anyone who has
> the password can still save what they see, so treat it as a controlled
> distribution list, not as a technical guarantee against a legitimate user
> re-sharing figures.

---

## What is in here

```
index.html                    the dashboard (eleven panels)
assets/theme.css              design tokens, light + dark, print styles
assets/app.js                 decryption, filters, aggregation, all charts
data/dashboard_data.js        encrypted payload  ← the only file that changes daily
codebook/codebook.json        questions, labels and choice lists from the XLSForms
instruments/                  the two XLSForm files, as the source of truth
data_in/                      drop CSVs here (never committed)
scripts/
  build_codebook.py           XLSForms  -> codebook.json
  update_dashboard.py         CSVs      -> encrypted payload
  make_demo_data.py           generates demonstration CSVs
  daily_update.py             the daily runner
UPDATE DASHBOARD.bat          double-click wrapper for the above
CNAME                         custom domain for GitHub Pages
```

### How the data flows

```
XLSForm instruments ──> build_codebook.py ──> codebook.json
                                                   │
SurveyCTO CSV exports ─────────────────────────────┴──> update_dashboard.py
                                                              │
                                          deflate + AES-256-GCM
                                                              │
                                                   data/dashboard_data.js
                                                              │
                                        browser decrypts with password
                                                              │
                                    app.js aggregates in-page ──> charts
```

Aggregation happens in the browser, not in Python. The payload is
record-level, which is what lets the filter bar re-cut all eleven panels
instantly without a server.

---

## Demonstration data

Until real exports arrive, the site runs on generated data so the client can
see a complete dashboard. Every build made this way is labelled **DEMO DATA**
on the login screen, in the header and in a banner across the top.

To regenerate it:

```bash
python scripts/make_demo_data.py
python scripts/daily_update.py --demo
```

**To switch to real data:** delete the CSVs in `data_in/`, drop the real
exports in, and run `UPDATE DASHBOARD.bat` — without `--demo`. The demo
labelling disappears on its own.

---

## Export shapes the build accepts

SurveyCTO can export the same form several ways, so the build tolerates all of
these without configuration:

| Element | Handled forms |
|---|---|
| `select_multiple` | `"1 3 4"` in one column, **or** expanded `Q2_1`, `Q2_2` binary columns |
| `geopoint` | `gps-Latitude` / `gps-Longitude` columns, **or** a single space-separated `gps` cell |
| Repeat groups | separate long CSVs joined on `PARENT_KEY`, **or** flattened wide columns |
| Dates | ISO, `Aug 08, 2026 5:32:11 PM`, `dd/mm/yyyy` and several others |

Unrecognised columns are ignored rather than fatal, so adding a question to the
instrument never breaks the build.

---

## Requirements

Python 3.9+ with:

```bash
pip install -r scripts/requirements.txt
```

The page itself needs no build step. It must be served over HTTPS or from
`localhost` — browsers only expose the WebCrypto API used for decryption in a
secure context. Opening `index.html` directly from disk (`file://`) will not
decrypt.

To preview locally:

```bash
python -m http.server 8791
# then open http://127.0.0.1:8791/
```
