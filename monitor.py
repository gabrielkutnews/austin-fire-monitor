#!/usr/bin/env python3
"""Austin Fire + TCEQ Spills + r/Austin -> Slack monitor, filtered for news value.

Three sources polled by one GitHub Actions cron job (every ~5 min), DMing a
list of Slack users via a bot token (chat.postMessage). No LLM involved: each
run is a few small API calls, with delta fetches only when data changed.

Source 1 — Austin Real-Time Fire Incidents (wpu4-x69d):
  - New incidents DM only when issue_reported starts with one of
    alert_prefixes (structure fires, rescues, hazmat, aircraft, Pri 1
    traffic, ...).
  - Anything else is logged as "suppressed" but tracked; if it is still
    ACTIVE after escalation_minutes it DMs anyway (routine calls archive in
    ~20 min median, so a long-running one means a real sustained response).
  - Status changes DM only for incidents that were alerted, with duration.

Source 2 — TCEQ Emergency Response Spills (data.texas.gov xagr-a3x2):
  - DM when a spill is local (loc_cnty_name in tceq_counties OR
    near_city_name in tceq_cities) OR very large (amt_num/uom_cd exceeds
    tceq_thresholds). Each spill alerts once (tceq_seen dedupe) no matter how
    often TCEQ edits the row. TCEQ bulk-touches tens of thousands of old rows
    (measured 18k in one day), so the delta fetch is server-side restricted
    to rcvd_dt within tceq_recent_days — old re-touched rows are never even
    downloaded. Non-matching recent spills get a "tceq suppressed" log line.

Source 3 — r/Austin (Reddit OAuth, app-only):
  - 🔴 keyword alert when a new post's title/body matches reddit_keywords;
    📈 trending alert when any recent post crosses reddit_score_threshold.
  - Needs REDDIT_CLIENT_ID/SECRET (GitHub Secrets or env); absent -> skipped.
    Fail-isolated like TCEQ: Reddit errors never block fire/spill alerts.

The bot token is read from the macOS Keychain (service 'austin-fire-monitor',
account 'slack-bot-token'), falling back to slack_bot_token in config.json.
Recipients come from slack_user_ids in config.json (legacy slack_user_id
string also accepted). See README.md.

Usage:
    monitor.py            normal run (posts to Slack)
    monitor.py --dry-run  print the would-be Slack message instead of posting
                          (state still advances, as if the post succeeded)

Exit codes: 0 ok/no-change, 1 network/Slack error (state untouched, retried
next run), 2 bot token / recipients not configured. A TCEQ outage alone never
blocks fire alerts: it is logged and retried, fire processing continues.
"""

import base64
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

FIRE_API = "https://data.austintexas.gov/resource/wpu4-x69d.json"
FIRE_PAGE = "https://data.austintexas.gov/d/wpu4-x69d"
TCEQ_API = "https://data.texas.gov/resource/xagr-a3x2.json"
DIR = Path(__file__).resolve().parent
CONFIG_FILE = DIR / "config.json"
STATE_FILE = DIR / "state.json"
LOCAL_TZ = ZoneInfo("America/Chicago")
MAX_LINES = 20                       # cap incident lines per Slack message
PRUNE_AFTER = timedelta(hours=24)    # drop non-ACTIVE fire incidents after this
TCEQ_SEEN_PRUNE = timedelta(days=30)  # must exceed tceq_recent_days

DEFAULT_PREFIXES = ["BOX", "BRUSH", "ATTACK", "ALERT", "WRESQT", "RESQT",
                    "HMTF", "HMI", "HMCLAN", "FLOOD",
                    "Traffic Injury Pri 1"]
DEFAULT_ESCALATION_MIN = 45
# Categories never worth a "still ACTIVE after N min" escalation (low news value);
# prefix-matched against issue_reported. Escalation still fires for everything else
# (e.g. GRASS/ELEC fires, Hazardous Condition).
DEFAULT_ESCALATION_EXCLUDE = ["ODOR", "RESQV", "Traffic Injury Pri 2 Rollover",
                              "ALARM", "ALARMM", "ALARMH", "BWP", "SMOKE",
                              "Traffic Injury Pri 2", "AUTO", "TRASH"]
