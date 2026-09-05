#!/usr/bin/env python3
"""
XD Downloader Bot — Telegram video downloader.
Engine: yt-dlp (the same engine ReClip uses) + ffmpeg.

Flow:
  1. user sends a link  -> bot asks for quality (inline keyboard)
  2. user picks quality -> yt-dlp downloads -> bot delivers video/audio

Special case — X/Twitter multi-photo posts:
  photos are fetched via the fxtwitter API and delivered as one
  Telegram media group (album) — no quality picker needed.

Supports: YouTube, X/Twitter, Instagram, TikTok, Reddit, Facebook,
Vimeo, SoundCloud + 1000+ more sites (anything yt-dlp handles).

Access control: only ALLOWED_USER_ID (the owner) can use it.
"""

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import urllib.parse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Config ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("XD_BOT_TOKEN", "8903556855:CHANGE_ME")
# Only this user may use the bot (owner's Telegram user id, NOT channel id).
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "5669842121"))
DOWNLOAD_DIR = os.environ.get("XD_DOWNLOAD_DIR", "/root/xd-downloader/downloads")
TG_FILE_LIMIT = 50 * 1024 * 1024  # bot API hard limit for video upload
DOC_LIMIT = 50 * 1024 * 1024      # same limit for document
TG_ALBUM_LIMIT = 10               # Telegram max items per media group

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Resolve yt-dlp binary: PATH first (dev shells), then known venv installs.
YTDLP = shutil.which("yt-dlp") or "/usr/local/lib/hermes-agent/venv/bin/yt-dlp"
if not os.path.isfile(YTDLP):
    YTDLP = shutil.which("yt-dlp") or "yt-dlp"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("xd-bot")

URL_RE = re.compile(r"https?://\S+")
X_URL_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/[^/]+/status/(\d+)", re.IGNORECASE
)
FXTWITTER_API = "https://api.fxtwitter.com"
VXTWITTER_API = "https://api.vxtwitter.com"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
ACTIVE = set()               # chat ids currently downloading
PENDING_URL = {}             # chat_id -> {"url": ..., "msg_id": ...} awaiting quality choice

# Quality presets: label -> yt-dlp -f selector (height caps keep under 50MB)
QUALITIES = {
    "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "Best":  "bestvideo+bestaudio/best",
}


# ── yt-dlp runner (blocking; runs in thread via asyncio.to_thread) ──────
def run_ytdlp(url: str, quality: str, audio_only: bool) -> tuple[str | None, str | None]:
    out_template = os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s")
    cmd = [
        YTDLP,
        "--no-playlist",
        "--no-warnings",
        "--restrict-filenames",
        "--socket-timeout", "30",
        "-o", out_template,
        "--max-filesize", "1900M",
    ]
    # Cookies file (e.g. cookies.txt from "Get cookies.txt LOCALLY" extension)
    # fixes YouTube/Instagram bot-checks on datacenter IPs.
    cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    if os.path.isfile(cookies_file):
        cmd += ["--cookies", cookies_file]
    if audio_only:
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "5"]
    else:
        fmt = QUALITIES.get(quality, QUALITIES["720p"])
        cmd += ["-f", fmt, "--merge-output-format", "mp4"]
    cmd.append(url)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None, "دانلود بیش از حد طول کشید (timeout)"
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        return None, (err[-1][:300] if err else "خطای نامشخص yt-dlp")

    # newest file in download dir, touched within the last 15 min
    fresh = [
        f for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*"))
        if time.time() - os.path.getmtime(f) < 900
    ]
    if not fresh:
        return None, "فایل خروجی پیدا نشد"
    return max(fresh, key=os.path.getmtime), None


