"""
daily_update.py
===============
The one command to run each day once new SurveyCTO exports have been dropped
into data_in/.

It does four things, and stops at the first one that fails:

  1. rebuilds codebook.json if either instrument has been edited
  2. rebuilds data/dashboard_data.js (encrypted) from the CSVs in data_in/
  3. commits the rebuilt payload
  4. pushes to GitHub, which redeploys turmericstudy.rs.org.pk

Raw CSVs are never committed — they hold vendor names, GPS fixes and
respondent-level answers, and .gitignore keeps them out of the repository.
Only the encrypted payload is published.

Usage:
    python scripts/daily_update.py                 # build, commit, push
    python scripts/daily_update.py --no-push       # build and commit only
    python scripts/daily_update.py --dry-run       # build only, touch nothing
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# A double-clicked .bat runs in a legacy console codepage that cannot encode
# every character in these messages. Without this, a stray dash aborts the
# whole update with a UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
LOGS = ROOT / "logs"
PAYLOAD = ROOT / "data" / "dashboard_data.js"
CODEBOOK = ROOT / "codebook" / "codebook.json"
INSTRUMENTS = ROOT / "instruments"

LOG_LINES = []


def say(msg="", bullet=""):
    line = f"{bullet}{msg}" if bullet else msg
    print(line)
    LOG_LINES.append(line)


def run(cmd, cwd=ROOT, check=True, quiet=False):
    # The child scripts print UTF-8; decoding with the console codepage instead
    # is what turns a middot into "Â·".
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", shell=False)
    out = (p.stdout or "") + (p.stderr or "")
    if not quiet:
        for ln in out.strip().splitlines():
            say("      " + ln)
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}")
    return p.returncode, out


def git(*args, check=True, quiet=True):
    return run(["git", *args], check=check, quiet=quiet)


def have_git_repo():
    code, _ = git("rev-parse", "--is-inside-work-tree", check=False)
    return code == 0


def count_inputs():
    aw = list((ROOT / "data_in" / "awareness").glob("*.csv"))
    ts = list((ROOT / "data_in" / "turmeric").glob("*.csv"))
    return aw, ts


def codebook_is_stale():
    """
    Compare instrument content hashes with the ones recorded in the codebook.

    An earlier version compared modification times, which silently missed a
    revised instrument: copying a file in Windows Explorer preserves the
    original timestamp, so an updated XLSForm can land looking older than the
    codebook built from its predecessor.
    """
    if not CODEBOOK.exists():
        return True
    try:
        book = json.loads(CODEBOOK.read_text(encoding="utf-8"))
    except Exception:
        return True

    recorded = {
        form.get("source_file"): form.get("source_sha256")
        for form in book.get("forms", {}).values()
    }
    if not any(recorded.values()):
        return True  # built before hashes were recorded

    for p in INSTRUMENTS.glob("*.xlsx"):
        if p.name.startswith("~$"):
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if recorded.get(p.name) != digest:
            return True
    return False


# Anything under these is respondent-level and must never reach a public repo,
# whatever the staging logic does. .gitignore already blocks them; this is the
# second lock on the same door.
NEVER_COMMIT = re.compile(r"^(data_in/|media/|attachments/)|\.(csv|dta|sav)$|\.plain\.js$", re.I)


def carries_microdata(path):
    """
    True for a path that holds respondent-level answers.

    The XLSForms in instruments/ are spreadsheets too, but they are the
    questionnaire definitions and the codebook is built from them, so they
    belong in the repository -- an extension test alone would wrongly block
    every instrument revision.
    """
    if NEVER_COMMIT.search(path):
        return True
    return path.lower().endswith((".xlsx", ".xls")) and not path.startswith("instruments/")

# getElementById('x'). -- dereferenced straight away, with no null guard. The
# optional-chaining form ")?." deliberately does not match.
UNGUARDED_GET = re.compile(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)\s*\.""")


