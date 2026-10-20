"""One fetcher per ATS.

Each fetcher takes a company entry from config.json, the search terms, and a
keep(job_id, title) predicate, and yields a Job for every posting that passes it.
Fetchers only do per-job detail requests for postings that pass keep().

Job.description is filled in when the ATS returns it cheaply. For boards where it
costs an extra request per job, it is left as None and DESCRIBERS fetches it later,
only for jobs that have already passed the title and location filters.
"""
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import namedtuple
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

Job = namedtuple("Job", "company job_id title location url posted_date description", defaults=(None,))

TIMEOUT = 30
MAX_RESULTS = 2000  # per search term; guards against runaway pagination
COUNTRY_NAMES = {"CA": "Canada", "US": "United States"}

session = requests.Session()
# Some boards (Eightfold, Oracle) reject requests without a browser user agent.
session.headers["User-Agent"] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def request(method, url, **kwargs):
    for attempt in range(4):
        r = session.request(method, url, timeout=TIMEOUT, **kwargs)
        if r.status_code != 429 or attempt == 3:
            break
        time.sleep(float(r.headers.get("Retry-After") or 2 ** (attempt + 1)))  # rate limited: back off
    r.raise_for_status()
    return r


def get_json(url, **kwargs):
    return request("GET", url, **kwargs).json()


def post_json(url, body):
    return request("POST", url, json=body).json()


