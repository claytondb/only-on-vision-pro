#!/usr/bin/env python3
"""Keep the "Only on Vision Pro" directory current.

Finds Apple Vision Pro apps on the US App Store, keeps the ones whose listing runs on
Vision Pro and nothing else, and writes the site's data file plus app icons.

Sources (all public):
  * Vision Pro top charts on apps.apple.com (US every run, 3 other countries per day)
  * the Vision Pro "Apps & Games" and category pages
  * the iTunes Lookup API (device support, prices, dates, and every app per developer)
  * each app's App Store page (subtitle, and the platforms it is sold for)

The script is incremental and polite: it keeps its memory in state/, spaces out requests,
backs off on HTTP 429, and stops at a time budget so the rest can continue next run.
"""
import datetime as dt
import html as htmllib
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
SITE_DIR = os.path.join(ROOT, "site")
ICON_DIR = os.path.join(SITE_DIR, "icons")
DATA_FILE = os.path.join(SITE_DIR, "data", "apps.json")

START = time.time()
BUDGET_MIN = float(os.environ.get("UPDATE_BUDGET_MIN", "40"))
FULL_EVERY_H = float(os.environ.get("FULL_EVERY_H", "20"))
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()

GENRES = [36, 6000, 6001, 6002, 6003, 6004, 6005, 6006, 6007, 6008, 6009, 6010, 6011, 6012,
          6013, 6014, 6015, 6016, 6017, 6018, 6020, 6021, 6023, 6024, 6026, 6027, 7001, 7002,
          7003, 7004, 7005, 7006, 7009, 7011, 7012, 7013, 7014, 7015, 7016, 7017, 7018, 7019]
COUNTRIES = ["jp", "gb", "cn", "hk", "sg", "au", "ca", "fr", "de", "kr", "ae", "tw"]
COUNTRIES_PER_DAY = 3
GAME_SUB = {7001: "Action", 7002: "Adventure", 7003: "Casual", 7004: "Board", 7005: "Card",
            7006: "Casino", 7009: "Family", 7011: "Music", 7012: "Puzzle", 7013: "Racing",
            7014: "Role Playing", 7015: "Simulation", 7016: "Sports", 7017: "Strategy",
            7018: "Trivia", 7019: "Word"}
CAT_RENAME = {"Book": "Books"}
GENRE_WORDS = {"games", "entertainment", "utilities", "education", "productivity", "photo & video",
               "lifestyle", "graphics & design", "music", "health & fitness", "business", "travel",
               "social networking", "sports", "medical", "developer tools", "finance", "reference",
               "food & drink", "shopping", "news", "weather", "book", "books", "navigation",
               "magazines & newspapers", "kids"} | {v.lower() for v in GAME_SUB.values()}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")


def log(*a):
    print(f"[{(time.time() - START) / 60:5.1f}m]", *a, flush=True)


def time_left():
    return BUDGET_MIN * 60 - (time.time() - START)


# ---------------------------------------------------------------- HTTP ---------------------------

