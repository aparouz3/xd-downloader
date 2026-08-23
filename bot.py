#!/usr/bin/env python3
"""
XD Downloader Bot — Telegram video downloader.
Engine: yt-dlp (the same engine ReClip uses) + ffmpeg.

Flow:
  1. user sends a link  -> bot asks for quality (inline keyboard)
  2. user picks quality -> yt-dlp downloads -> bot delivers video/audio

Supports: YouTube, X/Twitter, Instagram, TikTok, Reddit, Facebook,
Vimeo, SoundCloud + 1000+ more sites (anything yt-dlp handles).

Access control: only ALLOWED_USER_ID (the owner) can use it.
"""

import glob
import logging
import os
import re
import shutil
import subprocess
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
        "• /mp3 قبل از لینک = فقط صدا (بدون انتخاب کیفیت)\n"
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
    url = extract_url(update.message.text or "")
    if not url:
        await update.message.reply_text("لینک معتبر پیدا نکردم. یه URL کامل بفرست.")
        return
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE:
        await update.message.reply_text("یه دانلود در جریانه — صبر کن تموم شه ⏳")
        return
    await ask_quality(update, url)


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
        path, err = await asyncio_to_thread(run_ytdlp, url, quality, audio_only)
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


async def asyncio_to_thread(fn, *args):
    import asyncio
    return await asyncio.to_thread(fn, *args)


def main():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("mp3", on_mp3))
    app.add_handler(CallbackQueryHandler(on_quality_pick, pattern=r"^q:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    log.info(f"XD bot polling started (owner={ALLOWED_USER_ID})")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