# ── X/Twitter multi-photo support (fxtwitter API, no auth) ─────────────
def fetch_x_tweet(tweet_id: str) -> dict | None:
    """Fetch tweet metadata from fxtwitter (fallback: vxtwitter). None on failure."""
    # fxtwitter/vxtwitter path shape: /:user/status/:id — user segment is
    # ignored by the API, but it must be present for the route to resolve.
    path = f"/i/status/{tweet_id}"
    for api in (FXTWITTER_API, VXTWITTER_API):
        try:
            req = urllib.request.Request(
                f"{api}{path}", headers={"User-Agent": BROWSER_UA}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except Exception as e:
            log.warning("x api %s failed for %s: %s", api, tweet_id, e)
    return None


def x_media_urls(tweet_id: str) -> tuple[list[str], int]:
    """Return (photo_urls, video_count) for an X post. Photos keep ?name=orig."""
    data = fetch_x_tweet(tweet_id)
    if not data:
        return [], 0
    tweet = (data or {}).get("tweet") or {}
    media = tweet.get("media") or {}
    photos = [p.get("url") for p in media.get("photos", []) if p.get("url")]
    videos = media.get("videos") or []
    return photos, len(videos)


def download_x_photos(urls: list[str]) -> tuple[list[str], int]:
    """Download photos into a temp dir. Returns (local_paths, failed_count)."""
    tmpdir = tempfile.mkdtemp(prefix="xd-x-", dir=DOWNLOAD_DIR)
    paths, failed = [], 0
    for i, u in enumerate(urls):
        dest = os.path.join(tmpdir, f"{i + 1:02d}.jpg")
        try:
            req = urllib.request.Request(u, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as fh:
                fh.write(resp.read())
            if os.path.getsize(dest) > 0:
                paths.append(dest)
            else:
                failed += 1
        except Exception as e:
            log.warning("x photo %s failed: %s", u, e)
            failed += 1
    return paths, failed


def cleanup_dir(path: str):
    shutil.rmtree(path, ignore_errors=True)


# ── Instagram profile picture support (no auth, multi-source) ───────────
IG_USER_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)
IG_STORY_RE = re.compile(r"instagram\.com/stories/", re.IGNORECASE)
IG_P_RE = re.compile(r"instagram\.com/(p|reel|tv)/", re.IGNORECASE)


def _http_get(url: str, headers: dict | None = None, timeout: int = 25) -> bytes:
    """Blocking GET returning raw bytes. Raises on HTTP errors."""
    h = {"User-Agent": BROWSER_UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _ig_profile_from_instagram_api(username: str) -> str | None:
    """Try Instagram's own web profile API (best quality: profile_pic_url_hd)."""
    app_ids = ("936619743392459", "1217981644879628")
    for app_id in app_ids:
        try:
            data = _http_get(
                "https://www.instagram.com/api/v1/users/web_profile_info/?"
                + urllib.parse.urlencode({"username": username}),
                headers={
                    "x-ig-app-id": app_id,
                    "x-requested-with": "XMLHttpRequest",
                    "accept": "*/*",
                    "referer": f"https://www.instagram.com/{username}/",
                },
            )
            user = json.loads(data)["data"]["user"]
            return user.get("profile_pic_url_hd") or user.get("profile_pic_url")
        except Exception as e:
            log.info("ig api %s failed for %s: %s", app_id, username, e)
    return None


def _ig_profile_from_instastatistics(username: str) -> str | None:
    """Fallback: instastatistics embeds the IG CDN profile avatar (needs a real browser via CDP).

    The requested user's own avatar is matched by the page's profile header (h1/h2 text) or the
    img alt text containing the username — never the generic first image on the page.
    """
    if not os.path.isfile("/root/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome"):
        if not (shutil.which("chromium") or shutil.which("google-chrome")):
            return None
    safe_user = json.dumps(username)  # quoted JS string literal
    js_probe = (
        "(async () => {"
        "  const user = " + safe_user + ".toLowerCase().replace(/^@/, '');"
        "  const norm = s => (s || '').toLowerCase().replace(/^@/, '');"
        "  for (let i = 0; i < 12; i++) {"
        "    await new Promise(r => setTimeout(r, 1000));"
        "    const imgs = [...document.querySelectorAll('img')].filter("
        "      i => (i.src || '').includes('api/img'));"
        "    const h1 = document.querySelector('h1, h2, [class*=\"username\"]');"
        "    const uname = norm(h1 && h1.textContent);"
        "    const target = imgs.find(i => norm(i.alt) === user)"
        "      || imgs.find(i => norm(i.alt).includes(user) && user.length > 2)"
        "      || (uname && imgs.find(i => norm(i.alt).includes(uname) && uname.length > 2));"
        "    if (target) {"
        "      const u = new URL(target.src).searchParams.get('url');"
        "      if (u) return JSON.stringify({alt: target.alt || '', url: u});"
        "    }"
        "  }"
        "  return JSON.stringify({err: 'avatar not found for ' + user});"
        "})()"
    )
    try:
        # Use CDP on the persistent local Chrome daemon (port 9222) if present.
        req = urllib.request.Request("http://127.0.0.1:9222/json/version", method="GET")
        urllib.request.urlopen(req, timeout=5)
        return _ig_profile_via_cdp(username, js_probe)
    except Exception:
        return None


def _ig_profile_via_cdp(username: str, js_probe: str) -> str | None:
    """Open the profile stats page in local Chrome (CDP :9222) and extract the CDN avatar URL."""
    import base64

    async def _run():
        import websockets
        req = urllib.request.Request(
            "http://127.0.0.1:9222/json/new?about:blank", method="PUT"
        )
        tab = json.load(urllib.request.urlopen(req, timeout=15))
        pending: dict = {}

        async with websockets.connect(
            tab["webSocketDebuggerUrl"], max_size=80 * 1024 * 1024
        ) as ws:
            mid = 0

            async def reader():
                try:
                    while True:
                        msg = json.loads(await ws.recv())
                        if "id" in msg and msg["id"] in pending:
                            pending.pop(msg["id"]).set_result(msg)
                except Exception:
                    pass

            async def cmd(method, params=None):
                nonlocal mid
                mid += 1
                fut = asyncio.get_event_loop().create_future()
                pending[mid] = fut
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                return await asyncio.wait_for(fut, timeout=30)

            rt = asyncio.create_task(reader())
            await cmd("Page.enable")
            await cmd("Runtime.enable")
            await cmd("Page.navigate", {"url": f"https://instastatistics.com/{username}"})
            await asyncio.sleep(14)
            res = await cmd("Runtime.evaluate", {
                "expression": js_probe, "returnByValue": True, "awaitPromise": True
            })
            rt.cancel()
            return (res.get("result", {}).get("result", {}).get("value") or "NOT_FOUND")

    try:
        val = asyncio.run(_run())
    except Exception as e:
        log.info("ig cdp extract failed for %s: %s", username, e)
        return None
    if not val or not val.startswith("{"):
        return None
    try:
        parsed = json.loads(val)
    except Exception:
        return None
    url = parsed.get("url") or ""
    alt = parsed.get("alt") or ""
    if not url.startswith("http"):
        log.info("ig cdp: no avatar for %s (%s)", username, parsed.get("err") or alt)
        return None
    log.info("ig cdp: matched avatar for %s via alt=%r", username, alt)
    return url


def fetch_ig_profile_pic_url(username: str) -> str | None:
    """Return the profile picture URL for an IG username (HD first)."""
    return _ig_profile_from_instagram_api(username) or _ig_profile_from_instastatistics(
        username
    )


def download_ig_profile_pic(username: str) -> tuple[str | None, str | None]:
    """Download the profile picture. Returns (local_path, error)."""
    try:
        url = fetch_ig_profile_pic_url(username)
        if not url:
            return None, "عکس پروفایل پیدا نشد (پروفایل خصوصی/حذف‌شده یا محدودیت دسترسی)."
        dest = os.path.join(DOWNLOAD_DIR, f"ig_{username}_profile.jpg")
        data = _http_get(url, headers={"User-Agent": BROWSER_UA}, timeout=60)
        if not data.startswith(b"\xff\xd8") and not data.startswith(b"\x89PNG"):
            return None, "فایل دانلودشده عکس معتبر نبود."
        with open(dest, "wb") as fh:
            fh.write(data)
        return dest, None
    except Exception as e:
        log.warning("ig profile pic %s failed: %s", username, e)
        return None, f"خطا: {str(e)[:150]}"


# ── Access control ──────────────────────────────────────────────────────
def is_owner(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ALLOWED_USER_ID


# ── Handlers ────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("🔒 فقط مالک می‌تونه استفاده کنه.")
        return
    await update.message.reply_text(
        "🎬 لینک بفرست، کیفیت رو انتخاب کن، ویدیو تحویل بگیر\n\n"
        "• یوتیوب / X / اینستاگرام / تیک‌تاک / ردیت و ۱۰۰۰+ سایت دیگه\n"
        "• پست‌های چندعکسی X → همهٔ عکسها یه‌جا میاد (بدون انتخاب کیفیت)\n"
        "• /mp3 قبل از لینک = فقط صدا (بدون انتخاب کیفیت)\n"
        "• /dp username یا لینک پروفایل اینستاگرام = عکس پروفایل\n"
        "• سقف تلگرام: ~۵۰MB — کیفیت‌های بالاتر ممکنه جا نشه"
    )


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


async def ask_quality(update: Update, url: str):
    """Show inline keyboard with quality choices, store pending URL."""
    msg = await update.message.reply_text("📥 لینک دریافت شد. کیفیت مورد نظر؟")
    PENDING_URL[update.effective_chat.id] = {"url": url, "msg_id": msg.message_id}
    kb = [
        [
            InlineKeyboardButton("480p", callback_data="q:480"),
            InlineKeyboardButton("720p", callback_data="q:720"),
            InlineKeyboardButton("1080p", callback_data="q:1080"),
            InlineKeyboardButton("🎯 Best", callback_data="q:best"),
        ]
    ]
    await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(kb))


async def on_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text = update.message.text or ""
    url = extract_url(text)
    if not url:
        await update.message.reply_text("لینک معتبر پیدا نکردم. یه URL کامل بفرست.")
        return
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE:
        await update.message.reply_text("یه دانلود در جریانه — صبر کن تموم شه ⏳")
        return

    # Instagram profile URL (no /p/ /reel/ /stories/ path) → send profile picture.
    ig = IG_USER_RE.match(url)
    if ig and not IG_P_RE.search(url) and not IG_STORY_RE.search(url):
        await do_ig_profile(update, ctx, ig.group(1))
        return

    # X/Twitter multi-photo fast path: no quality picker, album straight away.
    xm = X_URL_RE.search(url)
    if xm and not text.strip().startswith("/mp3"):
        await do_x_photos(update, ctx, xm.group(1))
        return

    await ask_quality(update, url)


async def do_ig_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE, username: str):
    """Download and send an Instagram user's profile picture."""
    chat_id = update.effective_chat.id
    ACTIVE.add(chat_id)
    wait_msg = None
    path = None
    try:
        wait_msg = await update.message.reply_text(
            f"👤 @ {username} — ⏳ در حال گرفتن عکس پروفایل..."
        )
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        path, err = await asyncio.to_thread(download_ig_profile_pic, username)
        if err:
            await wait_msg.edit_text(f"❌ {err}")
            return
        with open(path, "rb") as fh:
            await update.effective_chat.send_photo(
                photo=fh, caption=f"👤 @{username} — عکس پروفایل"
            )
        try:
            await wait_msg.delete()
        except Exception:
            pass
    except Exception as e:
        log.exception("ig profile failed")
        try:
            if wait_msg:
                await wait_msg.edit_text(f"❌ خطا: {str(e)[:200]}")
        except Exception:
            pass
    finally:
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except Exception:
                pass
        ACTIVE.discard(chat_id)


async def on_dp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/dp <username> یا /dp <لینک پروفایل> → عکس پروفایل اینستاگرام"""
    if not is_owner(update):
        return
    text = (update.message.text or "").split(maxsplit=1)
    arg = text[1].strip() if len(text) > 1 else ""
    # accept @username, bare username, or a profile URL
    url = extract_url(arg)
    if url:
        ig = IG_USER_RE.match(url)
        if not ig or IG_P_RE.search(url) or IG_STORY_RE.search(url):
            await update.message.reply_text(
                "این لینک پست/استوریه، نه پروفایل. لینک پروفایل بده (مثل instagram.com/username)."
            )
            return
        username = ig.group(1)
    else:
        username = arg.lstrip("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", username):
            await update.message.reply_text("مثل: /dp username یا /dp @username")
            return
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE:
        await update.message.reply_text("یه دانلود در جریانه — صبر کن تموم شه ⏳")
        return
    await do_ig_profile(update, ctx, username)


async def do_x_photos(update: Update, ctx: ContextTypes.DEFAULT_TYPE, tweet_id: str):
    """Fetch all photos of an X post and send them as one media group."""
    chat_id = update.effective_chat.id
    ACTIVE.add(chat_id)
    wait_msg = None
    tmpdir = None
    try:
        wait_msg = await update.message.reply_text("🐦 در حال گرفتن عکس‌های پست X...")
        photos, videos = await asyncio.to_thread(x_media_urls, tweet_id)
        if not photos:
            hint = (
                "این پست عکس نداره (ویدیو یا متنیه) — با انتخاب کیفیت معمولی بفرستش."
                if videos
                else "عکسی از این پست پیدا نکردم (پست خصوصی/حذف‌شده یا API در دسترس نیست)."
            )
            await wait_msg.edit_text(f"❌ {hint}")
            return
        total = len(photos)
        await wait_msg.edit_text(f"🐦 {total} عکس پیدا شد — ⏳ در حال دانلود...")
        paths, failed = await asyncio.to_thread(download_x_photos, photos)
        if not paths:
            await wait_msg.edit_text("❌ دانلود عکس‌ها شکست خورد (twimg در دسترس نیست).")
            return
        tmpdir = os.path.dirname(paths[0])
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)

        # Telegram albums: max 10 items per send; send in chunks of 10.
        for chunk_start in range(0, len(paths), TG_ALBUM_LIMIT):
            chunk = paths[chunk_start:chunk_start + TG_ALBUM_LIMIT]
            handles = [open(p, "rb") for p in chunk]
            media = [InputMediaPhoto(media=h) for h in handles]
            try:
                await ctx.bot.send_media_group(chat_id, media)
            finally:
                for h in handles:
                    h.close()
        failed_note = f"\n⚠️ {failed} عکس دانلود نشد." if failed else ""
        try:
            await wait_msg.delete()
        except Exception:
            pass
        if failed:
            await update.message.reply_text(f"✅ عکس‌ها ارسال شد.{failed_note}")
    except Exception as e:
        log.exception("x photos failed")
        try:
            if wait_msg:
                await wait_msg.edit_text(f"❌ خطا: {str(e)[:200]}")
        except Exception:
            pass
    finally:
        if tmpdir:
            cleanup_dir(tmpdir)
        ACTIVE.discard(chat_id)


async def on_mp3(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    url = extract_url(update.message.text or "")
    if not url:
        await update.message.reply_text("مثل: /mp3 https://...")
        return
    await do_download(update, ctx, url, quality="audio")


async def on_quality_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    if chat_id != ALLOWED_USER_ID and q.from_user.id != ALLOWED_USER_ID:
        await q.answer("🔒 فقط مالک.", show_alert=True)
        return
    pending = PENDING_URL.pop(chat_id, None)
    if not pending:
        await q.answer("لینکی در انتظار نیست — دوباره لینک بفرست.", show_alert=True)
        return
    await q.answer()
    quality = {"480": "480p", "720": "720p", "1080": "1080p", "best": "Best"}.get(
        q.data.split(":")[1], "720p"
    )
    await do_download(update, ctx, pending["url"], quality=quality, edit_msg_id=pending["msg_id"])


async def do_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str,
                      quality: str, edit_msg_id: int | None = None):
    chat_id = update.effective_chat.id
    audio_only = quality == "audio"
    ACTIVE.add(chat_id)
    label = "🎵 صدا" if audio_only else f"🎬 {quality}"
    wait_msg = None
    try:
        if edit_msg_id:
            wait_msg = await ctx.bot.edit_message_text(
                f"{label} — ⏳ در حال دانلود...", chat_id, edit_msg_id
            )
        else:
            wait_msg = await update.message.reply_text(f"{label} — ⏳ در حال دانلود...")

        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        path, err = await asyncio.to_thread(run_ytdlp, url, quality, audio_only)
        if err:
            await wait_msg.edit_text(f"❌ {err}")
            return

        size = os.path.getsize(path)
        caption = os.path.splitext(os.path.basename(path))[0][:60]
        if size > DOC_LIMIT:
            await wait_msg.edit_text(
                f"❌ فایل {size // (1024*1024)}MB است — بزرگ‌تر از سقف ۵۰MB تلگرام.\n"
                "کیفیت پایین‌تر رو امتحان کن."
            )
            os.remove(path)
            return

        with open(path, "rb") as fh:
            if audio_only:
                await update.effective_chat.send_audio(audio=fh, title=caption[:64])
            elif size <= TG_FILE_LIMIT:
                await update.effective_chat.send_video(
                    video=fh, caption=caption, supports_streaming=True
                )
            else:
                await update.effective_chat.send_document(document=fh, caption=caption)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        os.remove(path)
    except Exception as e:
        log.exception("download failed")
        try:
            await wait_msg.edit_text(f"❌ خطا: {str(e)[:200]}")
        except Exception:
            pass
    finally:
        ACTIVE.discard(chat_id)


def main():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("mp3", on_mp3))
    app.add_handler(CommandHandler("dp", on_dp))
    app.add_handler(CallbackQueryHandler(on_quality_pick, pattern=r"^q:"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND
            & (filters.Entity("url") | filters.Entity("text_link")),
            on_link,
        )
    )
    log.info(f"XD bot polling started (owner={ALLOWED_USER_ID})")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
