#!/usr/bin/env python3
"""job-matcher helper (Python 3.10+, standard library only).

Deterministic plumbing for the job-matcher plugin. Claude does the reading and
scoring; this script does the parts that should never vary between runs:
fetching public job boards, filtering by date/title/location, remembering which
roles were already shown, and rendering the dashboard page.

Subcommands
  resume-text FILE [--redact]      Print plain text from a .pdf, .docx, .txt or .md resume.
                                   --redact strips email, phone, links and street address.
  probe NAME [NAME ...]            Find live Greenhouse / Ashby / Lever boards for company names.
  fetch --profile P --boards B     Pull fresh postings from the boards in B that fit profile P.
        [--state S] [--days N]     Roles already in state S are skipped. JSON list to stdout.
  dedupe --state S [--profile P] FILE
                                   Filter any candidate list (web search, connectors) the same way.
  state-extract FILE               Read the state block out of a previous dashboard page.
  build --profile P --state S --scored F --out OUT [--date YYYY-MM-DD]
                                   Merge today's scored roles into the state and write the page.
  empty --profile P --out OUT      Write a placeholder page (used by setup before the first run).

Profile, boards, state and scored-role formats are documented in
skills/setup/references/profile-schema.md and skills/run/references/scoring.md.
Progress and counts go to stderr; machine output goes to stdout.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import html
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

UA = "job-matcher-plugin/1.0 (+https://github.com)"
STATE_VERSION = 1
SEEN_RETENTION_DAYS = 60
RECENT_DAYS = 7


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- keys -----
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def job_key(company: str, title: str) -> str:
    """Stable identity for a role: normalized company + title."""
    co = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|labs|hq)\b", "", (company or "").lower())
    return f"{_slug(co)}|{_slug(title)}"


def h10(s: str) -> str:
    """Short hash used in the remembered-roles memory, so it stays small (about 20 bytes per role)."""
    return hashlib.sha1((s or "").strip().lower().encode("utf-8")).hexdigest()[:8]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# ---------------------------------------------------------------- http -----
def http_json(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)  # Greenhouse double-encodes its HTML
    s = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def parse_iso(s):
    if not s:
        return None
    try:
        if isinstance(s, (int, float)):
            return datetime.fromtimestamp(s / 1000.0, tz=timezone.utc)
        s = str(s)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ------------------------------------------------------------- excerpt -----
REQ_HEADING_RE = re.compile(
    r"(who you are|what you.{0,3}ll need|what you.{0,3}ll bring|qualifications|requirements|"
    r"about you|what we.{0,3}re looking for|you have|you bring|your experience|must have)", re.I)
YEARS_RE = re.compile(r"\b\d{1,2}\s*\+?\s*(?:[-–]\s*\d{1,2}\s*)?years?\b", re.I)


def excerpt(text: str, head: int = 3500, tail: int = 3500) -> str:
    """Head of the posting plus its requirements block.

    The lines that decide a score (years required, degree wording, state
    eligibility) usually sit far below the top of a posting, so a plain
    head-truncate hides them. Keep the head and splice in the requirements.
    """
    if not text or len(text) <= head + tail:
        return text or ""
    out, rest = text[:head], text[head:]
    m = REQ_HEADING_RE.search(rest) or YEARS_RE.search(rest)
    if m:
        start = max(0, m.start() - 200)
        out += "\n[...]\n" + rest[start:start + tail]
    loc = re.search(r"(?i)(not (?:be )?eligible|cannot (?:hire|employ)|must (?:reside|live|be located)|"
                    r"open to candidates in|eligible to work in)", rest)
    if loc and loc.start() > (m.start() + tail if m else 0):
        out += "\n[...]\n" + rest[max(0, loc.start() - 150):loc.start() + 500]
    return out


# ------------------------------------------------------------- filters -----
def _phrase_re(phrases):
    parts = []
    for p in phrases or []:
        p = p.strip().lower()
        if not p:
            continue
        esc = r"\s+".join(re.escape(w) for w in p.split())
        parts.append(rf"(?<![a-z0-9]){esc}(?![a-z0-9])")
    return re.compile("|".join(parts), re.I) if parts else None


class Filters:
    def __init__(self, profile: dict):
        f = profile.get("filters", {})
        self.inc = _phrase_re(f.get("title_include") or profile.get("target_titles") or [])
        self.exc = _phrase_re(f.get("title_exclude") or [])
        self.excluded_companies = [_norm(c) for c in profile.get("excluded_companies", []) if _norm(c)]
        loc = profile.get("locations", {})
        aliases = []
        for place in loc.get("places", []):
            aliases += [place.get("name", "")] + place.get("aliases", [])
        self.place_re = _phrase_re([a.split(",")[0] for a in aliases if a])
        self.remote_ok = bool(loc.get("remote_ok", True))
        self.country = (loc.get("country") or "United States").lower()
        self.us = self.country in ("united states", "us", "usa", "united states of america")

    def title_ok(self, title: str) -> bool:
        if not title or not self.inc or not self.inc.search(title):
            return False
        return not (self.exc and self.exc.search(title))

    def company_excluded(self, company: str) -> bool:
        c = f" {_norm(company)} "
        return any(f" {x} " in c for x in self.excluded_companies)

    def location_ok(self, loc: str, desc: str = "", ats_remote: bool = False) -> bool:
        loc = loc or ""
        if self.us and NON_US_RE.search(loc) and not (US_RE.search(loc) or (self.place_re and self.place_re.search(loc))):
            return False
        if self.place_re and self.place_re.search(loc):
            return True
        if self.remote_ok:
            if ats_remote or REMOTE_RE.search(loc):
                return True
            if self.us and any(US_ONLY_RE.match(p) for p in re.split(r"[|;/]", loc)):
                return True
            if DESC_REMOTE_RE.search(desc or ""):
                return True
        if self.place_re and desc and DESC_PLACE_HINT.search(desc):
            for m in DESC_PLACE_HINT.finditer(desc):
                if self.place_re.search(m.group(0)):
                    return True
        return False


NON_US_RE = re.compile(r"\b(canada|toronto|vancouver|montr[eé]al|mexico|india|bangalore|bengaluru|hyderabad|"
                       r"pune|united kingdom|uk|england|london|ireland|dublin|germany|berlin|munich|france|paris|"
                       r"spain|madrid|barcelona|portugal|lisbon|netherlands|amsterdam|poland|warsaw|brazil|"
                       r"s[aã]o paulo|argentina|colombia|chile|australia|sydney|melbourne|japan|tokyo|singapore|"
                       r"korea|seoul|israel|tel aviv|philippines|emea|apac|latam|europe)\b", re.I)
US_RE = re.compile(r"\b(us|u\.s\.?a?\.?|usa|united states|americas?)\b", re.I)
REMOTE_RE = re.compile(r"\b(remote|anywhere|distributed|work from home)\b", re.I)
US_ONLY_RE = re.compile(r"^\W*(?:(?:the\s+)?united\s+states(?:\s+of\s+america)?|usa|u\.s\.a\.?|us|u\.s\.?)\W*$", re.I)
DESC_REMOTE_RE = re.compile(
    r"(\bremote\s*[-–(]?\s*(?:in\s+(?:the\s+)?)?(?:us|u\.s\.|usa|united states)\b|"
    r"\b(?:us|u\.s\.|united states)[- ]remote\b|\bremote[- ](?:eligible|friendly|first|optional)\b|"
    r"\bopen to (?:fully )?remote\b|\bor (?:fully )?remote\b|\bcan be (?:performed|done|worked) remotely\b)", re.I)
DESC_PLACE_HINT = re.compile(r"(?i)(?:based in|office in|offices in|located in|hybrid in|locations?:)[^.\n]{0,80}")


def classify_workplace(loc: str, field=None, is_remote=None, desc: str = "") -> str:
    for src in (field or "", loc or ""):
        s = str(src).lower()
        if "hybrid" in s:
            return "Hybrid"
        if "remote" in s:
            return "Remote"
        if "onsite" in s or "on-site" in s or "in office" in s or "in-office" in s:
            return "Onsite"
    if is_remote is True:
        return "Remote"
    d = (desc or "")[:4000].lower()
    if "hybrid" in d:
        return "Hybrid"
    if re.search(r"\b(fully remote|remote[- ]first|100% remote)\b", d):
        return "Remote"
    return "Unknown"


# --------------------------------------------------------------- boards ----
def _gh_salary(j):
    for m in j.get("metadata") or []:
        if "salary" in (m.get("name") or "").lower() or "compensation" in (m.get("name") or "").lower():
            v = m.get("value")
            if v:
                return str(v)
    return None


def fetch_greenhouse(slug, cutoff, F):
    data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in data.get("jobs") or []:
        title = (j.get("title") or "").strip()
        if not F.title_ok(title):
            continue
        posted = parse_iso(j.get("first_published") or j.get("updated_at"))
        if not posted or posted < cutoff:
            continue
        loc = (j.get("location") or {}).get("name") or ""
        offices = " | ".join(o.get("name", "") for o in j.get("offices") or [])
        desc = html_to_text(j.get("content") or "")
        wp_field = next((str(m.get("value")) for m in j.get("metadata") or []
                         if (m.get("name") or "").lower() in ("workplace type", "work model", "remote/hybrid/onsite", "location type")
                         and m.get("value")), None)
        if not F.location_ok(f"{loc} | {offices}".strip(" |"), desc, ats_remote=bool(wp_field and "remote" in wp_field.lower())):
            continue
        out.append(dict(source="greenhouse", board=slug, company=j.get("company_name") or slug.replace("-", " ").title(),
                        title=title, location=loc or offices, url=j.get("absolute_url") or
                        f"https://job-boards.greenhouse.io/{slug}/jobs/{j.get('id')}",
                        posted=posted.date().isoformat(), workplace=classify_workplace(loc, wp_field, None, desc),
                        salary=_gh_salary(j), description=excerpt(desc), _full=desc))
    return out


def _ashby_salary(comp):
    if not comp:
        return None
    s = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary")
    return s.strip() if isinstance(s, str) and s.strip() else None


def fetch_ashby(slug, cutoff, F):
    data = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in data.get("jobs") or []:
        title = (j.get("title") or "").strip()
        if not F.title_ok(title) or j.get("isListed") is False:
            continue
        posted = parse_iso(j.get("publishedAt") or j.get("updatedAt"))
        if not posted or posted < cutoff:
            continue
        sec = " | ".join(x.get("location", "") for x in j.get("secondaryLocations") or [])
        loc = " | ".join(x for x in [j.get("location") or "", sec] if x)
        desc = html_to_text(j.get("descriptionHtml") or "") or (j.get("descriptionPlain") or "")
        wpt = str(j.get("workplaceType") or "").lower()
        ats_remote = ("remote" in wpt) if wpt else bool(j.get("isRemote"))  # workplaceType wins when both are set
        if not F.location_ok(loc, desc, ats_remote=ats_remote):
            continue
        out.append(dict(source="ashby", board=slug, company=(data.get("organizationName") or slug.replace("-", " ").title()),
                        title=title, location=loc, url=j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{j.get('id', '')}",
                        posted=posted.date().isoformat(), workplace=classify_workplace(loc, j.get("workplaceType"), ats_remote or None, desc),
                        salary=_ashby_salary(j.get("compensation")), description=excerpt(desc), _full=desc))
    return out


def _lever_salary(sr):
    try:
        if sr and sr.get("min") and sr.get("max"):
            per = (sr.get("interval") or "").replace("per-", "/")
            return f"{sr.get('currency') or 'USD'} {int(sr['min']):,}-{int(sr['max']):,}{(' ' + per) if per and 'year' not in per else ''}"
    except Exception:
        pass
    return None


def fetch_lever(slug, cutoff, F):
    data = http_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        raise ValueError("unexpected response")
    out = []
    for j in data:
        title = (j.get("text") or "").strip()
        if not F.title_ok(title):
            continue
        posted = parse_iso(j.get("createdAt"))
        if not posted or posted < cutoff:
            continue
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or ([cats.get("location")] if cats.get("location") else [])
        loc = " | ".join(x for x in locs if x)
        desc = "\n".join(x for x in [(j.get("descriptionPlain") or "").strip(),
                                       "\n".join(f"{l.get('text', '')}\n{html_to_text(l.get('content', ''))}" for l in j.get("lists") or []),
                                       (j.get("additionalPlain") or "").strip()] if x)
        if not F.location_ok(loc, desc, ats_remote="remote" in str(j.get("workplaceType") or "").lower()):
            continue
        out.append(dict(source="lever", board=slug, company=slug.replace("-", " ").title(), title=title, location=loc,
                        url=j.get("hostedUrl") or "", posted=posted.date().isoformat(),
                        workplace=classify_workplace(loc, j.get("workplaceType"), None, desc),
                        salary=_lever_salary(j.get("salaryRange")), description=excerpt(desc), _full=desc))
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "ashby": fetch_ashby, "lever": fetch_lever}


def _board_slugs(boards: dict, platform: str):
    out = []
    for b in boards.get(platform, []) or []:
        s = b.get("slug") if isinstance(b, dict) else b
        if s and s not in out:
            out.append(s)
    return out


def merge_boards(*sources) -> dict:
    """Union of board lists. Entries are {"slug", "name"} dicts (bare slug strings are accepted)."""
    merged = {p: [] for p in FETCHERS}
    for src in sources:
        for p in FETCHERS:
            have = {b["slug"]: b for b in merged[p]}
            for b in (src or {}).get(p, []) or []:
                b = {"slug": b} if isinstance(b, str) else {k: b[k] for k in ("slug", "name") if b.get(k)}
                if not b.get("slug"):
                    continue
                if b["slug"] in have:
                    if b.get("name") and not have[b["slug"]].get("name"):
                        have[b["slug"]]["name"] = b["name"]
                    continue
                merged[p].append(b)
                have[b["slug"]] = b
    return merged


def cmd_fetch(a):
    profile = _load(a.profile, {})
    boards = merge_boards(_load(a.boards, {}), (_load(a.state, {}) if a.state else {}).get("boards", {}))
    state = _load(a.state, {}) if a.state else {}
    F = Filters(profile)
    days = a.days or profile.get("posted_within_days", 14)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    jobs, errors = [], []
    tasks = [(p, s) for p in FETCHERS for s in _board_slugs(boards, p)]
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(FETCHERS[p], s, cutoff, F): (p, s) for p, s in tasks}
        for fu in cf.as_completed(futs):
            p, s = futs[fu]
            try:
                rows = fu.result()
                if rows:
                    log(f"  {p}:{s} -> {len(rows)}")
                jobs += rows
            except Exception as e:  # noqa: BLE001
                errors.append(f"{p}:{s}")
                log(f"  WARN {p}:{s}: {e}")
    total = len(jobs)
    names = {}
    for src in (_load(a.boards, {}), state.get("boards", {})):
        for p in FETCHERS:
            for b in (src or {}).get(p, []) or []:
                if isinstance(b, dict) and b.get("slug") and b.get("name"):
                    names[(p, b["slug"])] = b["name"]
    for j in jobs:
        j["company"] = names.get((j["source"], j["board"]), j["company"])
    jobs = [j for j in jobs if not F.company_excluded(j["company"])]
    jobs, skipped = _drop_seen(jobs, state)
    for j in jobs:
        j.pop("_full", None)
        j["key"] = job_key(j["company"], j["title"])
    jobs.sort(key=lambda j: j["posted"], reverse=True)
    log(f"boards checked: {len(tasks)} ({len(errors)} errored) | matched filters: {total} | "
        f"already seen or duplicate: {skipped} | new candidates: {len(jobs)}")
    if errors and len(errors) == len(tasks) and tasks:
        log("ALL BOARDS FAILED: the shell probably cannot reach the job-board APIs. Use the WebFetch fallback.")
        json.dump({"error": "network", "boards": boards, "candidates": []}, sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 2
    json.dump(jobs, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _drop_seen(jobs, state):
    seen = (state or {}).get("seen", {})
    urls = {r.get("u") for r in (state or {}).get("recent", []) if r.get("u")}
    keep, skipped, batch = [], 0, set()
    for j in jobs:
        k = job_key(j.get("company", ""), j.get("title", ""))
        if h10(k) in seen or (j.get("url") and j["url"] in urls) or k in batch:
            skipped += 1
            continue
        batch.add(k)
        keep.append(j)
    return keep, skipped


def cmd_dedupe(a):
    items = _load(a.file, [])
    state = _load(a.state, {})
    if a.profile:
        F = Filters(_load(a.profile, {}))
        items = [i for i in items if not F.company_excluded(i.get("company", ""))]
    keep, skipped = _drop_seen(items, state)
    for j in keep:
        j["key"] = job_key(j.get("company", ""), j.get("title", ""))
    log(f"dedupe: {len(items)} in, {skipped} already seen, {len(keep)} new")
    json.dump(keep, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------- probe ----
def _slug_variants(name: str):
    n = name.strip().lower()
    n = re.sub(r"\b(inc|llc|ltd|corp|corporation|co)\.?$", "", n).strip(" ,.")
    base = re.sub(r"[^a-z0-9 ]+", "", n)
    words = base.split()
    v = ["".join(words), "-".join(words)]
    if len(words) > 1 and words[-1] in ("ai", "hq", "labs", "app", "io", "inc", "technologies", "health"):
        v.append("".join(words[:-1]))
    out = []
    for x in v:
        if x and x not in out:
            out.append(x)
    return out


def _probe_one(platform, slug):
    try:
        if platform == "greenhouse":
            d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", 12)
            return len(d.get("jobs") or [])
        if platform == "ashby":
            d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", 12)
            return len(d.get("jobs") or [])
        d = http_json(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=200", 12)
        return len(d) if isinstance(d, list) else 0
    except urllib.error.HTTPError as e:
        return 0 if e.code in (404, 400) else -1
    except Exception:
        return -1


def cmd_probe(a):
    names = list(a.names)
    if a.file:
        names += [x.strip() for x in Path(a.file).read_text().splitlines() if x.strip()]
    tasks = [(n, p, s) for n in names for s in _slug_variants(n) for p in FETCHERS]
    found, net_err = {}, 0
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        res = list(ex.map(lambda t: (t, _probe_one(t[1], t[2])), tasks))
    for (n, p, s), cnt in res:
        if cnt < 0:
            net_err += 1
        if cnt > 0 and (n not in found or cnt > found[n]["jobs"]):
            found[n] = {"platform": p, "slug": s, "jobs": cnt}
    boards = {p: [] for p in FETCHERS}
    for n, f in found.items():
        boards[f["platform"]].append({"slug": f["slug"], "name": n, "jobs": f["jobs"]})
    missing = [n for n in names if n not in found]
    out = {"boards": boards, "not_found": missing}
    if net_err and net_err == len(tasks):
        out["error"] = "network"
        log("ALL PROBES FAILED: the shell probably cannot reach the job-board APIs.")
    log(f"probe: {len(names)} companies, {len(found)} live boards, {len(missing)} not on Greenhouse/Ashby/Lever")
    json.dump(out, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------- resume ---
def cmd_resume_text(a):
    p = Path(a.file)
    suf = p.suffix.lower()
    text = ""
    if suf == ".pdf":
        try:
            text = subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True, timeout=60).stdout
        except Exception:
            text = ""
        if not text.strip():
            try:
                from pypdf import PdfReader  # type: ignore
                text = "\n".join(pg.extract_text() or "" for pg in PdfReader(str(p)).pages)
            except Exception:
                text = ""
    elif suf == ".docx":
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab/>", "\t", xml)
        text = html.unescape(re.sub(r"<[^>]+>", "", xml))
    else:
        text = p.read_text(errors="replace")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if a.redact:
        text = redact(text)
    if not text:
        log("No text extracted (scanned image PDF?). Read the file directly or ask for a text version.")
        return 1
    print(text)
    return 0


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b(?:linkedin\.com|github\.com)/\S+")
STREET_RE = re.compile(r"(?im)^.*\b\d{1,6}\s+\w+(?:\s\w+){0,3}\s(?:st|street|ave|avenue|rd|road|blvd|lane|ln|dr|drive|ct|court|way|pl|place)\b.*$")


def redact(text: str) -> str:
    """Strip contact details a job score never needs: email, phone, links, street address."""
    text = EMAIL_RE.sub("[email]", text)
    text = PHONE_RE.sub("[phone]", text)
    text = URL_RE.sub("[link]", text)
    text = STREET_RE.sub("[address]", text)
    return text


# ---------------------------------------------------------------- state ----
STATE_RE = re.compile(r'<script type="application/json" id="jm-state">(.*?)</script>', re.S)


def empty_state():
    return {"version": STATE_VERSION, "seen": {}, "recent": [], "boards": {p: [] for p in FETCHERS}, "runs": []}


def cmd_state_extract(a):
    try:
        txt = Path(a.file).read_text(errors="replace")
    except OSError:
        txt = ""
    m = STATE_RE.search(txt)
    raw = m.group(1) if m else (txt.strip() if txt.strip().startswith("{") else None)
    st = empty_state()
    if raw:
        try:
            got = json.loads(raw)
            if isinstance(got, dict):
                st.update(got)
        except json.JSONDecodeError as e:
            log(f"WARN state block unreadable ({e}); starting fresh")
    else:
        log("no state block found; starting fresh")
    log(f"state: {len(st.get('seen', {}))} roles remembered, {sum(len(v) for v in st.get('boards', {}).values())} discovered boards, {len(st.get('runs', []))} runs")
    json.dump(st, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _today(a):
    return date.fromisoformat(a.date) if getattr(a, "date", None) else date.today()


def merge_state(state, scored, today, min_score, new_boards=None, run_info=None):
    """Add today's scored roles to the memory and prune it.

    Memory layout (kept small because Claude reads it back every run):
      seen    {hash(job_key): "YYMMDD"}   every role ever scored, kept for 60 days
      recent  [{k,c,t,u,s,d,w,l}]        roles shown in the last 7 days, for "Earlier this week"
    """
    st = empty_state()
    st.update(state or {})
    seen = st.setdefault("seen", {})
    d6 = today.strftime("%y%m%d")
    recent = [r for r in st.get("recent", []) if r.get("d", "") >= (today - timedelta(days=RECENT_DAYS)).isoformat()]
    have_recent = {r.get("k") for r in recent}
    for r in scored:
        k = r.get("key") or job_key(r.get("company", ""), r.get("title", ""))
        hk = h10(k)
        seen.setdefault(hk, d6)
        if (r.get("score") or 0) >= min_score and r.get("url") and k not in have_recent:
            recent.append({"k": k, "c": r.get("company", ""), "t": r.get("title", ""), "u": r["url"], "s": r.get("score"),
                           "d": today.isoformat(), "w": r.get("workplace", ""), "l": (r.get("location") or "")[:60]})
            have_recent.add(k)
    cut = (today - timedelta(days=SEEN_RETENTION_DAYS)).strftime("%y%m%d")
    st["seen"] = {k: v for k, v in seen.items() if isinstance(v, str) and v >= cut}
    st.pop("seen_urls", None)
    st["recent"] = recent
    st["boards"] = merge_boards(st.get("boards", {}), new_boards or {})
    runs = [r for r in st.get("runs", []) if r.get("date") != today.isoformat()]
    if run_info:
        runs.append({k: v for k, v in dict(run_info, date=today.isoformat()).items() if k != "notes"})
    st["runs"] = runs[-14:]
    st["version"] = STATE_VERSION
    return st


# ------------------------------------------------------------- dashboard ---
CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e6e6e3;--accent:#2f5bea;--good:#1f8a4c;--warn:#b35c00;--bad:#b42318;--chip:#f0f0ec;--on-accent:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1d1d20;--ink:#ececec;--muted:#9a9aa1;--line:#2c2c31;--accent:#7c95ff;--good:#4cc38a;--warn:#f0a35b;--bad:#ff7a6e;--chip:#26262b;--on-accent:#0f1222}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:920px;margin:0 auto;padding:28px 18px 60px}
header h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}header p{margin:0;color:var(--muted)}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:120px}
.stat b{display:block;font-size:20px}.stat span{color:var(--muted);font-size:12px}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0 16px}
.controls input{flex:1;min-width:180px;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
.controls button{padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
.controls button.on{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:0 0 12px;display:grid;grid-template-columns:56px 1fr;gap:14px}
.card.done{opacity:.45}.score{width:56px;height:56px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:22px;color:#fff;background:#1f7a45}
.score.s6{background:#5a8f29}.score.s7{background:#2f8f5b}.score.s8,.score.s9,.score.s10{background:#1f7a45}
.card h2{font-size:17px;margin:0}.card h2 a{color:var(--ink);text-decoration:none}.card h2 a:hover{text-decoration:underline}
.meta{color:var(--muted);font-size:13px;margin:2px 0 8px}.meta span+span:before{content:" \\00b7  "}
.flags{margin:6px 0}.flag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;background:color-mix(in srgb,var(--warn) 15%,transparent);color:var(--warn);margin:0 6px 4px 0}
.why{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.pt{font-size:12px;padding:2px 8px;border-radius:6px;background:var(--chip)}
.pt.pos{color:var(--good)}.pt.neg{color:var(--bad)}.pt.cap{color:var(--warn)}
.note{margin:6px 0 0}.angle,.unk{color:var(--muted);font-size:13px;margin:4px 0 0}
.actions{display:flex;gap:8px;margin-top:10px}.actions a,.actions button{font-size:13px;padding:6px 12px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--ink);cursor:pointer;text-decoration:none}
.actions a.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
section h3{font-size:15px;margin:28px 0 8px}.list{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 14px}
.list div{padding:7px 0;border-top:1px solid var(--line);font-size:14px}.list div:first-child{border-top:0}.list a{color:var(--accent)}
.empty{background:var(--card);border:1px dashed var(--line);border-radius:12px;padding:24px;text-align:center;color:var(--muted)}
footer{color:var(--muted);font-size:12px;margin-top:36px}
"""

