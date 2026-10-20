"""One fetcher per ATS.

Each fetcher takes a company entry from config.json, the search terms, and a
keep(job_id, title) predicate, and yields a Job for every posting that passes it.
Fetchers only do per-job detail requests for postings that pass keep().
"""
import re
import xml.etree.ElementTree as ET
from collections import namedtuple
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

Job = namedtuple("Job", "company job_id title location url posted_date")

TIMEOUT = 30
MAX_RESULTS = 2000  # per search term; guards against runaway pagination
COUNTRY_NAMES = {"CA": "Canada", "US": "United States"}

session = requests.Session()
# Some boards (Eightfold, Oracle) reject requests without a browser user agent.
session.headers["User-Agent"] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def get_json(url, **kwargs):
    r = session.get(url, timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r.json()


def post_json(url, body):
    r = session.post(url, json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def from_epoch(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat() if ts else None


def join(*parts):
    return "; ".join(dict.fromkeys(p for p in parts if p))  # drop blanks and duplicates


def workday(c, terms, keep):
    base = f"https://{c['host']}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{c['tenant']}/{c['site']}"
    done = set()
    for term in terms:
        offset, total = 0, None
        while total is None or offset < min(total, MAX_RESULTS):
            data = post_json(f"{api}/jobs", {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
            if total is None:
                total = data["total"]  # only reported on the first page
            postings = data["jobPostings"]
            if not postings:
                break
            offset += len(postings)
            for p in postings:
                path, title = p.get("externalPath"), p.get("title")  # a few postings come back without a title
                if not path or not title or path in done:
                    continue
                done.add(path)
                job_id = path.rsplit("/", 1)[-1]
                if not keep(job_id, title):
                    continue
                # The list only says "3 Locations" / "Posted 30+ Days Ago"; the detail has real values.
                info = get_json(api + path)["jobPostingInfo"]
                location = join(info.get("location"), *info.get("additionalLocations", []),
                                (info.get("country") or {}).get("descriptor"), info.get("remoteType"))
                yield Job(c["name"], job_id, title, location,
                          info.get("externalUrl") or f"{base}/{c['site']}{path}", info.get("startDate"))


def greenhouse(c, terms, keep):
    for j in get_json(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs")["jobs"]:
        job_id = str(j["id"])
        if keep(job_id, j["title"]):
            posted = j.get("first_published") or j.get("updated_at")
            yield Job(c["name"], job_id, j["title"], (j.get("location") or {}).get("name", ""),
                      j["absolute_url"], posted[:10] if posted else None)


def lever(c, terms, keep):
    for j in get_json(f"https://api.lever.co/v0/postings/{c['slug']}", params={"mode": "json"}):
        if keep(j["id"], j["text"]):
            cat = j.get("categories", {})
            location = join(*(cat.get("allLocations") or [cat.get("location")]),
                            COUNTRY_NAMES.get(j.get("country")),
                            "Remote" if j.get("workplaceType") == "remote" else None)
            created = j.get("createdAt")
            yield Job(c["name"], j["id"], j["text"], location, j["hostedUrl"],
                      from_epoch(created // 1000) if created else None)


def eightfold(c, terms, keep):
    host, v2 = c["host"], c.get("api") == "v2"  # ST still uses the older v2 API
    done = set()
    for term in terms:
        start, total = 0, None
        while total is None or start < min(total, MAX_RESULTS):
            if v2:
                data = get_json(f"https://{host}/api/apply/v2/jobs",
                                params={"domain": c["domain"], "query": term, "start": start, "num": 10})
            else:
                data = get_json(f"https://{host}/api/pcsx/search",
                                params={"domain": c["domain"], "query": term, "start": start})["data"]
            total, positions = data["count"], data["positions"]
            if not positions:
                break
            start += len(positions)
            for p in positions:
                job_id = str(p["id"])
                if job_id in done:
                    continue
                done.add(job_id)
                if not keep(job_id, p["name"]):
                    continue
                if v2:
                    locations = p.get("locations") or [p.get("location")]
                    url, ts, mode = p["canonicalPositionUrl"], p.get("t_create"), p.get("work_location_option")
                else:
                    locations = p.get("locations", []) + p.get("standardizedLocations", [])
                    url, ts, mode = f"https://{host}{p['positionUrl']}", p.get("postedTs"), p.get("workLocationOption")
                remote = "Remote" if (mode or "").startswith("remote") else None
                yield Job(c["name"], job_id, p["name"], join(*locations, remote), url, from_epoch(ts))


def oracle(c, terms, keep):
    host, site = c["host"], c["site"]
    done = set()
    for term in terms:
        offset, total = 0, None
        while total is None or offset < min(total, MAX_RESULTS):
            # The finder's ; , = separators must stay unencoded, so build the URL by hand.
            finder = (f"findReqs;siteNumber={site},keyword={quote(term)},limit=25,"
                      f"offset={offset},sortBy=POSTING_DATES_DESC")
            url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                   f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}")
            data = get_json(url)["items"][0]
            total, reqs = data["TotalJobsCount"], data["requisitionList"]
            if not reqs:
                break
            offset += len(reqs)
            for r in reqs:
                job_id = str(r["Id"])
                if job_id in done:
                    continue
                done.add(job_id)
                if not keep(job_id, r["Title"]):
                    continue
                remote = r.get("WorkplaceType") if "remote" in (r.get("WorkplaceType") or "").lower() else None
                location = join(r.get("PrimaryLocation"), COUNTRY_NAMES.get(r.get("PrimaryLocationCountry")),
                                *(s.get("Name") for s in r.get("secondaryLocations", [])), remote)
                yield Job(c["name"], job_id, r["Title"], location,
                          f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}", r.get("PostedDate"))


def ukg(c, terms, keep):
    # UKG boards are small, so fetch everything and let keep() filter.
    done = set()
    for board in c["boards"]:
        base = f"https://{c['host']}/{c['tenant']}/JobBoard/{board}"
        skip, total = 0, None
        while total is None or skip < min(total, MAX_RESULTS):
            data = post_json(f"{base}/JobBoardView/LoadSearchResults", {"opportunitySearch": {
                "Top": 50, "Skip": skip, "QueryString": "", "Filters": [],
                "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}]}})
            total, opps = data["totalCount"], data["opportunities"]
            if not opps:
                break
            skip += len(opps)
            for o in opps:
                job_id = o["Id"]
                if job_id in done:
                    continue
                done.add(job_id)
                if not keep(job_id, o["Title"]):
                    continue
                parts = []
                for loc in o.get("Locations", []):
                    addr = loc.get("Address") or {}
                    parts += [loc.get("LocalizedDescription"), addr.get("City"),
                              (addr.get("State") or {}).get("Name"), (addr.get("Country") or {}).get("Name")]
                posted = o.get("PostedDate")
                yield Job(c["name"], job_id, o["Title"], join(*parts),
                          f"{base}/OpportunityDetail?opportunityId={job_id}", posted[:10] if posted else None)


def bamboohr(c, terms, keep):
    for j in get_json(f"https://{c['slug']}.bamboohr.com/careers/list")["result"]:
        job_id = str(j["id"])
        if keep(job_id, j["jobOpeningName"]):
            loc, ats = j.get("location") or {}, j.get("atsLocation") or {}
            location = join(loc.get("city"), loc.get("state"), ats.get("city"),
                            ats.get("province") or ats.get("state"), ats.get("country"),
                            "Remote" if j.get("isRemote") else None)
            yield Job(c["name"], job_id, j["jobOpeningName"], location,
                      f"https://{c['slug']}.bamboohr.com/careers/{job_id}", None)


def successfactors(c, terms, keep):
    # SuccessFactors career sites have no public JSON API, but they do publish an RSS search feed.
    done = set()
    for term in terms:
        r = session.get(f"https://{c['host']}/services/rss/job/",
                        params={"locale": "en_US", "keywords": term}, timeout=TIMEOUT)
        r.raise_for_status()
        for item in ET.fromstring(r.content).iter("item"):
            link = item.findtext("link", "").split("?")[0]
            m = re.search(r"/(\d+)/?$", link)
            job_id = m.group(1) if m else link
            if job_id in done:
                continue
            done.add(job_id)
            # Titles look like "RF Design Co-Op (Jan-June 2027) (Andover, MA, US, 01810)".
            raw = item.findtext("title", "")
            title, sep, location = raw.rpartition(" (")
            title, location = (title, location.rstrip(")")) if sep else (raw, "")
            if not keep(job_id, title):
                continue
            pub = item.findtext("pubDate")
            yield Job(c["name"], job_id, title, location, link,
                      parsedate_to_datetime(pub).date().isoformat() if pub else None)


def jibe(c, terms, keep):
    base = f"https://{c['host']}"
    done = set()
    for term in terms:
        page, fetched, total = 1, 0, None
        while total is None or fetched < min(total, MAX_RESULTS):
            data = get_json(f"{base}/api/jobs", params={"keywords": term, "limit": 100, "page": page})
            total, jobs = data["totalCount"], data["jobs"]
            if not jobs:
                break
            page, fetched = page + 1, fetched + len(jobs)
            for j in jobs:
                d = j["data"]
                job_id = str(d["req_id"])
                if job_id in done:
                    continue
                done.add(job_id)
                if not keep(job_id, d["title"]):
                    continue
                extra = [f"{a.get('city')}, {a.get('country')}" for a in d.get("additional_locations") or []]
                posted = d.get("posted_date")
                yield Job(c["name"], job_id, d["title"], join(d.get("full_location"), d.get("country"), *extra),
                          f"{base}/careers-home/jobs/{d['slug']}", posted[:10] if posted else None)


FETCHERS = {
    "workday": workday,
    "greenhouse": greenhouse,
    "lever": lever,
    "eightfold": eightfold,
    "oracle": oracle,
    "ukg": ukg,
    "bamboohr": bamboohr,
    "successfactors": successfactors,
    "jibe": jibe,
}