class Http:
    """Per-host pacing, retries with backoff, and a circuit breaker per host."""
    PACE = {"apps.apple.com": 1.25, "itunes.apple.com": 3.1}

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        self.last = {}
        self.fail_streak = {}
        self.dead = set()
        self.requests = 0
        self.errors = []

    def get(self, url, tries=5):
        host = url.split("/")[2]
        if host in self.dead:
            return None
        pace = self.PACE.get(host, 0)
        for attempt in range(tries):
            wait = pace - (time.time() - self.last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self.last[host] = time.time()
            self.requests += 1
            try:
                r = self.s.get(url, timeout=40)
            except requests.RequestException as e:
                r = None
                err = e.__class__.__name__
            if r is not None and r.status_code == 200:
                self.fail_streak[host] = 0
                return r.content.decode("utf-8", "replace")
            if r is not None and r.status_code == 404:
                return None
            code = r.status_code if r is not None else err
            if r is not None and 400 <= r.status_code < 500 and r.status_code not in (403, 429):
                return None
            self.fail_streak[host] = self.fail_streak.get(host, 0) + 1
            if self.fail_streak[host] >= 12:
                self.dead.add(host)
                self.errors.append(f"{host}: gave up after repeated {code}")
                log(f"!! {host} keeps failing ({code}); skipping it for this run")
                return None
            backoff = min(90, 8 * (2 ** attempt)) + random.random() * 4
            log(f"   {code} on {url[:80]} - retry in {backoff:.0f}s")
            time.sleep(backoff)
        return None


http = Http()


def server_data(page):
    m = re.search(r'<script type="application/json" id="serialized-server-data">(.*?)</script>', page or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- state --------------------------

def load(name, default):
    path = os.path.join(STATE_DIR, name)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(name, obj):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)


apps = load("apps.json", {})          # id -> record for every Vision Pro-only app
excluded = load("excluded.json", {})  # id -> {"platforms": [...], "checked": date} (e.g. also on Apple TV)
devs = load("devs.json", {})          # artistId -> date last scanned ("" = never)
meta = load("meta.json", {})
status = {"started": NOW.isoformat(timespec="seconds"), "steps": {}, "errors": []}


# ---------------------------------------------------------------- helpers ------------------------

def is_exclusive(rec):
    devices = rec.get("supportedDevices") or []
    return bool(devices) and all(d.startswith("AppleVisionPro") for d in devices)


def is_vision(rec):
    return any(d.startswith("AppleVisionPro") for d in rec.get("supportedDevices") or [])


ZERO_WIDTH = re.compile("[" + chr(0x200B) + "-" + chr(0x200F) + chr(0xFEFF) + "]")


def clean(s):
    s = htmllib.unescape(s or "")
    s = ZERO_WIDTH.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_count(v):
    if isinstance(v, (int, float)):
        return int(v)
    v = str(v or "").strip().upper().replace(",", "")
    mult = 1
    if v.endswith("K"):
        mult, v = 1_000, v[:-1]
    elif v.endswith("M"):
        mult, v = 1_000_000, v[:-1]
    try:
        return int(float(v) * mult)
    except ValueError:
        return 0


def walk_lockups(obj, out):
    """Collect subtitle / rating from every app 'lockup' in an App Store page's data."""
    if isinstance(obj, dict):
        aid = obj.get("adamId")
        if aid and isinstance(obj.get("title"), str):
            rec = out.setdefault(str(aid), {})
            sub = obj.get("developerTagline") or obj.get("subtitle")
            if isinstance(sub, str) and sub.strip() and "sub" not in rec:
                rec["sub"] = sub.strip()
            rt, rc = obj.get("rating"), obj.get("ratingCount")
            if isinstance(rt, (int, float)) and rt > 0 and parse_count(rc) > 0 and "r" not in rec:
                rec["r"], rec["rc"] = round(float(rt), 1), parse_count(rc)
        for v in obj.values():
            walk_lockups(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_lockups(v, out)


def chart(country, genre):
    """Return ({'free': [ids], 'paid': [ids]}, lockups) for a Vision Pro chart page, or (None, {})."""
    page = http.get(f"https://apps.apple.com/{country}/vision/charts/{genre}", tries=3)
    d = server_data(page)
    try:
        segs = d["data"][0]["data"]["segments"]
    except (TypeError, KeyError, IndexError):
        return None, {}
    lists = {}
    for seg in segs:
        name = seg.get("chart")
        if name not in ("top-free-native", "top-paid-native"):
            continue
        ids = [str(it["adamId"]) for sh in seg.get("shelves", []) for it in (sh.get("items") or []) if it.get("adamId")]
        ids += [str(rc["id"]) for rc in (seg.get("nextPage") or {}).get("remainingContent", []) if rc.get("type") == "apps"]
        lists["free" if "free" in name else "paid"] = ids
    lockups = {}
    if country == "us":
        walk_lockups(d, lockups)
    return lists, lockups


def lookup_ids(ids):
    """iTunes Lookup for app ids -> {id: record}. Missing ids simply aren't in the result."""
    out = {}
    ids = list(dict.fromkeys(str(i) for i in ids))
    for k in range(0, len(ids), 150):
        chunk = ids[k:k + 150]
        txt = http.get(f"https://itunes.apple.com/lookup?id={','.join(chunk)}&country=us&entity=software&limit=200")
        if txt is None:
            raise RuntimeError("iTunes lookup failed")
        for r in json.loads(txt).get("results", []):
            if r.get("wrapperType") == "software" and r.get("trackId"):
                out[str(r["trackId"])] = r
    return out


def lookup_devs(artist_ids):
    """iTunes Lookup: every app from each developer (limit applies per developer)."""
    url = ("https://itunes.apple.com/lookup?id=" + ",".join(map(str, artist_ids)) +
           "&entity=software&country=us&limit=200")
    txt = http.get(url)
    if txt is None:
        return None
    return [r for r in json.loads(txt).get("results", []) if r.get("wrapperType") == "software"]


def absorb(r, source):
    """Update or add an app from an iTunes record. Returns True if it is a new Vision Pro-only app."""
    i = str(r["trackId"])
    if is_vision(r) or is_exclusive(r):
        devs.setdefault(str(r.get("artistId")), "")
    if not is_exclusive(r) or i in excluded:
        if i in apps and not is_exclusive(r):
            devices = r.get("supportedDevices") or []
            if any(d.startswith("AppleTV") for d in devices):
                # the lookup API alternates between the tvOS and visionOS records of such apps
                excluded[i] = {"platforms": ["tv", "vision"], "checked": TODAY.isoformat(), "n": apps[i].get("n")}
                log(f"   - {apps[i].get('n')} ({i}) is also on Apple TV; removing")
            else:
                log(f"   - {r.get('trackName')} ({i}) is no longer Vision Pro-only")
            del apps[i]
        return False
    new = i not in apps
    rec = apps.setdefault(i, {"first": TODAY.isoformat(), "src": source})
    rec.update({
        "n": clean(r.get("trackName")),
        "d": clean(r.get("artistName") or r.get("sellerName")),
        "aid": str(r.get("artistId")),
        "c": r.get("primaryGenreName"),
        "cid": str(r.get("primaryGenreId")),
        "gids": [str(g) for g in r.get("genreIds", [])],
        "price": r.get("price"),
        "fprice": r.get("formattedPrice"),
        "rel": (r.get("releaseDate") or "")[:10],
        "upd": (r.get("currentVersionReleaseDate") or "")[:10],
        "desc": clean(r.get("description"))[:400],
        "url": (r.get("trackViewUrl") or f"https://apps.apple.com/us/app/id{i}").split("?")[0],
        "art": r.get("artworkUrl512") or r.get("artworkUrl100") or "",
        "miss": 0,
    })
    if new and source != "seed":
        log(f"   + {rec['n']} ({i}) via {source}")
    return new


def product_page(i):
    """App Store page -> {'platforms': [...], 'sub': str, 'iap': bool, 'at': date} or None."""
    url = apps[i].get("url") or f"https://apps.apple.com/us/app/id{i}"
    page = http.get(url, tries=4)
    d = server_data(page)
    try:
        pg = d["data"][0]["data"]
    except (TypeError, KeyError, IndexError):
        return None
    lk = pg.get("lockup") or {}
    offer = lk.get("offerDisplayProperties") or {}
    return {
        "platforms": pg.get("appPlatforms") or [],
        "sub": clean(lk.get("developerTagline") or lk.get("subtitle") or ""),
        "iap": bool(offer.get("hasInAppPurchases")),
        "at": TODAY.isoformat(),
    }


# ---------------------------------------------------------------- steps --------------------------

def step_us_charts():
    ranks, lockups, ok = {}, {}, 0
    for g in GENRES:
        lists, lk = chart("us", g)
        if lists is None:
            continue
        ok += 1
        lockups.update({k: v for k, v in lk.items() if k not in lockups})
        for ids in lists.values():
            for pos, i in enumerate(ids, 1):
                cur = ranks.setdefault(i, {})
                cur[str(g)] = min(pos, cur.get(str(g), 999))
    status["steps"]["us_charts"] = f"{ok}/{len(GENRES)} pages, {len(ranks)} apps"
    log(f"US charts: {ok}/{len(GENRES)} pages, {len(ranks)} ranked apps")
    return (ranks if ok >= len(GENRES) * 0.7 else None), lockups


def step_editorial():
    ids, lockups = set(), {}
    home = http.get("https://apps.apple.com/us/vision/apps-and-games", tries=3)
    pages = [home]
    for url in sorted(set(re.findall(r"https://apps\.apple\.com/us/vision/editorial/\d+", home or "")))[:20]:
        pages.append(http.get(url, tries=2))
    for page in pages:
        d = server_data(page)
        if d:
            walk_lockups(d, lockups)
    ids = set(lockups)
    status["steps"]["editorial"] = f"{sum(1 for p in pages if p)} pages, {len(ids)} apps"
    log(f"Editorial: {len(ids)} apps on {sum(1 for p in pages if p)} pages")
    return ids, lockups


def step_intl_charts():
    """Vision Pro charts in 3 other countries per day (all 12 every 4 days) -> {id: {cc: rank}}."""
    start = int(meta.get("country_idx", 0)) % len(COUNTRIES)
    picks = [COUNTRIES[(start + k) % len(COUNTRIES)] for k in range(COUNTRIES_PER_DAY)]
    ranks, done = {}, []
    for cc in picks:
        if time_left() < 12 * 60:
            break
        for g in GENRES:
            lists, _ = chart(cc, g)
            for ids in (lists or {}).values():
                for pos, i in enumerate(ids, 1):
                    cur = ranks.setdefault(i, {})
                    cur[cc] = min(pos, cur.get(cc, 999))
        done.append(cc)
    meta["country_idx"] = (start + len(done)) % len(COUNTRIES)
    status["steps"]["intl_charts"] = f"{','.join(done) or 'none'}: {len(ranks)} apps"
    log(f"Other countries ({','.join(done) or 'none'}): {len(ranks)} ranked apps")
    return ranks


def step_scan_devs(limit):
    """Scan developers never scanned first, then the ones scanned longest ago."""
    order = sorted(devs, key=lambda a: (devs[a] != "", devs[a]))
    todo = [a for a in order if devs[a] != TODAY.isoformat()][:limit]
    found = 0
    scanned = 0
    for k in range(0, len(todo), 15):
        if time_left() < 5 * 60:
            break
        grp = todo[k:k + 15]
        res = lookup_devs(grp)
        if res is None:
            break
        for r in res:
            if absorb(r, "developer"):
                found += 1
        for a in grp:
            devs[a] = TODAY.isoformat()
        scanned += len(grp)
    status["steps"]["developers"] = f"{scanned} scanned, {found} new apps"
    log(f"Developers: scanned {scanned}, found {found} new Vision Pro-only apps")


def step_product_pages(refresh):
    """Check each app's store page once (and re-check a few old ones each day)."""
    # apps found by the updater itself go first: they are the ones that still need the platform check
    pending = sorted((i for i in apps if not apps[i].get("prod")),
                     key=lambda i: (apps[i].get("src") == "seed", apps[i].get("first", "")), reverse=False)
    stale = sorted((i for i in apps if apps[i].get("prod")), key=lambda i: apps[i]["prod"].get("at", ""))[:refresh]
    fetched = dropped = 0
    for i in pending + stale:
        if time_left() < 4 * 60:
            break
        if i not in apps:
            continue
        info = product_page(i)
        if info is None:
            apps[i]["prod_tries"] = apps[i].get("prod_tries", 0) + 1
            if apps[i]["prod_tries"] >= 3:
                apps[i]["prod"] = {"platforms": [], "sub": "", "at": TODAY.isoformat(), "failed": True}
            continue
        fetched += 1
        others = set(info["platforms"]) - {"vision"}
        if info["platforms"] and others:
            excluded[i] = {"platforms": info["platforms"], "checked": TODAY.isoformat(), "n": apps[i].get("n")}
            log(f"   - {apps[i].get('n')} is also on {', '.join(sorted(others))}; removing")
            del apps[i]
            dropped += 1
            continue
        apps[i]["prod"] = info
    left = sum(1 for i in apps if not apps[i].get("prod"))
    status["steps"]["product_pages"] = f"{fetched} checked, {dropped} removed, {left} still to check"
    log(f"Store pages: checked {fetched}, removed {dropped}, {left} left for next run")


def step_recheck_excluded():
    """Apps dropped for being on another platform get a fresh look once a month."""
    due = [i for i, v in excluded.items() if v.get("checked", "") < (TODAY - dt.timedelta(days=30)).isoformat()][:20]
    for i in due:
        if time_left() < 4 * 60:
            break
        rec = lookup_ids([i]).get(i)
        if not rec or not is_exclusive(rec):
            excluded[i]["checked"] = TODAY.isoformat()
            continue
        del excluded[i]
        absorb(rec, "recheck")
        info = product_page(i)
        if info and set(info["platforms"]) - {"vision"}:
            excluded[i] = {"platforms": info["platforms"], "checked": TODAY.isoformat(), "n": rec.get("trackName")}
            apps.pop(i, None)
        elif info and i in apps:
            apps[i]["prod"] = info


def step_icons():
    os.makedirs(ICON_DIR, exist_ok=True)
    s = requests.Session()

    def fetch(i):
        rec = apps[i]
        path = os.path.join(ICON_DIR, f"{i}.webp")
        if os.path.exists(path) and rec.get("icon_src") == rec.get("art"):
            return "ok"
        src = re.sub(r"/\d+x\d+bb\.\w+$", "/128x128bb.webp", rec.get("art") or "")
        if not src.startswith("http"):
            return "none"
        for attempt in range(3):
            try:
                r = s.get(src, timeout=30)
                if r.status_code == 200 and r.content[:4] == b"RIFF":
                    with open(path, "wb") as f:
                        f.write(r.content)
                    rec["icon_src"] = rec.get("art")
                    return "new"
            except requests.RequestException:
                pass
            time.sleep(1 + attempt * 2)
        return "fail"

    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(fetch, list(apps)))
    keep = {f"{i}.webp" for i in apps}
    removed = 0
    for f in os.listdir(ICON_DIR):
        if f.endswith(".webp") and f not in keep:
            os.remove(os.path.join(ICON_DIR, f))
            removed += 1
    counts = {k: res.count(k) for k in set(res)}
    status["steps"]["icons"] = f"{counts}, removed {removed}"
    log(f"Icons: {counts}, removed {removed}")


def good_subtitle(sub, name):
    s = clean(sub)
    if not s or s.lower() in GENRE_WORDS or s.lower() == (name or "").lower():
        return None
    return s


def first_sentence(desc, name="", limit=90):
    s = clean(desc)
    s = re.sub(r'^[\s"“”\'\-–—•*·]+', "", s)
    s = re.sub(r"^\d+[.)]\s*", "", s)
    s = re.sub(r"^(welcome to|introducing|meet)\s+", "", s, flags=re.I)
    m = re.match(r"(.{12,}?(?:[.!?](?=\s|$)|[。！？]))", s)
    out = m.group(1) if m else s
    base = re.split(r"\s[-–—:|]\s|:\s", name or "")[0].strip()
    if base:
        m2 = re.match(r'^["“]?' + re.escape(base) + r'["”]?\s+(?:is|are)\s+(.+)$', out, flags=re.I)
        if m2 and len(m2.group(1)) > 12:
            out = m2.group(1)[0].upper() + m2.group(1)[1:]
    b = re.search(r"\s[-•]\s", out)
    if b and b.start() >= 20:
        out = out[:b.start()].rstrip(":;, ")
    out = out.strip('"“” ')
    if len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0].rstrip(",;:-–— ") + "…"
    return out