JS = """
(function(){
  var KEY='jm-marks-v1',marks={};
  try{marks=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
  function save(){try{localStorage.setItem(KEY,JSON.stringify(marks))}catch(e){}}
  var min=0,q='';
  function apply(){
    document.querySelectorAll('.card').forEach(function(c){
      var s=+c.dataset.score,k=c.dataset.key,t=c.textContent.toLowerCase();
      c.classList.toggle('done',!!marks[k]);
      var b=c.querySelector('[data-act=applied]');if(b)b.textContent=marks[k]==='applied'?'Applied \\u2713':'Mark applied';
      c.style.display=(s>=min&&(!q||t.indexOf(q)>=0)&&marks[k]!=='hidden')?'':'none';
    });
  }
  document.addEventListener('click',function(e){
    var b=e.target.closest('[data-act]');if(!b)return;
    var c=b.closest('.card');
    if(b.dataset.act==='min'){min=+b.dataset.v;document.querySelectorAll('[data-act=min]').forEach(function(x){x.classList.toggle('on',x===b)});}
    else{var k=c.dataset.key,a=b.dataset.act;marks[k]=marks[k]===a?undefined:a;if(!marks[k])delete marks[k];save();}
    apply();
  });
  var s=document.getElementById('q');if(s)s.addEventListener('input',function(){q=s.value.toLowerCase();apply()});
  apply();
})();
"""


