"""
daily_update.py
===============
The one command to run each day once new SurveyCTO exports have been dropped
into data_in/.

It does four things, and stops at the first one that fails:

  1. rebuilds codebook.json if either instrument has been edited
  2. rebuilds data/dashboard_data.js (encrypted) from the CSVs in data_in/
  3. commits the rebuilt payload
  4. pushes to GitHub, which redeploys turmericquality.rs.org.pk

Raw CSVs are never committed — they hold vendor names, GPS fixes and
respondent-level answers, and .gitignore keeps them out of the repository.
Only the encrypted payload is published.

Usage:
    python scripts/daily_update.py                 # build, commit, push
    python scripts/daily_update.py --no-push       # build and commit only
    python scripts/daily_update.py --dry-run       # build only, touch nothing
    python scripts/daily_update.py --demo          # tag payload as demo data
"""

import argparse
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
    if not CODEBOOK.exists():
        return True
    newest = max((p.stat().st_mtime for p in INSTRUMENTS.glob("*.xlsx")), default=0)
    return newest > CODEBOOK.stat().st_mtime


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="commit but do not push")
    ap.add_argument("--dry-run", action="store_true", help="rebuild only; no commit, no push")
    ap.add_argument("--demo", action="store_true", help="tag the payload as demonstration data")
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
    say("[1/4] Checking inputs")
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
    say("[2/4] Rebuilding data")
    if codebook_is_stale():
        say("      instrument changed — rebuilding codebook")
        run([sys.executable, str(SCRIPTS / "build_codebook.py")])
    else:
        say("      codebook is current")

    cmd = [sys.executable, str(SCRIPTS / "update_dashboard.py"),
           "--aw-target", str(a.aw_target), "--ts-target", str(a.ts_target)]
    if a.demo:
        cmd.append("--demo")
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
    say("[3/4] Committing")
    if not have_git_repo():
        say("      not a git repository — skipping commit and push")
        say(f"      payload is ready at {PAYLOAD.relative_to(ROOT)}")
        write_log(started)
        return 0

    git("add", "--", str(PAYLOAD.relative_to(ROOT)).replace("\\", "/"),
        "codebook/codebook.json", check=False)
    code, _ = git("diff", "--cached", "--quiet", check=False)
    if code == 0:
        say("      no change in the rebuilt payload — nothing to commit")
        say()
        say("[4/4] Push")
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
    say("[4/4] Pushing to GitHub")
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
    say("  [OK] Done. https://turmericquality.rs.org.pk")
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
