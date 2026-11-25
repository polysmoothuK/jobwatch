"""JobWatch - polls studio career feeds and job boards, keeps only new matching roles,
scores them and sends alerts. Runs on GitHub Actions (or locally: python watcher.py).

Environment variables (all optional):
  NTFY_TOPIC     ntfy.sh topic for phone push alerts (keep it secret)
  GITHUB_TOKEN   + GITHUB_REPOSITORY + JOBWATCH_ISSUES=1 -> also post a daily GitHub issue (off by default: the repo is public)
  JOBWATCH_DRY   "1" = do not send notifications (testing)
"""
from __future__ import annotations

import concurrent.futures as cf
import csv
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SOURCES = ROOT / "sources"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

UA = {"User-Agent": "Mozilla/5.0 (JobWatch personal job alerts; +https://github.com)",
      "Accept": "application/json, text/html, */*"}
TIMEOUT = 25
NOW = dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- helpers
def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
    if r.status_code == 429:                      # rate limited: wait once and retry
        time.sleep(int(r.headers.get("Retry-After", "8") or 8) if str(r.headers.get("Retry-After", "")).isdigit() else 8)
        r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
    if (r.encoding or "").lower() in ("iso-8859-1", "latin-1", ""):
        r.encoding = "utf-8"
    return r


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def norm_date(v):
    """Turn the many date formats of job boards into YYYY-MM-DD ('' if unknown)."""
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v / (1000 if v > 1e11 else 1), dt.timezone.utc).strftime("%Y-%m-%d")
    s = str(v).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    low = s.lower()
    if "today" in low or "just" in low or "aujourd" in low:
        return NOW.strftime("%Y-%m-%d")
    if "yesterday" in low:
        return (NOW - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\+?\s*(day|week|month|hour)", low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"hour": 0, "day": n, "week": 7 * n, "month": 30 * n}[unit]
        return (NOW - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%B %d, %Y", "%d %b %Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def job(src, title, url, location="", posted="", remote=None, extra=""):
    title = strip_html(title)
    posted = norm_date(posted)
    uid = hashlib.sha1(f"{src['name']}|{title}|{url}".encode()).hexdigest()[:16]
    return {"uid": uid, "company": src["name"], "track": src.get("track", "vfx"),
            "title": title, "location": strip_html(location), "url": url,
            "posted": posted or "", "remote": remote, "source": src.get("ats", ""),
            "country_hint": src.get("country", ""), "extra": extra[:300]}


# --------------------------------------------------------------------------- ATS fetchers
def f_greenhouse(s):
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{s['id']}/jobs").json()
    return [job(s, j["title"], j["absolute_url"], (j.get("location") or {}).get("name", ""),
                j.get("updated_at", "")) for j in r.get("jobs", [])]


def f_lever(s, host="api.lever.co"):
    r = get(f"https://{host}/v0/postings/{s['id']}?mode=json").json()
    out = []
    for j in r:
        cat = j.get("categories", {})
        posted = dt.datetime.fromtimestamp(j.get("createdAt", 0) / 1000, dt.timezone.utc).isoformat() if j.get("createdAt") else ""
        out.append(job(s, j["text"], j["hostedUrl"], cat.get("location", ""), posted,
                       remote=(j.get("workplaceType") == "remote")))
    return out


def f_lever_eu(s):
    return f_lever(s, "api.eu.lever.co")


def f_smartrecruiters(s):
    out, offset = [], 0
    while offset < 500:
        r = get(f"https://api.smartrecruiters.com/v1/companies/{s['id']}/postings",
                params={"limit": 100, "offset": offset}).json()
        for j in r.get("content", []):
            loc = j.get("location", {})
            comp = (j.get("company") or {}).get("name") or s["name"]
            out.append(job({**s, "name": comp}, j["name"], f"https://jobs.smartrecruiters.com/{s['id']}/{j['id']}",
                           ", ".join(x for x in [loc.get("city"), loc.get("country")] if x),
                           j.get("releasedDate", ""), remote=loc.get("remote")))
        if len(r.get("content", [])) < 100:
            break
        offset += 100
    return out


def f_recruitee(s):
    r = get(f"https://{s['id']}.recruitee.com/api/offers/").json()
    return [job(s, j["title"], j.get("careers_url") or j.get("url", ""), j.get("location", ""),
                j.get("published_at", ""), remote=j.get("remote")) for j in r.get("offers", [])]


def f_workable(s):
    try:
        r = get(f"https://apply.workable.com/api/v1/widget/accounts/{s['id']}").json()
        return [job(s, j["title"], j.get("url") or j.get("shortlink", ""),
                    ", ".join(x for x in [j.get("city"), j.get("country")] if x),
                    j.get("published_on", ""), remote=j.get("telecommuting")) for j in r.get("jobs", [])]
    except ValueError:
        r = requests.post(f"https://apply.workable.com/api/v3/accounts/{s['id']}/jobs",
                          json={"query": "", "location": [], "department": [], "worktype": [], "remote": []},
                          headers={**UA, "Content-Type": "application/json"}, timeout=TIMEOUT).json()
        out = []
        for j in r.get("results", []):
            loc = j.get("location") or {}
            out.append(job(s, j.get("title", ""), f"https://apply.workable.com/{s['id']}/j/{j.get('shortcode', '')}/",
                           ", ".join(x for x in [loc.get("city"), loc.get("country")] if x),
                           j.get("published", ""), remote=j.get("remote")))
        return out


def f_bamboohr(s):
    r = get(f"https://{s['id']}.bamboohr.com/careers/list").json()
    out = []
    for j in r.get("result", []):
        loc = j.get("location") or {}
        out.append(job(s, j.get("jobOpeningName", ""), f"https://{s['id']}.bamboohr.com/careers/{j.get('id')}",
                       ", ".join(x for x in [loc.get("city"), loc.get("state")] if x)))
    return out


def _rss(s, url):
    root = ET.fromstring(get(url).content)
    out = []
    for it in root.iter("item"):
        out.append(job(s, it.findtext("title", ""), it.findtext("link", ""),
                       it.findtext("{*}location", "") or "", it.findtext("pubDate", "")))
    return out


def f_teamtailor(s):
    return _rss(s, f"https://{s['id']}/jobs.rss")


def f_personio(s):
    root = ET.fromstring(get(f"https://{s['id']}.jobs.personio.de/xml").content)
    return [job(s, p.findtext("name", ""), f"https://{s['id']}.jobs.personio.de/job/{p.findtext('id', '')}",
                p.findtext("office", "")) for p in root.iter("position")]


def f_ashby(s):
    r = get(f"https://api.ashbyhq.com/posting-api/job-board/{s['id']}").json()
    return [job(s, j["title"], j.get("jobUrl", ""), j.get("location", ""), j.get("publishedAt", ""),
                remote=j.get("isRemote")) for j in r.get("jobs", [])]


def f_workday(s):
    tenant, wd, site = s["id"].split("|")
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    out = []
    terms = CONFIG.get("workday_search_terms_it", []) if s.get("track") == "it" else CONFIG["workday_search_terms"]
    for term in list(dict.fromkeys(terms + s.get("keywords", []))):
        try:
            r = requests.post(url, json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term},
                              headers={**UA, "Content-Type": "application/json"}, timeout=TIMEOUT).json()
        except Exception:
            continue
        for j in r.get("jobPostings", []):
            out.append(job(s, j.get("title", ""), f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{j.get('externalPath', '')}",
                           j.get("locationsText", ""), j.get("postedOn", "")))
    return out


def f_phenom(s):
    """Phenom career sites (e.g. Disney Careers, which hosts all ILM / Lucasfilm jobs). id = host|lang|country."""
    host, lang, country = (s["id"].split("|") + ["en_global", "global"])[:3]
    terms = list(dict.fromkeys(CONFIG["workday_search_terms"] + s.get("keywords", [])))
    out, seen = [], set()
    for term in terms:
        body = {"lang": lang, "deviceType": "desktop", "country": country, "pageName": "search-results",
                "ddoKey": "refineSearch", "sortBy": "", "subsearch": "", "from": 0, "jobs": True, "counts": False,
                "all_fields": [], "size": 50, "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
                "pageId": "page20", "siteType": "external", "keywords": term, "global": True,
                "selected_fields": {}, "locationData": {}}
        try:
            r = requests.post(f"https://{host}/widgets", json=body,
                              headers={**UA, "Content-Type": "application/json"}, timeout=TIMEOUT).json()
        except Exception:
            continue
        for j in r.get("refineSearch", {}).get("data", {}).get("jobs", []):
            jid = j.get("jobId") or j.get("reqId")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
            out.append(job(s, j.get("title", ""), f"https://{host}/{country}/en/job/{jid}", loc,
                           j.get("postedDate", ""), extra=j.get("descriptionTeaser", "") or ""))
    return out


def f_jobvite(s):
    page = get(f"https://jobs.jobvite.com/{s['id']}/jobs").text
    out = []
    for m in re.finditer(r'href="(/[^"]*/job/[^"]+)"[^>]*>(.*?)</a>', page, re.S):
        out.append(job(s, m.group(2), "https://jobs.jobvite.com" + m.group(1)))
    return out


def f_html(s):
    """No ATS: detect any change in the careers page text, plus links that look like roles."""
    url = s.get("careers_url")
    if not url:
        return []
    page = get(url).text
    text = strip_html(re.sub(r"(?is)<(script|style).*?</\1>", " ", page))
    # Only the job-like words of the page count: dynamic pages (dates, tokens, ads) no longer look "changed".
    track = s.get("track", "vfx")
    words = sorted(set(w for w in re.findall(r"[^.!?\n|]{3,90}", text) if matches(w.strip(), track)[0]))
    out = []
    if words:
        out.append({"uid": "page:" + hashlib.sha1(url.encode()).hexdigest()[:12], "company": s["name"],
                    "track": track, "title": "__PAGE__", "location": "", "url": url,
                    "posted": "", "remote": None, "source": "html", "country_hint": s.get("country", ""),
                    "extra": hashlib.sha1("|".join(words).encode()).hexdigest()})
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
        t = strip_html(m.group(2))
        if 4 < len(t) < 90 and matches(t, track)[0]:
            out.append(job(s, t, requests.compat.urljoin(url, m.group(1))))
    return out


FETCHERS = {k[2:]: v for k, v in globals().items() if k.startswith("f_")}


# --------------------------------------------------------------------------- aggregators
def agg_google_sheet():
    url = CONFIG["google_sheet_csv"]
    rows = list(csv.reader(io.StringIO(get(url).text)))
    out, src = [], {"name": "Job Sheet", "ats": "sheet", "track": "vfx"}
    for r in rows[1:]:
        if len(r) < 24:
            continue
        try:
            age = int(r[23] or 999)
        except ValueError:
            age = 999
        if age > CONFIG["sheet_max_age_days"]:
            continue
        j = job({**src, "name": r[0] or "?"}, r[8], r[16] if r[16].startswith("http") else "",
                ", ".join(x for x in [r[2], r[6]] if x), r[14], remote=(r[12].lower() == "remote"),
                extra=f"{r[10]} | {r[20]}")
        j["source"] = "sheet"
        out.append(j)
    return out


def agg_vfxwire():
    out, src = [], {"name": "VFXwire", "ats": "vfxwire", "track": "vfx"}
    for path in CONFIG["vfxwire_pages"]:
        page = get("https://vfxwire.com" + path).text
        for m in re.finditer(r'<a[^>]+href="(/jobs/(?!role/|in/|by/|studio)[^"#?]+)"[^>]*>(.*?)</a>', page, re.S):
            block = strip_html(m.group(2))
            if not (4 < len(block) < 260):
                continue
            # card text usually reads "Title Location · ... AGE" ; keep the title part
            title = re.split(r"\s{2,}| London| Remote| Vancouver| Montr| Paris| Los Angeles|,", block)[0].strip()
            when = ""
            d = re.search(r"(\d+)\s+DAYS?\s+AGO|\bNEW\b|(\d{1,2})\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b", block)
            if d and d.group(1):
                when = f"{d.group(1)} days ago"
            elif d and d.group(0) == "NEW":
                when = "today"
            elif d:
                cand = dt.datetime.strptime(f"{d.group(2)} {d.group(3).title()} {NOW.year}", "%d %b %Y")
                if cand.date() > NOW.date():
                    cand = cand.replace(year=NOW.year - 1)
                when = cand.strftime("%Y-%m-%d")
            j = job(src, title or block[:80], "https://vfxwire.com" + m.group(1), posted=when, extra=block)
            j["location"] = block[len(title):][:80].strip()
            out.append(j)
    return out


def agg_rss_feeds():
    out = []
    for name, url in CONFIG["rss_feeds"].items():
        try:
            root = ET.fromstring(get(url).content)
        except Exception as e:
            print("rss failed", name, e)
            continue
        src = {"name": name, "ats": "rss", "track": "vfx"}
        for it in root.iter("item"):
            title = it.findtext("title", "")
            comp = it.findtext("{*}company", "") or it.findtext("company", "")
            loc = it.findtext("{*}location", "") or it.findtext("location", "")
            m = re.match(r"\s*(.+?) is hiring (?:an? )?(.+?)(?: to | in | for |$)", strip_html(title))
            if m and not comp:                     # "Studio X is hiring a Cinematic Designer ..."
                comp, title = m.group(1), m.group(2)
            j = job({**src, "name": comp or name}, title, it.findtext("link", ""), loc, it.findtext("pubDate", ""))
            j["source"] = name
            out.append(j)
    return out


def agg_artstation():
    out, src = [], {"name": "ArtStation", "ats": "artstation", "track": "vfx"}
    for page in (1, 2):
        r = get("https://www.artstation.com/api/v2/jobs/public/jobs.json", params={"page": page, "per_page": 50}).json()
        for j in r.get("data", []):
            loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x) or ("Remote" if j.get("remote") else "")
            jj = job({**src, "name": (j.get("company") or {}).get("name") or j.get("company_name") or "ArtStation"},
                     j.get("title", ""), j.get("url") or f"https://www.artstation.com/jobs/{j.get('hash_id', '')}",
                     loc, j.get("published_at", ""), remote=j.get("remote"))
            jj["source"] = "artstation"
            out.append(jj)
    return out