def check_page_boots():
    """
    Fail the build if app.js dereferences an element id that index.html does
    not define.

    This is the failure that shipped an empty dashboard once already: the page
    lost an element, the script that touched it was published one commit
    later, and every getElementById on the missing id threw inside boot() --
    so the shell rendered and not one chart did. It is cheap to check and the
    symptom is invisible until someone opens the live site.
    """
    idx = (ROOT / "index.html").read_text(encoding="utf-8")
    have = set(re.findall(r"""(?:^|\s)id=["']([^"']+)["']""", idx))
    missing = []
    for js in sorted((ROOT / "assets").glob("*.js")):
        src = js.read_text(encoding="utf-8")
        for m in UNGUARDED_GET.finditer(src):
            if m.group(1) not in have:
                line = src.count(chr(10), 0, m.start()) + 1
                missing.append(f"{js.name}:{line}  getElementById('{m.group(1)}') has no matching id in index.html")
    return missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="commit but do not push")
    ap.add_argument("--dry-run", action="store_true", help="rebuild only; no commit, no push")
    ap.add_argument("--password", default=None, help="override the dashboard access password")
    ap.add_argument("--aw-target", type=int, default=1000)
    ap.add_argument("--ts-target", type=int, default=500)
    a = ap.parse_args()

    started = datetime.now()
    say("=" * 68)
    say(f"  TURMERIC QUALITY DASHBOARD — daily update")
    say(f"  {started.strftime('%A %d %B %Y, %I:%M %p')}")
    say("=" * 68)
    say()

    aw_csv, ts_csv = count_inputs()
    say("[1/5] Checking inputs")
    say(f"      awareness CSVs : {len(aw_csv)}")
    say(f"      sampling CSVs  : {len(ts_csv)}")
    if not aw_csv and not ts_csv:
        say()
        say("  [FAIL] No CSVs found in data_in/.")
        say("    Drop the SurveyCTO exports into:")
        say(f"      {ROOT / 'data_in' / 'awareness'}")
        say(f"      {ROOT / 'data_in' / 'turmeric'}")
        return 1

    say()
    say("[2/5] Rebuilding data")
    if codebook_is_stale():
        say("      instrument changed — rebuilding codebook")
        run([sys.executable, str(SCRIPTS / "build_codebook.py")])
    else:
        say("      codebook is current")

    cmd = [sys.executable, str(SCRIPTS / "update_dashboard.py"),
           "--aw-target", str(a.aw_target), "--ts-target", str(a.ts_target)]
    if a.password:
        cmd += ["--password", a.password]
    run(cmd)

    if not PAYLOAD.exists():
        say()
        say("  [FAIL] Build produced no payload — stopping before commit.")
        return 1

    if a.dry_run:
        say()
        say("  --dry-run: rebuilt only, nothing committed.")
        write_log(started)
        return 0

    say()
    say("[3/5] Checking the page will boot")
    problems = check_page_boots()
    if problems:
        say("  [FAIL] index.html and assets/ are out of step — this would deploy")
        say("         a dashboard that renders its shell and no charts:")
        for pr in problems:
            say(f"      {pr}")
        say("      Nothing was committed.")
        write_log(started)
        return 1
    say("      index.html and assets/ agree on every element id")

    say()
    say("[4/5] Committing")
    if not have_git_repo():
        say("      not a git repository — skipping commit and push")
        say(f"      payload is ready at {PAYLOAD.relative_to(ROOT)}")
        write_log(started)
        return 0

    # Stage everything tracked that changed, not a hand-picked list. An
    # earlier version added only the payload, index.html and the codebook,
    # which published a cache-stamped index.html against a stale assets/app.js
    # and left the live dashboard as an empty shell. .gitignore is what keeps
    # the raw exports out; the staging list is not the place to enforce that.
    git("add", "-A", "--", ".", check=False)

    _, staged = git("diff", "--cached", "--name-only", check=False)
    staged_files = [f.strip() for f in staged.splitlines() if f.strip()]
    leaked = [f for f in staged_files if carries_microdata(f)]
    if leaked:
        git("reset", check=False)
        say("  [FAIL] Refusing to commit — these carry respondent-level data:")
        for f in leaked:
            say(f"      {f}")
        say("      Check .gitignore before running again. Nothing was committed.")
        write_log(started)
        return 1
    for f in staged_files:
        say(f"      + {f}")

    code = 1 if staged_files else 0
    if code == 0:
        say("      no change in the rebuilt payload — nothing to commit")
        say()
        say("[5/5] Push")
        say("      skipped (nothing new)")
        write_log(started)
        return 0

    stamp = started.strftime("%Y-%m-%d")
    msg = f"Data update {stamp}"
    rc, out = git("commit", "-m", msg, check=False)
    if rc != 0:
        say("      commit failed:")
        say("      " + out.strip().replace("\n", "\n      "))
        return 1
    say(f"      committed: {msg}")

    say()
    say("[5/5] Pushing to GitHub")
    if a.no_push:
        say("      --no-push set; run 'git push' when ready")
        write_log(started)
        return 0

    rc, out = git("push", check=False, quiet=False)
    if rc != 0:
        say()
        say("  [FAIL] Push failed. The commit is saved locally — fix the remote and run:")
        say("      git push")
        write_log(started)
        return 1

    say("      pushed — GitHub Pages will redeploy in about a minute")
    say()
    say("  [OK] Done. https://turmericstudy.rs.org.pk")
    write_log(started)
    return 0


def write_log(started):
    LOGS.mkdir(exist_ok=True)
    p = LOGS / f"update_{started.strftime('%Y%m%d')}.log"
    with p.open("a", encoding="utf-8") as f:
        f.write("\n".join(LOG_LINES) + "\n\n")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # a failed step should print plainly, not traceback
        say()
        say(f"  [FAIL] {e}")
        try:
            write_log(datetime.now())
        except Exception:
            pass
        sys.exit(1)