def _e(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def _pt_html(p):
    pts = p.get("points")
    reason = p.get("reason", "")
    quote = p.get("quote")
    if isinstance(pts, (int, float)):
        cls, label = ("pos", f"+{pts:g}") if pts > 0 else ("neg", f"{pts:g}".replace("-", "−"))
    else:
        cls, label = "cap", str(pts or "")
    title = f' title="{_e(quote)}"' if quote else ""
    return f'<span class="pt {cls}"{title}>{_e(label)} {_e(reason)}</span>'


SOURCE_NAMES = {"greenhouse": "via Greenhouse", "ashby": "via Ashby", "lever": "via Lever", "indeed": "via Indeed",
                "ziprecruiter": "via ZipRecruiter", "search": "via web search"}


def _card(r):
    s = int(r.get("score") or 0)
    meta = [x for x in [r.get("company"), r.get("location"), r.get("workplace") if r.get("workplace") not in (None, "", "Unknown") else None,
                        r.get("salary"), f"posted {r['posted']}" if r.get("posted") else None,
                        SOURCE_NAMES.get(r.get("source"), r.get("source"))] if x]
    flags = "".join(f'<span class="flag">{_e(f)}</span>' for f in r.get("flags") or [])
    why = "".join(_pt_html(p) for p in r.get("breakdown") or [] if isinstance(p, dict))
    if not why and isinstance(r.get("breakdown"), str):
        why = f'<span class="pt">{_e(r["breakdown"])}</span>'
    unk = r.get("unconfirmed") or []
    url = r.get("url") or "#"
    return f"""<article class="card" data-score="{s}" data-key="{_e(r.get('key') or job_key(r.get('company', ''), r.get('title', '')))}">
<div class="score s{min(s, 10)}">{s}</div><div>
<h2><a href="{_e(url)}" target="_blank" rel="noopener">{_e(r.get('title'))}</a></h2>
<div class="meta">{''.join(f'<span>{_e(m)}</span>' for m in meta)}</div>
{f'<div class="flags">{flags}</div>' if flags else ''}
{f'<div class="why">{why}</div>' if why else ''}
{f'<p class="note">{_e(r.get("fit_note"))}</p>' if r.get('fit_note') else ''}
{f'<p class="angle"><b>Resume angle:</b> {_e(r.get("resume_angle"))}</p>' if r.get('resume_angle') else ''}
{f'<p class="unk">Not stated in the posting: {_e(", ".join(unk))}</p>' if unk else ''}
{f'<p class="unk">About the company: {_e(r.get("company_note"))}</p>' if r.get('company_note') else ''}
<div class="actions"><a class="primary" href="{_e(url)}" target="_blank" rel="noopener">Open posting</a>
<button data-act="applied">Mark applied</button><button data-act="hidden">Hide</button></div>
</div></article>"""


def render(profile, state, scored_today, today, run_info):
    min_score = int(profile.get("min_score", 6))
    shown = [r for r in scored_today if (r.get("score") or 0) >= min_score and r.get("url")]
    shown.sort(key=lambda r: r.get("posted") or "", reverse=True)
    shown.sort(key=lambda r: -(r.get("score") or 0))
    recent_cut = (today - timedelta(days=RECENT_DAYS)).isoformat()
    today_keys = {r.get("key") or job_key(r.get("company", ""), r.get("title", "")) for r in scored_today}
    recent = sorted([v for v in state.get("recent", [])
                     if v.get("k") not in today_keys and recent_cut <= v.get("d", "") < today.isoformat()],
                    key=lambda v: (v.get("d", ""), v.get("s") or 0), reverse=True)
    name = profile.get("display_name") or "Your"
    possessive = f"{name}'s" if name != "Your" else "Your"
    ri = run_info or {}
    stats = [(len(shown), "new matches today"), (ri.get("candidates", len(scored_today)), "fresh roles checked"),
             (len(state.get("seen", {})), "roles remembered"),
             (sum(len(v) for v in state.get("boards", {}).values()), "extra boards found by search")]
    cards = "\n".join(_card(r) for r in shown) or (
        '<div class="empty">No new roles cleared your bar today. That is normal on quiet days: '
        'every role checked is remembered, so tomorrow only shows what is new.</div>')
    recent_html = ""
    if recent:
        rows = "".join(f'<div><b>{_e(v.get("s"))}</b> &nbsp;<a href="{_e(v["u"])}" target="_blank" rel="noopener">{_e(v.get("t"))}</a>'
                       f' at {_e(v.get("c"))} <span style="color:var(--muted)">({_e(v.get("d"))})</span></div>' for v in recent[:40])
        recent_html = f'<section><h3>Earlier this week</h3><div class="list">{rows}</div></section>'
    notes = ""
    if ri.get("notes"):
        notes = '<section><h3>Run notes</h3><div class="list">' + "".join(f"<div>{_e(n)}</div>" for n in ri["notes"]) + "</div></section>"
    # Escape every "<" so nothing in a company or title can end or confuse the script block.
    state_json = json.dumps(state, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(possessive)} job matches</title><style>{CSS}</style></head>
<body><main>
<header><h1>{_e(possessive)} job matches</h1><p>Updated {_e(f'{today:%A, %B} {today.day}, {today.year}')}. Scored against your resume; roles under {min_score}/10 are not shown.</p></header>
<div class="stats">{''.join(f'<div class="stat"><b>{_e(n)}</b><span>{_e(l)}</span></div>' for n, l in stats)}</div>
<div class="controls"><input id="q" placeholder="Filter by company, title or keyword"><button data-act="min" data-v="0" class="on">All</button><button data-act="min" data-v="7">7+</button><button data-act="min" data-v="8">8+</button></div>
{cards}
{recent_html}
{notes}
<footer>Made by the job-matcher plugin for Claude. "Mark applied" and "Hide" are remembered in this browser only.
Hover a score reason to see the line from the posting that triggered it.</footer>
</main>
<script>{JS}</script>
<script type="application/json" id="jm-state">{state_json}</script>
</body></html>
"""


def cmd_build(a):
    profile = _load(a.profile, {})
    state = _load(a.state, {}) or empty_state()
    scored = _load(a.scored, [])
    if isinstance(scored, dict):
        run_info = scored.get("run", {})
        new_boards = scored.get("discovered_boards", {})
        scored = scored.get("roles", [])
    else:
        run_info, new_boards = {}, {}
    for r in scored:
        r.setdefault("key", job_key(r.get("company", ""), r.get("title", "")))
    today = _today(a)
    min_score = int(profile.get("min_score", 6))
    run_info.setdefault("scored", len(scored))
    run_info["shown"] = sum(1 for r in scored if (r.get("score") or 0) >= min_score and r.get("url"))
    st = merge_state(state, scored, today, min_score, new_boards, run_info)
    Path(a.out).write_text(render(profile, st, scored, today, run_info))
    log(f"wrote {a.out}: {run_info['shown']} cards, {len(st['seen'])} roles remembered")
    return 0


def cmd_empty(a):
    profile = _load(a.profile, {})
    st = empty_state()
    page = render(profile, st, [], date.today(), {"candidates": 0, "notes": ["Setup complete. The first run will fill this page."]})
    Path(a.out).write_text(page)
    log(f"wrote {a.out}")
    return 0


# ------------------------------------------------------------------ main ---
def _load(path, default):
    if not path:
        return default
    p = Path(path)
    if not p.exists() or not p.read_text().strip():
        return default
    return json.loads(p.read_text())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("resume-text"); s.add_argument("file"); s.add_argument("--redact", action="store_true")
    s.set_defaults(fn=cmd_resume_text)
    s = sub.add_parser("probe"); s.add_argument("names", nargs="*"); s.add_argument("--file"); s.set_defaults(fn=cmd_probe)
    s = sub.add_parser("fetch"); s.add_argument("--profile", required=True); s.add_argument("--boards", required=True)
    s.add_argument("--state"); s.add_argument("--days", type=int); s.set_defaults(fn=cmd_fetch)
    s = sub.add_parser("dedupe"); s.add_argument("file"); s.add_argument("--state", required=True); s.add_argument("--profile")
    s.set_defaults(fn=cmd_dedupe)
    s = sub.add_parser("state-extract"); s.add_argument("file"); s.set_defaults(fn=cmd_state_extract)
    s = sub.add_parser("build"); s.add_argument("--profile", required=True); s.add_argument("--state")
    s.add_argument("--scored", required=True); s.add_argument("--out", required=True); s.add_argument("--date")
    s.set_defaults(fn=cmd_build)
    s = sub.add_parser("empty"); s.add_argument("--profile", required=True); s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_empty)
    a = ap.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
