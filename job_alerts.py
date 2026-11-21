"""Poll employer job boards for new intern/co-op postings and post them to Discord.

config.json holds shared defaults and a list of feeds. Each feed has its own companies,
optional keyword overrides, and the name of the secret (or env var) holding its Discord webhook.
See README.md for setup.

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
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from fetchers import DESCRIBERS, FETCHERS, company_key

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
PRESETS_DIR = ROOT / "presets"
# The workflow sets SEEN_DB to a file named after the repo owner, so a copy of this repo starts
# with its own empty history instead of inheriting someone else's seen jobs.
DB_PATH = Path(os.environ.get("SEEN_DB") or ROOT / "seen.db")
MS_SOFTWARE = re.compile(r"\bMS[\s-]+(?:Office|Excel|Word|PowerPoint|Outlook|Teams|Project|Access|Visio|SQL)\b",
                         re.IGNORECASE)
US_LOCATION_NAME = re.compile(
    r"\b(?:united states|u\.s\.a?\.?|usa|alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|district of columbia|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|"
    r"louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|"
    r"nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|"
    r"oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|"
    r"virginia|washington|west virginia|wisconsin|wyoming|chicago)\b",
    re.IGNORECASE,
)
# Exclude CA (ambiguous with Canada) and Canadian province codes; CA is checked separately below.
US_STATE_CODE = re.compile(
    r"(?:,\s*|\s-\s)(?:AL|AK|AZ|AR|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|"
    r"NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b"
)
US_CA_CODE = re.compile(r"(?:,\s*|\s-\s)CA\b")


def is_us_location(location, ca_words):
    if US_LOCATION_NAME.search(location) or US_STATE_CODE.search(location):
        return True
    loc_lower = location.lower()
    if US_CA_CODE.search(location) and "canada" not in loc_lower and not any(w in loc_lower for w in ca_words):
        return True
    return False


def open_db(path, legacy_feed, read_only=False):
    db = sqlite3.connect(":memory:")
    if path.exists() or not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
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


def load_feed(defaults, feed):
    """Defaults, then the feed's preset (presets/<name>.json), then the feed's own settings.

    "companies" in the feed replaces the preset's list; "extra_companies" adds to it.
    """
    preset = {}
    if feed.get("preset"):
        preset = json.loads((PRESETS_DIR / f"{feed['preset']}.json").read_text(encoding="utf-8"))
        preset.pop("description", None)
    merged = {**defaults, **preset, **feed}
    merged["companies"] = feed.get("companies", preset.get("companies", [])) + feed.get("extra_companies", [])
    return merged


def find_secret(name):
    # An env var of that name, or a repo secret of that name: the workflow passes all repo secrets
    # as JSON, so a feed can use any secret name without editing the workflow.
    secrets = json.loads(os.environ.get("REPO_SECRETS") or "{}")
    return os.environ.get(name) or secrets.get(name)


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
    us_field_re = word_pattern(feed["us_field_keywords"]) if feed.get("us_field_keywords") else field_re
    location_words = [w.lower() for w in feed["location_keywords"]]
    duration_re = re.compile("|".join(feed["duration_patterns"]), re.IGNORECASE)
    other_duration_re = re.compile("|".join(feed["other_duration_patterns"]), re.IGNORECASE)
    max_age_days = feed.get("max_age_days")
    today = datetime.now(timezone.utc).date()

    grad_re = word_pattern(feed["graduate_keywords"])
    undergrad_re = word_pattern(feed["undergrad_keywords"])

    def wanted_term(job):
        # Keep 12/16-month postings and postings that don't state a length; drop ones that only state other lengths.
        text = job.title + "\n" + plain_text(job.description)
        return bool(duration_re.search(text)) or not other_duration_re.search(text)

    def recent_enough(job):
        if max_age_days is None or not job.posted_date:
            return True
        try:
            posted = date.fromisoformat(job.posted_date[:10])
        except ValueError:
            return True
        return (today - posted).days <= max_age_days

    def undergrad_ok(job):
        # Drop postings aimed at Master's/PhD students: the title decides if it names a degree,
        # otherwise the description does. "BS/MS" style postings stay.
        if not feed.get("undergrad_only"):
            return True
        for text in (job.title, plain_text(job.description)):
            text = MS_SOFTWARE.sub(" ", text)  # "MS Office" is not a Master's degree
            if grad_re.search(text):
                return bool(undergrad_re.search(text))
        return True

    webhook = find_secret(feed["webhook_env"])
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
        allow_us_all = company.get("allow_us", False)
        allow_us_keys = {company_key(n) for n in company.get("allow_us_companies", [])}
        has_us = allow_us_all or bool(allow_us_keys)

        def keep(job_id, title):
            if (cname, job_id) in seen or (check_title and not title_re.search(title)):
                return False
            if not check_field:
                return True
            return bool(field_re.search(title)) or (has_us and us_field_re is not None and bool(us_field_re.search(title)))

        def job_regions(job):
            in_ca = any(w in job.location.lower() for w in location_words)
            in_us = (allow_us_all or company_key(job.company) in allow_us_keys) and is_us_location(job.location, location_words)
            return in_ca, in_us

        def matches_filters(job):
            in_ca, in_us = job_regions(job)
            ca_ok = in_ca and (not check_field or bool(field_re.search(job.title))) and wanted_term(job)
            us_ok = in_us and (not check_field or us_field_re is None or bool(us_field_re.search(job.title)))
            return (ca_ok or us_ok) and undergrad_ok(job) and recent_enough(job)

        try:
            fetch_cfg = {**company, "location_keywords": location_words, "undergrad_only": feed.get("undergrad_only"),
                         "feed_companies": [c["name"] for c in feed["companies"]]}
            terms = company.get("search_terms", feed["search_terms"])
            jobs = list(FETCHERS[company["ats"]](fetch_cfg, terms, keep))
            located = [j for j in jobs if any(job_regions(j))]
            describe = DESCRIBERS.get(company["ats"])
            located = [j._replace(description=describe(company, j)) if j.description is None and describe else j
                       for j in located]
            matches = [j for j in located if matches_filters(j)]
            print(f"[{name}] {cname}: {len(jobs)} new title matches, {len(located)} in target locations, "
                  f"{len(matches)} after filters")
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
    feeds = [load_feed(config["defaults"], feed) for feed in config["feeds"]]
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