DEFAULT_TCEQ_COUNTIES = ["TRAVIS", "WILLIAMSON", "HAYS", "BASTROP", "CALDWELL"]
DEFAULT_TCEQ_CITIES = ["AUSTIN"]
DEFAULT_TCEQ_THRESHOLDS = {"GALLONS": 1000, "BARRELS": 50, "POUNDS": 5000}
DEFAULT_TCEQ_RECENT_DAYS = 14

REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_OAUTH = "https://oauth.reddit.com"
DEFAULT_REDDIT_SUBREDDIT = "Austin"
DEFAULT_REDDIT_KEYWORDS = [
    "fire", "wildfire", "crash", "collision", "wreck", "shooting", "shooter",
    "active shooter", "stabbing", "police", "apd", "swat", "standoff",
    "evacuate", "evacuation", "flood", "flooding", "low water crossing",
    "tornado", "severe weather", "boil water", "power outage", "outage",
    "explosion", "hazmat", "amber alert", "silver alert", "protest",
    "closure", "i-35", "mopac", "fatal", "homicide", "missing person"]
DEFAULT_REDDIT_SCORE_THRESHOLD = 50
DEFAULT_REDDIT_TRENDING_HOURS = 24
DEFAULT_REDDIT_SEEN_PRUNE_DAYS = 3

FIRE_FIELDS = ("traffic_report_id,published_date,issue_reported,address,"
               "latitude,longitude,traffic_report_status,"
               "traffic_report_status_date_time")
TCEQ_FIELDS = ("incid_track_num,rcvd_dt,material_name,amt_num,uom_cd,"
               "loc_cnty_name,near_city_name,lat_dec_coord_num,"
               "long_dec_coord_num")

UNIT_SYNONYMS = {"GAL": "GALLONS", "GALLON": "GALLONS", "GALLONS": "GALLONS",
                 "BBL": "BARRELS", "BBLS": "BARRELS", "BARREL": "BARRELS",
                 "BARRELS": "BARRELS",
                 "LB": "POUNDS", "LBS": "POUNDS", "POUND": "POUNDS",
                 "POUNDS": "POUNDS"}

API_ERRORS = (urllib.error.URLError, OSError, ValueError, LookupError)