def agg_linkedin():
    """LinkedIn public job search (logged-out, no account involved). Gentle: a few queries, pauses, 1 retry."""
    url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
    out = []
    for q in CONFIG["linkedin_queries"]:
        for start in range(0, q.get("pages", 1) * 10, 10):
            params = {"keywords": q["keywords"], "location": q["location"], "f_TPR": "r604800", "start": start}
            if q.get("distance"):
                params["distance"] = q["distance"]
            if q.get("remote"):
                params["f_WT"] = "2"
            r = None
            for attempt in range(2):
                r = requests.get(url, params=params, headers=hdr, timeout=TIMEOUT)
                if r.status_code != 429:
                    break
                time.sleep(20)
            time.sleep(3)
            if r is None or r.status_code != 200 or not r.text.strip():
                break
            cards = re.split(r'<li>', r.text)
            n = 0
            for c in cards:
                t = re.search(r'base-search-card__title">\s*(.*?)\s*</h3>', c, re.S)
                if not t:
                    continue
                n += 1
                comp = re.search(r'base-search-card__subtitle">(.*?)</h4>', c, re.S)
                loc = re.search(r'job-search-card__location">\s*(.*?)\s*</span>', c, re.S)
                link = re.search(r'href="(https://[^"]*linkedin\.com/jobs/view/[^"?]+)', c)
                date = re.search(r'datetime="([^"]+)"', c)
                src = {"name": strip_html(comp.group(1)) if comp else "LinkedIn", "ats": "linkedin", "track": q["track"]}
                j = job(src, t.group(1), link.group(1) if link else "", loc.group(1) if loc else "",
                        date.group(1) if date else "", remote=bool(q.get("remote")))
                j["source"] = "linkedin"
                out.append(j)
            if n < 10:
                break
    return out