def build_site():
    rows = []
    for i, a in apps.items():
        cat = CAT_RENAME.get(a.get("c"), a.get("c") or "Other")
        sub_genre, sub_id = None, None
        if cat == "Games":
            for g in a.get("gids", []):
                if int(g) in GAME_SUB:
                    sub_genre, sub_id = GAME_SUB[int(g)], g
                    break
        lk = a.get("lk") or {}
        prod = a.get("prod") or {}
        subtitle = good_subtitle(prod.get("sub"), a.get("n")) or good_subtitle(lk.get("sub"), a.get("n"))
        if not subtitle:
            subtitle = first_sentence(a.get("desc"), a.get("n"))
        price = a.get("price")
        if price is None:
            plabel, pv = "Arcade", -1
        elif price == 0:
            plabel, pv = "Free", 0
        else:
            plabel, pv = a.get("fprice") or f"${price:.2f}", price
        rk = a.get("rank") or {}
        ra, rg, rs = rk.get("36"), rk.get(a.get("cid")), rk.get(sub_id) if sub_id else None
        cr = a.get("cr")
        if ra:
            pop = ra
        elif rg:
            pop = 200 + rg
        elif cr:
            pop = 400 + cr
        else:
            try:
                days = (TODAY - dt.date.fromisoformat(a.get("upd") or "2024-01-01")).days
            except ValueError:
                days = 999
            pop = 1000 + max(0, days)
        row = {"id": i, "n": a.get("n"), "d": a.get("d"), "c": cat, "g": sub_genre, "s": subtitle,
               "x": (a.get("desc") or "")[:320], "p": plabel, "pv": pv, "r": lk.get("r"), "rc": lk.get("rc"),
               "rel": a.get("rel"), "upd": a.get("upd"), "ra": ra, "rg": rg, "rs": rs, "pop": pop,
               "u": a.get("url"), "iap": prod.get("iap") or None,
               "ic": os.path.exists(os.path.join(ICON_DIR, f"{i}.webp")) or None}
        rows.append({k: v for k, v in row.items() if v not in (None, "")})
    rows.sort(key=lambda r: int(r["id"]))           # stable order keeps daily git diffs small
    out = {"meta": {"updated": NOW.isoformat(timespec="minutes"), "count": len(rows), "store": "US"}, "apps": rows}
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, DATA_FILE)
    log(f"Site data: {len(rows)} apps")
    return len(rows)