def log(msg):
    print(datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def api_get(url, params):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "austin-fire-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(iso):
    base = iso.rstrip("Z").split(".")[0]
    return datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def fmt_local_time(iso_utc):
    """'2026-06-10T20:09:32.000Z' -> '3:09 PM' Austin time."""
    return parse_utc(iso_utc).astimezone(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")


def fmt_duration(minutes):
    minutes = int(minutes)
    if minutes < 120:
        return "{} min".format(minutes)
    return "{}h {:02d}m".format(minutes // 60, minutes % 60)


def slack_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def maps_link(label, lat, lon):
    label = slack_escape(label)
    if lat and lon:
        return "<https://www.google.com/maps?q={},{}|{}>".format(lat, lon, label)
    return label


def place(item):
    """Maps-linked address; works on fire API rows and state entries alike."""
    return maps_link(item.get("address") or "unknown location",
                     item.get("latitude"), item.get("longitude"))


def slack_api(method, payload, token):
    """Call a Slack Web API method; raise on transport or ok:false errors."""
    req = urllib.request.Request(
        "https://slack.com/api/" + method,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": "Bearer " + token,
                 "User-Agent": "austin-fire-monitor/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        raise OSError("Slack {} error: {}".format(method, result.get("error", "unknown")))
    return result


def post_to_slack(cfg, text, dry_run):
    if dry_run:
        log("dry-run, message would be:\n" + text)
        return
    # Posting straight to each member ID DMs them; needs only chat:write
    # (conversations.open would additionally require im:write).
    for uid in cfg["user_ids"]:
        slack_api("chat.postMessage",
                  {"channel": uid, "text": text, "unfurl_links": False},
                  cfg["token"])


def keychain_token():
    """Slack token from the macOS Keychain (service austin-fire-monitor)."""
    try:
        proc = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-s", "austin-fire-monitor", "-a", "slack-bot-token", "-w"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def load_config():
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        cfg = {}
    # On GitHub Actions the whole secret set is injected as one JSON env var
    # (ALL_SECRETS = ${{ toJSON(secrets) }}). Parse it once; absent locally.
    secrets = {}
    raw = os.environ.get("ALL_SECRETS", "")
    if raw:
        try:
            secrets = json.loads(raw)
        except ValueError:
            secrets = {}
    # Token: secret first, then Keychain, then config.json (Keychain/config are
    # the local fallbacks — this Mac's login keychain needs a manual unlock).
    token = (secrets.get("SLACK_BOT_TOKEN", "").strip()
             or keychain_token() or cfg.get("slack_bot_token", ""))
    # Recipients: SLACK_USER_IDS secret (comma/space/newline-separated), else
    # the config.json list (legacy single slack_user_id still honored).
    ids_raw = secrets.get("SLACK_USER_IDS", "")
    if ids_raw:
        ids = re.split(r"[,\s]+", ids_raw.strip())
    else:
        ids = cfg.get("slack_user_ids")
        if ids is None:
            ids = [cfg.get("slack_user_id", "")]
        if isinstance(ids, str):
            ids = [ids]
    user_ids = [str(u).strip() for u in ids if str(u).strip().startswith("U")]
    thresholds = cfg.get("tceq_thresholds", DEFAULT_TCEQ_THRESHOLDS)
    # Reddit creds: GitHub secret first, then a plain env var (so a local
    # `REDDIT_CLIENT_ID=… python3 monitor.py` can live-test without putting
    # any secret into the PUBLIC config.json). Never read creds from cfg.
    def secret_or_env(name):
        return (secrets.get(name, "") or os.environ.get(name, "")).strip()
    reddit_id = secret_or_env("REDDIT_CLIENT_ID")
    reddit_secret = secret_or_env("REDDIT_CLIENT_SECRET")
    reddit_user = secret_or_env("REDDIT_USERNAME") or "austin-news-bot"
    return {
        "token": token,
        "user_ids": user_ids,
        "prefixes": list(cfg.get("alert_prefixes", DEFAULT_PREFIXES)),
        "escalation_min": int(cfg.get("escalation_minutes", DEFAULT_ESCALATION_MIN)),
        "escalation_exclude_prefixes": list(cfg.get("escalation_exclude_prefixes",
                                                    DEFAULT_ESCALATION_EXCLUDE)),
        "tceq_counties": {str(c).strip().upper()
                          for c in cfg.get("tceq_counties", DEFAULT_TCEQ_COUNTIES)},
        "tceq_cities": {str(c).strip().upper()
                        for c in cfg.get("tceq_cities", DEFAULT_TCEQ_CITIES)},
        "tceq_thresholds": {str(k).strip().upper(): float(v)
                            for k, v in thresholds.items()},
        "tceq_recent_days": int(cfg.get("tceq_recent_days", DEFAULT_TCEQ_RECENT_DAYS)),
        "reddit_id": reddit_id,
        "reddit_secret": reddit_secret,
        "reddit_user": reddit_user,
        "reddit_enabled": bool(reddit_id and reddit_secret),
        "reddit_subreddit": str(cfg.get("reddit_subreddit", DEFAULT_REDDIT_SUBREDDIT)),
        "reddit_keywords": list(cfg.get("reddit_keywords", DEFAULT_REDDIT_KEYWORDS)),
        "reddit_score_threshold": int(cfg.get("reddit_score_threshold",
                                              DEFAULT_REDDIT_SCORE_THRESHOLD)),
        "reddit_trending_hours": int(cfg.get("reddit_trending_hours",
                                             DEFAULT_REDDIT_TRENDING_HOURS)),
        "reddit_seen_prune_days": int(cfg.get("reddit_seen_prune_days",
                                              DEFAULT_REDDIT_SEEN_PRUNE_DAYS)),
        "valid": token.startswith("xoxb-") and len(user_ids) > 0,
    }


def is_alertable(issue, prefixes):
    return any(issue.startswith(p) for p in prefixes)


def entry_from_row(row, now, alerted):
    return {"status": row.get("traffic_report_status", "?"),
            "last_seen": now,
            "published": row.get("published_date"),
            "issue": row.get("issue_reported"),
            "address": row.get("address"),
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
            "alerted": alerted}


def save_state(watermark, incidents, tceq_watermark, tceq_seen,
               reddit_kw_watermark=None, reddit_trending=None,
               reddit_prune_days=DEFAULT_REDDIT_SEEN_PRUNE_DAYS):
    now = datetime.now(timezone.utc)
    cutoff = (now - PRUNE_AFTER).strftime("%Y-%m-%dT%H:%M:%SZ")
    incidents = {k: v for k, v in incidents.items()
                 if v.get("status") == "ACTIVE" or v.get("last_seen", "") >= cutoff}
    seen_cutoff = (now - TCEQ_SEEN_PRUNE).strftime("%Y-%m-%dT%H:%M:%SZ")
    tceq_seen = {k: v for k, v in tceq_seen.items() if v >= seen_cutoff}
    reddit_trending = reddit_trending or {}
    r_cutoff = (now - timedelta(days=reddit_prune_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reddit_trending = {k: v for k, v in reddit_trending.items() if v >= r_cutoff}
    STATE_FILE.write_text(json.dumps({"watermark": watermark,
                                      "incidents": incidents,
                                      "tceq_watermark": tceq_watermark,
                                      "tceq_seen": tceq_seen,
                                      "reddit_kw_watermark": reddit_kw_watermark,
                                      "reddit_trending": reddit_trending}))


# ---------------------------------------------------------------- TCEQ spills

def spill_amount(amt, uom):
    """('1,500', 'GALLONS') -> (1500.0, 'GALLONS'); unparseable -> (None, None).

    The number regex tolerates commas/stray text; the unit is normalized via
    UNIT_SYNONYMS (rate units like TPERYEAR and unknown units -> unit None).
    """
    m = re.search(r"[\d,]*\.?\d+", str(amt or ""))
    if not m:
        return None, None
    try:
        value = float(m.group().replace(",", ""))
    except ValueError:
        return None, None
    unit = UNIT_SYNONYMS.get(re.sub(r"[^A-Z]", "", str(uom or "").upper()))
    return value, unit


def is_big_spill(row, thresholds):
    value, unit = spill_amount(row.get("amt_num"), row.get("uom_cd"))
    if value is None or unit is None:
        return False
    limit = thresholds.get(unit)
    return limit is not None and value > limit


def tceq_matches(row, cfg):
    county = (row.get("loc_cnty_name") or "").strip().upper()
    city = (row.get("near_city_name") or "").strip().upper()
    local = county in cfg["tceq_counties"] or city in cfg["tceq_cities"]
    return local or is_big_spill(row, cfg["tceq_thresholds"])


def fmt_spill_line(row):
    material = slack_escape((row.get("material_name") or "Unknown material").strip())
    value, _ = spill_amount(row.get("amt_num"), row.get("uom_cd"))
    uom = (row.get("uom_cd") or "").strip().lower()
    if value and uom:
        amount = "{:,.0f} {}".format(value, uom)
    elif value:
        amount = "{:,.0f} (unit unknown)".format(value)
    else:
        amount = "amount unknown"
    city = (row.get("near_city_name") or "").strip().title()
    county = (row.get("loc_cnty_name") or "").strip().title()
    loc = ", ".join(p for p in [city, county + " Co." if county else ""] if p) \
        or "location unknown"
    loc = maps_link(loc, row.get("lat_dec_coord_num"), row.get("long_dec_coord_num"))
    when = ""
    if row.get("rcvd_dt"):
        try:
            dt = parse_utc(row["rcvd_dt"])
            when = " — rcvd {} {}".format(dt.strftime("%b"), dt.day)
        except ValueError:
            pass
    return "🛢️ *{}* — {} — {}{}".format(material, amount, loc, when)


def process_tceq(state, cfg, now_dt):
    """Poll the TCEQ spills source. Returns (dm_lines, suppressed, changed);
    mutates state's tceq_watermark/tceq_seen in memory only (caller saves).
    Never raises: a TCEQ outage is logged and retried, fire alerts continue.
    """
    try:
        latest = api_get(TCEQ_API, {"$select": "max(:updated_at)"})[0]["max_updated_at"]
    except API_ERRORS as e:
        log("tceq ERROR probe failed, will retry next run: {}".format(e))
        return [], 0, False

    seen = state.get("tceq_seen") or {}
    state["tceq_seen"] = seen
    watermark = state.get("tceq_watermark")
    if watermark is None:
        # First TCEQ cycle: start from now, no historical backfill.
        state["tceq_watermark"] = latest
        log("tceq: seeded watermark {}".format(latest))
        return [], 0, True
    if latest <= watermark:
        return [], 0, False

    # rcvd_dt guard keeps TCEQ's bulk re-touches of old rows (measured 18k/day)
    # from ever being downloaded; only recent spills are eligible to alert.
    rcvd_cutoff = (now_dt - timedelta(days=cfg["tceq_recent_days"])
                   ).strftime("%Y-%m-%dT00:00:00.000")
    try:
        rows = api_get(TCEQ_API, {
            "$select": TCEQ_FIELDS,
            "$where": ":updated_at > '{}' AND rcvd_dt > '{}'".format(
                watermark, rcvd_cutoff),
            "$order": ":updated_at",
            "$limit": "1000"})
    except API_ERRORS as e:
        log("tceq ERROR fetch failed, will retry next run: {}".format(e))
        return [], 0, False

    now_iso = utc_now_iso()
    dm, suppressed = [], 0
    for row in rows:
        rid = row.get("incid_track_num")
        if not rid or rid in seen:
            continue  # alert once per spill; later TCEQ edits don't re-alert
        seen[rid] = now_iso
        if tceq_matches(row, cfg):
            dm.append(fmt_spill_line(row))
        else:
            log("tceq suppressed: {} — {}, {}".format(
                row.get("material_name") or "?",
                (row.get("near_city_name") or "?").title(),
                (row.get("loc_cnty_name") or "?").title()))
            suppressed += 1
    state["tceq_watermark"] = latest
    return dm, suppressed, True


# --------------------------------------------------------------- reddit pulse

def reddit_user_agent(cfg):
    return "austin-pulse-monitor/1.0 (by /u/{})".format(cfg["reddit_user"])


def reddit_token(cfg):
    """App-only OAuth bearer for public read access; "" on any failure."""
    auth = base64.b64encode(
        "{}:{}".format(cfg["reddit_id"], cfg["reddit_secret"]).encode()).decode()
    req = urllib.request.Request(
        REDDIT_TOKEN_URL,
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={"Authorization": "Basic " + auth,
                 "User-Agent": reddit_user_agent(cfg)})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp).get("access_token", "")


def reddit_fetch_new(token, cfg):
    """Newest ~100 posts from /r/<sub>/new -> list of post 'data' dicts."""
    req = urllib.request.Request(
        "{}/r/{}/new?limit=100".format(REDDIT_OAUTH, cfg["reddit_subreddit"]),
        headers={"Authorization": "bearer " + token,
                 "User-Agent": reddit_user_agent(cfg)})
    with urllib.request.urlopen(req, timeout=30) as resp:
        children = json.load(resp).get("data", {}).get("children", [])
    return [c["data"] for c in children if c.get("data")]


def reddit_fetch_new_rss(cfg):
    """Credential-free fallback: parse the public Atom feed into the SAME post
    shape as reddit_fetch_new, but with score=None (RSS carries no upvotes, so
    the trending pass becomes a no-op and only 🔴 keyword alerts fire)."""
    ns = {"a": "http://www.w3.org/2005/Atom"}
    sub = cfg["reddit_subreddit"]
    req = urllib.request.Request(
        "https://www.reddit.com/r/{}/new/.rss?limit=100".format(sub),
        headers={"User-Agent": reddit_user_agent(cfg)})
    with urllib.request.urlopen(req, timeout=30) as resp:
        root = ET.fromstring(resp.read())
    posts = []
    for e in root.findall("a:entry", ns):
        def txt(tag):
            node = e.find("a:" + tag, ns)
            return node.text if node is not None and node.text else ""
        fullname = txt("id")                       # e.g. "t3_1u6r92i"
        pid = fullname.split("_", 1)[1] if "_" in fullname else fullname
        link_el = e.find("a:link", ns)
        permalink = link_el.get("href") if link_el is not None else ""
        cat = e.find("a:category", ns)
        flair = cat.get("label") if cat is not None else None
        if flair == "r/" + sub:                    # RSS uses this when no real flair
            flair = None
        try:
            created = datetime.fromisoformat(txt("published")).timestamp()
        except ValueError:
            created = 0
        body = html.unescape(re.sub(r"<[^>]+>", " ", txt("content")))
        posts.append({"id": pid, "title": txt("title"), "selftext": body,
                      "score": None, "num_comments": 0, "permalink": permalink,
                      "created_utc": created, "link_flair_text": flair})
    return posts


def reddit_keyword_hit(text, keywords):
    """Case-insensitive word-boundary match; phrases match literally."""
    low = (text or "").lower()
    return next((k for k in keywords
                 if re.search(r"\b" + re.escape(k.lower()) + r"\b", low)), None)


def fmt_reddit_line(emoji, label, post):
    title = slack_escape((post.get("title") or "(untitled)").strip())
    permalink = post.get("permalink") or ""
    link = permalink if permalink.startswith("http") else "https://reddit.com" + permalink
    flair = post.get("link_flair_text")
    tag = " [{}]".format(slack_escape(flair)) if flair else ""
    score = post.get("score")
    stats = ""  # RSS has no scores -> score is None -> omit the ↑/💬 segment
    if score is not None:
        stats = " — {}↑ {}💬".format(score, post.get("num_comments", 0))
    return "{} {}: *{}*{}{} — <{}|link>".format(emoji, label, title, tag, stats, link)


def reddit_classify(posts, state, cfg, now_dt):
    """Pure: decide alerts from a post list, mutate reddit_* state in place.

    Returns (dm_lines, changed). 'changed' is True when reddit state must be
    persisted (first-run seed or any alert). Low-churn: the keyword watermark
    advances only on a keyword alert, and the trending set only grows on a
    real threshold crossing — quiet cycles write nothing.
    """
    threshold = cfg["reddit_score_threshold"]
    label = "r/" + cfg["reddit_subreddit"]
    created = [p.get("created_utc") or 0 for p in posts]

    # First run: seed, no backfill (mirrors fire/TCEQ first_run).
    if state.get("reddit_kw_watermark") is None:
        state["reddit_kw_watermark"] = max(created) if created else 0
        now_iso = utc_now_iso()
        state["reddit_trending"] = {
            (p.get("id") or ""): now_iso for p in posts
            if (p.get("score") or 0) >= threshold and p.get("id")}
        log("reddit: seeded {} post(s), watermark {}".format(
            len(posts), state["reddit_kw_watermark"]))
        return [], True

    watermark = state["reddit_kw_watermark"]
    trending = state.setdefault("reddit_trending", {})
    now_iso = utc_now_iso()
    dm, alerted_ids, new_watermark = [], set(), watermark

    # Keyword pass: only posts newer than the watermark.
    for p in posts:
        pid, ts = p.get("id"), (p.get("created_utc") or 0)
        if not pid or ts <= watermark:
            continue
        hit = reddit_keyword_hit(
            "{} {}".format(p.get("title", ""), p.get("selftext", "")),
            cfg["reddit_keywords"])
        if hit:
            dm.append(fmt_reddit_line("🔴", label, p))
            alerted_ids.add(pid)
            new_watermark = max(new_watermark, ts)

    # Trending pass: any recent post over threshold not already alerted.
    horizon = (now_dt - timedelta(hours=cfg["reddit_trending_hours"])).timestamp()
    for p in posts:
        pid, ts = p.get("id"), (p.get("created_utc") or 0)
        if (not pid or pid in trending or pid in alerted_ids
                or ts < horizon or (p.get("score") or 0) < threshold):
            continue
        dm.append(fmt_reddit_line("📈", label + " trending", p))
        trending[pid] = now_iso

    # Record keyword-alerted ids in the trending set too, so a later climb
    # doesn't re-alert the same post as 📈.
    for pid in alerted_ids:
        trending.setdefault(pid, now_iso)

    state["reddit_kw_watermark"] = new_watermark
    return dm, bool(dm)


def process_reddit(state, cfg, now_dt):
    """Fetch + classify r/<sub>. With OAuth creds -> full feed (keywords +
    trending). Without -> credential-free RSS (keywords only; no scores).
    Fail-isolated like TCEQ: never raises, never exits, so Reddit problems
    can't block fire/TCEQ alerts."""
    mode = "oauth" if cfg["reddit_enabled"] else "rss"
    try:
        if cfg["reddit_enabled"]:
            token = reddit_token(cfg)
            if not token:
                log("reddit ERROR: no access token, will retry next run")
                return [], False
            posts = reddit_fetch_new(token, cfg)
        else:
            posts = reddit_fetch_new_rss(cfg)
    except (API_ERRORS + (ET.ParseError,)) as e:
        log("reddit ({}) ERROR fetch failed, will retry next run: {}".format(mode, e))
        return [], False
    return reddit_classify(posts, state, cfg, now_dt)


# ------------------------------------------------------------- fire incidents

def first_run(latest, cfg, dry_run):
    """Seed state from current data and announce; no historical backfill."""
    active = api_get(FIRE_API, {"$select": FIRE_FIELDS,
                                "$where": "traffic_report_status = 'ACTIVE'",
                                "$limit": "1000"})
    now = utc_now_iso()
    incidents = {r["traffic_report_id"]: entry_from_row(r, now, False)
                 for r in active if r.get("traffic_report_id")}
    text = ("✅ Austin fire-incident monitor started — {} active incident(s) right now. "
            "DMs are filtered for news value (🚨 alert categories, ⏱️ select incidents "
            "still active >{} min, 🛢️ Austin-area or very large TCEQ spills, 🔴 r/Austin "
            "keywords); the rest is logged. Watching <{}|the dataset> every 5 minutes."
            ).format(len(incidents), cfg["escalation_min"], FIRE_PAGE)
    post_to_slack(cfg, text, dry_run)
    save_state(latest, incidents, None, {})
    log("first run: seeded watermark {}, {} active incident(s)".format(latest, len(incidents)))


def main():
    dry_run = "--dry-run" in sys.argv[1:]

    cfg = load_config()
    if not cfg["valid"] and not dry_run:
        log("ERROR not configured: need Slack token in Keychain (service "
            "'austin-fire-monitor', account 'slack-bot-token') or in config.json, "
            "plus slack_user_ids in config.json — see README.md")
        sys.exit(2)

    try:
        latest = api_get(FIRE_API, {"$select": "max(:updated_at)"})[0]["max_updated_at"]
    except API_ERRORS as e:
        log("ERROR probe failed: {}".format(e))
        sys.exit(1)

    rows = []
    try:
        if not STATE_FILE.exists():
            first_run(latest, cfg, dry_run)
            return
        state = json.loads(STATE_FILE.read_text())
        watermark = state["watermark"]
        if latest > watermark:
            rows = api_get(FIRE_API, {"$select": FIRE_FIELDS + ",:updated_at",
                                      "$where": ":updated_at > '{}'".format(watermark),
                                      "$order": ":updated_at",
                                      "$limit": "1000"})
    except API_ERRORS as e:
        log("ERROR fetch failed, will retry next run: {}".format(e))
        sys.exit(1)

    incidents = state["incidents"]
    now = utc_now_iso()
    now_dt = datetime.now(timezone.utc)
    dm = []
    suppressed = 0

    for row in rows:
        rid = row.get("traffic_report_id")
        if not rid:
            continue
        status = row.get("traffic_report_status", "?")
        issue_raw = row.get("issue_reported") or "Unknown issue"
        issue = slack_escape(issue_raw)
        known = incidents.get(rid)

        if known is None:
            alertable = is_alertable(issue_raw, cfg["prefixes"])
            if alertable:
                when = (" ({})".format(fmt_local_time(row["published_date"]))
                        if row.get("published_date") else "")
                dm.append("🚨 *{}* — {} — {}{}".format(issue, place(row), status, when))
            else:
                log("suppressed: {} — {}".format(issue_raw, row.get("address") or "?"))
                suppressed += 1
            incidents[rid] = entry_from_row(row, now, alertable)
        else:
            prev_status = known.get("status")
            entry = entry_from_row(row, now, known.get("alerted", False))
            if prev_status != status:
                # Closures ("case ended") are no longer DM'd — log only.
                log("status change: {} — {} -> {}".format(issue_raw, prev_status, status))
            incidents[rid] = entry

    # Escalation pass: runs every cycle, including no-change cycles, because
    # an incident escalates by time passing, not by its row being touched.
    for entry in incidents.values():
        if (entry.get("status") == "ACTIVE" and not entry.get("alerted")
                and entry.get("published")
                and not is_alertable(entry.get("issue") or "",
                                     cfg["escalation_exclude_prefixes"])):
            try:
                pub = parse_utc(entry["published"])
            except ValueError:
                continue
            age_min = (now_dt - pub).total_seconds() / 60
            if age_min >= cfg["escalation_min"]:
                dm.append("⏱️ *{}* — {} — still ACTIVE after {}".format(
                    slack_escape(entry.get("issue") or "Unknown issue"),
                    place(entry), fmt_duration(age_min)))
                entry["alerted"] = True

    tceq_dm, tceq_suppressed, tceq_changed = process_tceq(state, cfg, now_dt)
    dm.extend(tceq_dm)

    reddit_dm, reddit_changed = process_reddit(state, cfg, now_dt)
    dm.extend(reddit_dm)

    if not rows and not dm and not tceq_changed and not reddit_changed:
        log("no change (watermark {})".format(watermark))
        return

    if dm:
        shown = dm[:MAX_LINES]
        if len(dm) > MAX_LINES:
            shown.append("…and {} more — see <{}|the dataset>".format(
                len(dm) - MAX_LINES, FIRE_PAGE))
        try:
            post_to_slack(cfg, "\n".join(shown), dry_run)
        except (urllib.error.URLError, OSError) as e:
            log("ERROR Slack post failed, will retry next run: {}".format(e))
            sys.exit(1)  # state NOT saved; delta, escalations and tceq retry

    new_watermark = max([watermark, latest]
                        + [r[":updated_at"] for r in rows if r.get(":updated_at")])
    save_state(new_watermark, incidents,
               state.get("tceq_watermark"), state.get("tceq_seen") or {},
               state.get("reddit_kw_watermark"), state.get("reddit_trending") or {},
               cfg["reddit_seen_prune_days"])
    log("fire {} row(s) + tceq {} spill(s) + reddit {} post(s): {} DM line(s), "
        "{} suppressed; watermark -> {}".format(
            len(rows), len(tceq_dm) + tceq_suppressed, len(reddit_dm),
            len(dm), suppressed + tceq_suppressed, new_watermark))


if __name__ == "__main__":
    main()