# --------------------------------------------------------------------------- matching + scoring
def matches(title: str, track: str):
    t = title.lower()
    rules = CONFIG["tracks"][track]
    if any(re.search(x, t) for x in rules["exclude"]):
        return False, 0
    core = [x for x in rules["core"] if re.search(x, t)]
    near = [x for x in rules["near"] if re.search(x, t)]
    if not core and not near:
        return False, 0
    return True, (60 if core else 30)


def score(j):
    ok, base = matches(j["title"], j["track"])
    if not ok:
        return 0
    s = base
    loc = (j["location"] or f"{j['country_hint']} {j['extra']}").lower()
    loc = re.sub(r",\s*gb\b", ", united kingdom", re.sub(r",\s*in\b", ", india", loc))
    t = j["title"].lower()
    prefs = CONFIG["location_scores"]
    for pat, pts in prefs.items():
        if re.search(pat, loc):
            s += pts
            break
    if j.get("remote") or "remote" in loc:
        s += CONFIG["remote_bonus"]
    if re.search(r"senior|lead|supervisor|principal", t):
        s += 10
    if j["track"] == "it" and j.get("source") != "linkedin":   # LinkedIn IT queries are already radius-limited
        if not re.search(CONFIG["it_local_regex"], loc) and "remote" not in loc:
            return 0
    return max(0, min(100, s))


