"""Poll employer job boards for new intern/co-op postings and post them to Discord.

config.json holds shared defaults and a list of feeds. Each feed has its own companies,
optional keyword overrides, and the name of the env var holding its Discord webhook.

    python job_alerts.py                  # fetch, post new matches to each feed's webhook, record them
    python job_alerts.py --dry-run        # print new matches; no posting, no database writes
    python job_alerts.py --seed           # record current postings as seen without posting
    python job_alerts.py --feed materials --only OPG --dry-run
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


def open_db(path, legacy_feed, read_only=False):
    db = sqlite3.connect(":memory:")
    if path.exists() or not read_only:
        disk = sqlite3.connect(path)
        if read_only:  # work on an in-memory copy so a dry run never changes the file
            disk.backup(db)
            disk.close()
        else:
            db = disk
    columns = [row[1] for row in db.execute("PRAGMA table_info(seen)")]
    migrate = bool(columns) and "feed" not in columns  # created before feeds existed
    if migrate:
        db.execute("ALTER TABLE seen RENAME TO seen_old")
    db.execute("""CREATE TABLE IF NOT EXISTS seen (
        feed TEXT, company TEXT, job_id TEXT, title TEXT, location TEXT, url TEXT, posted_date TEXT,
        matched INTEGER, first_seen TEXT, PRIMARY KEY (feed, company, job_id))""")
    if migrate:
        db.execute("INSERT INTO seen SELECT ?, * FROM seen_old", (legacy_feed,))
        db.execute("DROP TABLE seen_old")
        db.commit()
    return db


def record(db, feed, source, jobs, matched_ids):
    # Rows are keyed by the config entry (source), not the posting's company: one source such as
    # the Simplify list covers many companies.
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.executemany("INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        (feed, source, j.job_id, j.title, j.location, j.url, j.posted_date, j.job_id in matched_ids, now)
        for j in jobs])
    db.commit()


def word_pattern(words):
    return re.compile(rf"\b(?:{'|'.join(map(re.escape, words))})s?\b", re.IGNORECASE)


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


def run_feed(feed, db, args):
    """Process one feed; returns (companies attempted, companies failed)."""
    name = feed["name"]
    title_re = word_pattern(feed["title_keywords"])
    field_re = word_pattern(feed["field_keywords"]) if feed.get("field_keywords") else None
    location_words = [w.lower() for w in feed["location_keywords"]]
    duration_re = re.compile("|".join(feed["duration_patterns"]), re.IGNORECASE)
    other_duration_re = re.compile("|".join(feed["other_duration_patterns"]), re.IGNORECASE)

    def wanted_term(job):
        # Keep 12/16-month postings and postings that don't state a length; drop ones that only state other lengths.
        text = job.title + "\n" + plain_text(job.description)
        return bool(duration_re.search(text)) or not other_duration_re.search(text)

    webhook = os.environ.get(feed["webhook_env"])
    if not webhook and not (args.dry_run or args.seed):
        print(f"::warning::{name}: {feed['webhook_env']} is not set, skipping this feed")
        return 0, 0

    seen = {(c, j) for c, j in db.execute("SELECT company, job_id FROM seen WHERE feed = ?", (name,))}
    only = {n.lower() for n in args.only}
    companies = [c for c in feed["companies"] if not only or c["name"].lower() in only]

    failures = 0
    for company in companies:
        cname = company["name"]
        # Companies whose whole business is in the feed's field can opt out of the field keyword check,
        # and internship-only sources can opt out of the intern/co-op title check.
        check_field = field_re is not None and company.get("field_filter", True)
        check_title = company.get("title_filter", True)

        def keep(job_id, title):
            return ((cname, job_id) not in seen and (not check_title or bool(title_re.search(title)))
                    and (not check_field or bool(field_re.search(title))))

        try:
            fetch_cfg = {**company, "location_keywords": location_words,
                         "feed_companies": [c["name"] for c in feed["companies"]]}
            jobs = list(FETCHERS[company["ats"]](fetch_cfg, feed["search_terms"], keep))
            located = [j for j in jobs if any(w in j.location.lower() for w in location_words)]
            describe = DESCRIBERS.get(company["ats"])
            located = [j._replace(description=describe(company, j)) if j.description is None and describe else j
                       for j in located]
            matches = [j for j in located if wanted_term(j)]
            print(f"[{name}] {cname}: {len(jobs)} new title matches, {len(located)} in target locations, "
                  f"{len(matches)} after the 12-16 month filter")
            if args.dry_run:
                for j in located:
                    print(f"    {'+' if j in matches else '-'} {j.title} | {j.location} | {j.posted_date} | {j.url}")
                continue
            if not args.seed:
                post_to_discord(webhook, matches)
            record(db, name, cname, jobs, {j.job_id for j in matches})
            seen.update((cname, j.job_id) for j in jobs)
        except Exception as e:
            failures += 1
            print(f"::warning::[{name}] {cname} failed: {e!r}")  # shows as an annotation in GitHub Actions
    return len(companies), failures


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print matches; don't post or write the database")
    parser.add_argument("--seed", action="store_true", help="record current postings as seen without posting")
    parser.add_argument("--feed", action="append", default=[], metavar="FEED", help="limit to these feeds")
    parser.add_argument("--only", action="append", default=[], metavar="COMPANY", help="limit to these companies")
    args = parser.parse_args()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    feeds = [{**config["defaults"], **feed} for feed in config["feeds"]]
    db = open_db(DB_PATH, legacy_feed=feeds[0]["name"], read_only=args.dry_run)

    wanted = {f.lower() for f in args.feed}
    attempted = failed = 0
    for feed in feeds:
        if not wanted or feed["name"].lower() in wanted:
            a, f = run_feed(feed, db, args)
            attempted, failed = attempted + a, failed + f

    if attempted and failed == attempted:
        sys.exit("every company failed")


if __name__ == "__main__":
    main()