def seed():
    """First run: start from the seed list of known Vision Pro-only apps."""
    path = os.path.join(STATE_DIR, "seed.json")
    if apps or not os.path.exists(path):
        return
    with open(path) as f:
        s = json.load(f)
    for i, v in (s.get("excluded") or {}).items():
        excluded.setdefault(i, v)
    ids = [x for x in s.get("apps", "").split(",") if x]
    log(f"Seeding from {len(ids)} known apps")
    try:
        for r in lookup_ids(ids).values():
            absorb(r, "seed")
    except RuntimeError as e:
        status["errors"].append(f"seed: {e}")


# ---------------------------------------------------------------- main ---------------------------

def main():
    last_full = meta.get("last_full")
    due = not last_full or (NOW - dt.datetime.fromisoformat(last_full)).total_seconds() > FULL_EVERY_H * 3600
    if "--full" in sys.argv:
        due = True
    backlog = (not apps) or any(not a.get("prod") for a in apps.values()) or any(v == "" for v in devs.values())
    log(f"Apps known: {len(apps)} | full update due: {due} | backlog: {backlog}")
    if not due and not backlog:
        log("Nothing to do.")
        status["result"] = "nothing to do"
        return 0

    seed()

    if due:
        ranks, lockups = step_us_charts()
        ed_ids, ed_lockups = step_editorial()
        for k, v in ed_lockups.items():
            lockups.setdefault(k, v)
        # every app seen in the US native charts or on Apple's Vision Pro pages, plus what we know
        candidates = set(apps) | set(ed_ids) | set(ranks or {})
        try:
            found = lookup_ids(candidates)
        except RuntimeError as e:
            status["errors"].append(str(e))
            found = None
        if found is not None:
            new = sum(absorb(r, "charts") for r in found.values())
            gone = [i for i in list(apps) if i not in found]
            for i in gone:
                apps[i]["miss"] = apps[i].get("miss", 0) + 1
                if apps[i]["miss"] >= 2:
                    log(f"   - {apps[i].get('n')} ({i}) is no longer on the US App Store")
                    del apps[i]
            status["steps"]["lookup"] = f"{len(found)} records, {new} new apps, {len(gone)} not found"
            log(f"Lookup: {len(found)} records, {new} new, {len(gone)} not returned")
            # developers of every native Vision Pro app in the charts may ship a Vision-only app next
            for i in ranks or {}:
                r = found.get(i)
                if r:
                    devs.setdefault(str(r.get("artistId")), "")
        if ranks is not None:
            for i, a in apps.items():
                a["rank"] = ranks.get(i, {})
        for i, lk in lockups.items():
            if i in apps:
                old = apps[i].get("lk") or {}
                old.update(lk)
                apps[i]["lk"] = old

        intl = step_intl_charts()
        intl_new = [i for i in intl if i not in apps and i not in excluded]
        if intl_new:
            try:
                for r in lookup_ids(intl_new).values():
                    absorb(r, "intl charts")
            except RuntimeError as e:
                status["errors"].append(str(e))
        fresh = (TODAY - dt.timedelta(days=14)).isoformat()
        for i, a in apps.items():
            crs = {cc: v for cc, v in (a.get("crs") or {}).items() if v[1] >= fresh}
            for cc, pos in (intl.get(i) or {}).items():
                crs[cc] = [pos, TODAY.isoformat()]
            if crs:
                a["crs"] = crs
                a["cr"] = min(v[0] for v in crs.values())
            else:
                a.pop("crs", None), a.pop("cr", None)

        step_scan_devs(limit=450)
        step_recheck_excluded()
        step_icons()
        step_product_pages(refresh=60)
        meta["last_full"] = NOW.isoformat(timespec="seconds")
    else:
        step_scan_devs(limit=600)
        step_icons()
        step_product_pages(refresh=0)

    step_icons()
    count = build_site()
    status["result"] = f"{count} apps"
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception as e:  # keep whatever we have; report the error
        import traceback
        traceback.print_exc()
        status["errors"].append(f"{e.__class__.__name__}: {e}")
    finally:
        status["finished"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        status["minutes"] = round((time.time() - START) / 60, 1)
        status["requests"] = http.requests
        status["errors"] += http.errors
        status["apps"] = len(apps)
        save("apps.json", apps)
        save("excluded.json", excluded)
        save("devs.json", devs)
        save("meta.json", meta)
        save("status.json", status)
    sys.exit(code)