# --------------------------------------------------------------------------- notify
def ntfy(title, msg, url, priority="high"):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic or os.environ.get("JOBWATCH_DRY") == "1":
        return
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"),
                      headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                               "Priority": priority, "Click": url, "Tags": "briefcase"}, timeout=15)
    except Exception as e:
        print("ntfy failed:", e)


def github_issue(lines):
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if os.environ.get("JOBWATCH_ISSUES") != "1":
        return
    if not tok or not repo or not lines or os.environ.get("JOBWATCH_DRY") == "1":
        return
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    title = f"JobWatch — nouvelles offres {NOW:%Y-%m-%d}"
    api = f"https://api.github.com/repos/{repo}/issues"
    found = requests.get(api, headers=h, params={"state": "open", "labels": "jobwatch"}, timeout=20).json()
    issue = next((i for i in found if isinstance(i, dict) and i.get("title") == title), None)
    body = "\n".join(lines)
    if issue:
        requests.post(f"{api}/{issue['number']}/comments", headers=h, json={"body": body}, timeout=20)
    else:
        requests.post(api, headers=h, json={"title": title, "body": body, "labels": ["jobwatch"]}, timeout=20)


# --------------------------------------------------------------------------- main
def load_sources():
    srcs, seen_keys = [], set()
    for f in sorted(SOURCES.glob("*.json")):
        if f.name == "aggregators.json":
            continue
        for s in json.loads(f.read_text(encoding="utf-8")):
            if s.get("ats") not in FETCHERS or not (s.get("id") or s.get("careers_url")):
                continue
            if s["ats"] == "html" and not s.get("careers_url"):
                continue
            key = (s["ats"], (s.get("id") or s.get("careers_url") or "").lower())
            if key in seen_keys:          # shared boards (Ubisoft2, RodeoFX, PitchBlack...) are read once
                continue
            seen_keys.add(key)
            srcs.append(s)
    return srcs