def from_epoch(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat() if ts else None


def join(*parts, sep="; "):
    return sep.join(dict.fromkeys(p for p in parts if p))  # drop blanks and duplicates


WORKDAY_CANADA = "a30a87ed25634629aa6c3958aa2b91ea"  # Workday's country ID for Canada, the same in every tenant


def workday_location_facets(facets, location_words):
    """Facets that narrow a Workday search to Canada or the target cities.

    Returns {} when the board has no location facets to narrow by, and None when its
    location facets show no jobs in the target area (so the search can be skipped).
    """
    location_groups = []
    for f in facets:
        for g in [v for v in f.get("values", []) if "facetParameter" in v] or [f]:  # some facets are nested
            values = g.get("values", [])
            if any(v.get("id") == WORKDAY_CANADA for v in values):
                return {g["facetParameter"]: [WORKDAY_CANADA]}
            if "location" in g.get("facetParameter", "").lower():
                location_groups.append(g)
    words = [*location_words, "canada"]
    for g in location_groups:
        ids = [v["id"] for v in g["values"] if any(w in v.get("descriptor", "").lower() for w in words)]
        if ids:
            return {g["facetParameter"]: ids}
    return None if location_groups else {}


def workday(c, terms, keep):
    base = f"https://{c['host']}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{c['tenant']}/{c['site']}"
    done = set()
    for term in terms:
        offset, total, facets = 0, None, None
        while total is None or offset < min(total, MAX_RESULTS):
            data = post_json(f"{api}/jobs", {"appliedFacets": facets or {}, "limit": 20, "offset": offset, "searchText": term})
            if facets is None:
                # Big boards return thousands of matches worldwide; narrow to Canada when the board allows it.
                facets = workday_location_facets(data.get("facets", []), c.get("location_keywords", []))
                if facets is None:
                    break  # nothing in the target area for this term
                if facets:
                    continue  # rerun the first page with the location facets applied
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
                yield Job(c["name"], job_id, title, location, info.get("externalUrl") or f"{base}/{c['site']}{path}",
                          info.get("startDate"), info.get("jobDescription", ""))


def greenhouse(c, terms, keep):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs", params={"content": "true"})
    for j in data["jobs"]:
        job_id = str(j["id"])
        if keep(job_id, j["title"]):
            posted = j.get("first_published") or j.get("updated_at")
            yield Job(c["name"], job_id, j["title"], (j.get("location") or {}).get("name", ""),
                      j["absolute_url"], posted[:10] if posted else None, j.get("content", ""))


def lever(c, terms, keep):
    for j in get_json(f"https://api.lever.co/v0/postings/{c['slug']}", params={"mode": "json"}):
        if keep(j["id"], j["text"]):
            cat = j.get("categories", {})
            location = join(*(cat.get("allLocations") or [cat.get("location")]),
                            COUNTRY_NAMES.get(j.get("country")),
                            "Remote" if j.get("workplaceType") == "remote" else None)
            description = join(j.get("descriptionPlain"), *(l.get("content") for l in j.get("lists", [])),
                               j.get("additionalPlain"), sep="\n")
            created = j.get("createdAt")
            yield Job(c["name"], j["id"], j["text"], location, j["hostedUrl"],
                      from_epoch(created // 1000) if created else None, description)


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
                    description = p.get("job_description", "")
                else:  # PCSX search results have no description; eightfold_description fetches it
                    locations = p.get("locations", []) + p.get("standardizedLocations", [])
                    url, ts, mode = f"https://{host}{p['positionUrl']}", p.get("postedTs"), p.get("workLocationOption")
                    description = None
                remote = "Remote" if (mode or "").startswith("remote") else None
                yield Job(c["name"], job_id, p["name"], join(*locations, remote), url, from_epoch(ts), description)


def eightfold_description(c, job):
    data = get_json(f"https://{c['host']}/api/pcsx/position_details",
                    params={"position_id": job.job_id, "domain": c["domain"], "hl": "en"})
    return data["data"].get("jobDescription", "")


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
                # The list often says only "Canada"; the detail has the work site's city and the description.
                detail = get_json(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                                  f'?expand=all&onlyData=true&finder=ById;Id="{job_id}",siteNumber={site}')["items"]
                sites = [w for d in detail for w in d.get("workLocation", []) + d.get("otherWorkLocations", [])]
                location = join(r.get("PrimaryLocation"), COUNTRY_NAMES.get(r.get("PrimaryLocationCountry")),
                                *(s.get("Name") for s in r.get("secondaryLocations", [])),
                                *(f"{w.get('TownOrCity')}, {w.get('Region2')}" for w in sites), remote)
                description = join(*(d.get(k) for d in detail for k in (
                    "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")), sep="\n")
                yield Job(c["name"], job_id, r["Title"], location,
                          f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}",
                          r.get("PostedDate"), description)


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
                # BriefDescription is only the opening paragraph; ukg_description fetches the full text.
                yield Job(c["name"], job_id, o["Title"], join(*parts),
                          f"{base}/OpportunityDetail?opportunityId={job_id}", posted[:10] if posted else None)


def ukg_description(c, job):
    html = request("GET", job.url).text  # the detail page embeds the posting as JSON
    m = re.search(r'"Description":("(?:[^"\\]|\\.)*")', html)
    return json.loads(m.group(1)) if m else ""


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


def bamboohr_description(c, job):
    data = get_json(f"https://{c['slug']}.bamboohr.com/careers/{job.job_id}/detail")
    return data["result"]["jobOpening"].get("description", "")


def successfactors(c, terms, keep):
    # SuccessFactors career sites have no public JSON API, but they do publish an RSS search feed.
    done = set()
    for term in terms:
        r = request("GET", f"https://{c['host']}/services/rss/job/", params={"locale": "en_US", "keywords": term})
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
                      parsedate_to_datetime(pub).date().isoformat() if pub else None,
                      item.findtext("description", ""))


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
                description = join(d.get("description"), d.get("responsibilities"), d.get("qualifications"), sep="\n")
                yield Job(c["name"], job_id, d["title"], join(d.get("full_location"), d.get("country"), *extra),
                          f"{base}/careers-home/jobs/{d['slug']}", posted[:10] if posted else None, description)


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

DESCRIBERS = {
    "eightfold": eightfold_description,
    "ukg": ukg_description,
    "bamboohr": bamboohr_description,
}
