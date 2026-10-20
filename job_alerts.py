"""Poll employer job boards for new intern/co-op postings and post them to Discord.

    python job_alerts.py              # fetch, post new matches to $DISCORD_WEBHOOK_URL, record them
    python job_alerts.py --dry-run    # print new matches; no posting, no database writes
    python job_alerts.py --seed       # record current postings as seen without posting
    python job_alerts.py --only AMD --only Intel --dry-run
"""
import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from fetchers import DESCRIBERS, FETCHERS

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
        (j.company, j.job_id, j.title, j.location, j.url, j.posted_date, j.job_id in matched_ids, now)
        for j in jobs])
    db.commit()


def plain_text(text):
    # Descriptions arrive as HTML, sometimes escaped twice (Greenhouse).
    return re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(text or "")))


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
    duration_re = re.compile("|".join(config["duration_patterns"]), re.IGNORECASE)
    other_duration_re = re.compile("|".join(config["other_duration_patterns"]), re.IGNORECASE)

    def wanted_term(job):
        # Keep 12/16-month postings and postings that don't state a length; drop ones that only state other lengths.
        text = job.title + "\n" + plain_text(job.description)
        return bool(duration_re.search(text)) or not other_duration_re.search(text)

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
            located = [j for j in jobs if any(w in j.location.lower() for w in location_words)]
            describe = DESCRIBERS.get(company["ats"])
            located = [j._replace(description=describe(company, j)) if j.description is None and describe else j
                       for j in located]
            matches = [j for j in located if wanted_term(j)]
            print(f"{name}: {len(jobs)} new title matches, {len(located)} in target locations, "
                  f"{len(matches)} after the 12-16 month filter")
            if args.dry_run:
                for j in located:
                    print(f"    {'+' if j in matches else '-'} {j.title} | {j.location} | {j.posted_date} | {j.url}")
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
