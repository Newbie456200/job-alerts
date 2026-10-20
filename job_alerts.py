"""Poll employer job boards for new intern/co-op postings and post them to Discord.

    python job_alerts.py              # fetch, post new matches to $DISCORD_WEBHOOK_URL, record them
    python job_alerts.py --dry-run    # print new matches; no posting, no database writes
    python job_alerts.py --seed       # record current postings as seen without posting
    python job_alerts.py --only AMD --only Intel --dry-run
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from fetchers import FETCHERS

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
DB_PATH = ROOT / "seen.db"


def open_db(path):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS seen (
        company TEXT, job_id TEXT, title TEXT, location TEXT, url TEXT, posted_date TEXT,
        matched INTEGER, first_seen TEXT, PRIMARY KEY (company, job_id))""")
    return db


def record(db, jobs, matched_ids):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.executemany("INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        (*j, j.job_id in matched_ids, now) for j in jobs])
    db.commit()


def post_to_discord(webhook, jobs):
    for i in range(0, len(jobs), 10):  # Discord allows 10 embeds per message
        embeds = []
        for j in jobs[i:i + 10]:
            embed = {"title": j.title[:256], "url": j.url, "description": f"**{j.company}**\n{j.location}"[:4096]}
            if j.posted_date:
                embed["footer"] = {"text": f"Posted {j.posted_date}"}
            embeds.append(embed)
        while True:
            r = requests.post(webhook, json={"embeds": embeds}, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 1)))
                continue
            r.raise_for_status()
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print matches; don't post or write the database")
    parser.add_argument("--seed", action="store_true", help="record current postings as seen without posting")
    parser.add_argument("--only", action="append", default=[], metavar="COMPANY", help="limit to these companies")
    args = parser.parse_args()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    words = "|".join(map(re.escape, config["title_keywords"]))
    title_re = re.compile(rf"\b(?:{words})s?\b", re.IGNORECASE)
    location_words = [w.lower() for w in config["location_keywords"]]

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook and not (args.dry_run or args.seed):
        sys.exit("DISCORD_WEBHOOK_URL is not set (use --dry-run to test without it)")

    db = open_db(DB_PATH)
    seen = set(db.execute("SELECT company, job_id FROM seen"))
    only = {name.lower() for name in args.only}
    companies = [c for c in config["companies"] if not only or c["name"].lower() in only]

    failures = 0
    for company in companies:
        name = company["name"]

        def keep(job_id, title):
            return (name, job_id) not in seen and bool(title_re.search(title))

        try:
            jobs = list(FETCHERS[company["ats"]](company, config["search_terms"], keep))
            matches = [j for j in jobs if any(w in j.location.lower() for w in location_words)]
            print(f"{name}: {len(jobs)} new title matches, {len(matches)} in Canada/remote")
            if args.dry_run:
                for j in matches:
                    print(f"    {j.title} | {j.location} | {j.posted_date} | {j.url}")
                continue
            if not args.seed:
                post_to_discord(webhook, matches)
            record(db, jobs, {j.job_id for j in matches})
            seen.update((name, j.job_id) for j in jobs)
        except Exception as e:
            failures += 1
            print(f"::warning::{name} failed: {e!r}")  # shows as an annotation in GitHub Actions

    if companies and failures == len(companies):
        sys.exit("every company failed")


if __name__ == "__main__":
    main()