def fetch_one(s):
    try:
        return s, FETCHERS[s["ats"]](s), None
    except Exception as e:
        return s, [], f"{type(e).__name__}: {str(e)[:120]}"


def main():
    DATA.mkdir(exist_ok=True)
    seen_p, jobs_p, health_p = DATA / "seen.json", DATA / "jobs.json", DATA / "health.json"
    seen = json.loads(seen_p.read_text()) if seen_p.exists() else {}
    first_run = not seen
    jobs = json.loads(jobs_p.read_text()) if jobs_p.exists() else []
    srcs = load_sources()
    print(f"{len(srcs)} sources")

    results, health = [], {}
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for s, items, err in ex.map(fetch_one, srcs):
            health[s["name"]] = {"ats": s["ats"], "ok": err is None, "count": len(items), "error": err}
            results += items
    for name, fn in (("Job Sheet", agg_google_sheet), ("VFXwire", agg_vfxwire),
                     ("RSS feeds", agg_rss_feeds), ("ArtStation", agg_artstation), ("LinkedIn", agg_linkedin)):
        try:
            items = fn()
            results += items
            health[name] = {"ats": "aggregator", "ok": True, "count": len(items), "error": None}
        except Exception as e:
            health[name] = {"ats": "aggregator", "ok": False, "count": 0, "error": str(e)[:120]}

    new_hits, digest = [], []
    for j in results:
        if j["title"] == "__PAGE__":            # html page-change detection
            old = seen.get(j["uid"])
            seen[j["uid"]] = j["extra"]
            if old and old != j["extra"]:
                new_hits.append({**j, "title": f"Page carrières modifiée — {j['company']}", "score": 40})
            continue
        sc = score(j)
        if sc <= 0 or j["uid"] in seen:
            continue
        if j["source"] == "vfxwire":              # enrich: real studio + location from the listing page
            # 1) studio from the URL slug: /jobs/listed/<title-slug>-<studio-slug>-<hash>
            m = re.search(r"/jobs/listed/(.+)-[0-9a-f]{6}$", j["url"])
            tslug = re.sub(r"[^a-z0-9]+", "-", j["title"].lower()).strip("-")
            if m and m.group(1).startswith(tslug + "-"):
                j["company"] = m.group(1)[len(tslug) + 1:].replace("-", " ").title()
            try:
                pg = get(j["url"]).text
                t = strip_html((re.search(r"<title>(.*?)</title>", pg, re.S) or [None, ""])[1])
                t = re.sub(r"\s*[·|]\s*VFXwire.*$", "", t)
                if " at " in t:                   # 2) exact studio name from "<Title> at <Studio>"
                    j["company"] = t.rsplit(" at ", 1)[1].strip()
                loc = re.search(r'(?i)(?:location|lieu)[^<]{0,20}</[^>]+>\s*<[^>]+>([^<]{3,80})<', pg)
                if loc:
                    j["location"] = strip_html(loc.group(1))
                sc = score(j) or sc
            except Exception:
                pass
        seen[j["uid"]] = NOW.isoformat()
        j.update(score=sc, found=NOW.isoformat(), status="new")
        new_hits.append(j)

    jobs = (new_hits + jobs)[: CONFIG["keep_last"]]
    jobs_p.write_text(json.dumps(jobs, indent=1, ensure_ascii=False), encoding="utf-8")
    seen_p.write_text(json.dumps(seen), encoding="utf-8")
    health_p.write_text(json.dumps({"run": NOW.isoformat(), "sources": health}, indent=1), encoding="utf-8")

    ok = sum(1 for h in health.values() if h["ok"])
    print(f"sources ok {ok}/{len(health)} | jobs read {len(results)} | new matches {len(new_hits)}")
    if first_run:
        print("First run: baseline saved, no alerts sent.")
        return
    for j in sorted(new_hits, key=lambda x: -x["score"]):
        line = f"- **{j['score']}** · [{j['title']}]({j['url']}) — {j['company']} · {j['location']}"
        digest.append(line)
        print(line)
        if j["score"] >= CONFIG["push_threshold"]:
            ntfy(f"{j['company']}: {j['title']}", f"{j['location']} · score {j['score']}", j["url"])
    github_issue(digest)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"done in {time.time() - t0:.0f}s")
