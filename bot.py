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
from itertools import count
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

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                    InputMediaPhoto, InputMediaVideo, InputMediaAudio,
                    InputMediaDocument, KeyboardButton, ReplyKeyboardMarkup)
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
PENDING_URL = {}             # chat_id -> {"url", "msg_id", "user_caption"} awaiting quality choice
# Download mode per chat: "quick" (auto 1080p-or-best, caption, no questions)
# or "pro" (send caption + author, then ask quality). Default = pro.
MODES = {}
# --- "ارسال به کانال" (share to channel) ---
SHARE_CHANNEL = "@twitteology"
SHARES: dict[str, dict] = {}   # key -> {chat_id, message_ids, kind}
_share_seq = count()
MODE_QUICK = "⚡ سریع"
MODE_PRO = "🎬 حرفه‌ای"
# Quick mode: 1080p first; the format selectors fall back to lower
# qualities (or best) automatically when 1080p doesn't exist.
QUICK_QUALITY = "1080p"

def mode_kb(chat_id):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(MODE_QUICK), KeyboardButton(MODE_PRO)]],
        resize_keyboard=True, one_time_keyboard=False,
        selective=True, input_field_placeholder="لینک بفرستید…")

def mode_of(chat_id):
    return MODES.get(chat_id, "pro")


def share_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📤 ارسال به کانال", callback_data=f"share:{key}")]])


def _share_payload(msgs) -> list[dict]:
    """Snapshot sent messages so the channel re-post needs no later fetching:
    file_id (media) or plain text + caption — identical rendering, zero re-download."""
    out = []
    for m in msgs:
        if m.video:
            # PTB Video has no supports_streaming attribute (it's a send-time
            # param only) — reading it raised AttributeError and silently
            # killed the share button on video posts.
            out.append({"kind": "video", "file_id": m.video.file_id,
                        "caption": m.caption,
                        "supports_streaming": getattr(m.video, "supports_streaming", True)})
        elif m.photo:
            out.append({"kind": "photo", "file_id": m.photo[-1].file_id, "caption": m.caption})
        elif m.audio:
            out.append({"kind": "audio", "file_id": m.audio.file_id,
                        "caption": m.caption or m.audio.title})
        elif m.document:
            out.append({"kind": "document", "file_id": m.document.file_id, "caption": m.caption})
        else:
            out.append({"kind": "text", "text": m.text or m.caption or ""})
    # Verbatim: the pro-mode 📬 author line belongs in the channel post too.
    return out


async def add_share(update, sent, kind: str) -> InlineKeyboardMarkup | None:
    """Pro mode only: snapshot 'sent' message(s) for a native channel re-post
    and return the share-button keyboard (None in quick mode)."""
    if not isinstance(sent, (list, tuple)):
        sent = [sent]
    key = str(next(_share_seq))
    while len(SHARES) > 200:
        SHARES.pop(next(iter(SHARES)))
    SHARES[key] = {"chat_id": update.effective_chat.id,
                   "message_ids": [m.message_id for m in sent],
                   "album": kind == "album", "multi_text": kind == "thread",
                   "payload": _share_payload(sent)}
    return share_kb(key)

# Quality presets: label -> yt-dlp -f selector (height caps keep under 50MB)
QUALITIES = {
    "480p":  "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "Best":  "bestvideo+bestaudio/best",
}


# ── yt-dlp runner (blocking; runs in thread via asyncio.to_thread) ──────
def run_ytdlp(url: str, quality: str, audio_only: bool) -> tuple[str | None, str | None, str]:
    """Returns (local_path, error, post_caption)."""
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
        return None, "دانلود بیش از حد طول کشید (timeout)", ""
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        return None, (err[-1][:300] if err else "خطای نامشخص yt-dlp"), ""

    # newest file in download dir, touched within the last 15 min
    fresh = [
        f for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*"))
        if time.time() - os.path.getmtime(f) < 900
    ]
    if not fresh:
        return None, None, ""
    path = max(fresh, key=os.path.getmtime)
    return path, None, fetch_post_caption(url)


def fetch_post_meta(url: str) -> tuple[str, str]:
    """(caption, uploader) via yt-dlp metadata-only call. Best-effort: '' on failure."""
    cmd = [YTDLP, "--no-playlist", "--no-warnings", "--skip-download",
           "--socket-timeout", "30", "-j", url]
    cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    if os.path.isfile(cookies_file):
        cmd = cmd[:2] + ["--cookies", cookies_file] + cmd[2:]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            return "", ""
        info = json.loads(r.stdout.strip().splitlines()[-1])
        text = (info.get("description") or "").strip() or (info.get("title") or "").strip()
        author = (info.get("uploader") or info.get("channel") or info.get("creator") or "").strip()
        return sanitize_caption(text), sanitize_caption(author)
    except Exception:
        return "", ""


def fetch_post_caption(url: str) -> str:
    """Post caption only — see fetch_post_meta."""
    return fetch_post_meta(url)[0]


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


def x_video_variants(tweet_id: str) -> list[dict]:
    """Return X post video variants (direct mp4s with bitrates) via fxtwitter/vxtwitter.

    Each entry: {"url", "bitrate", "duration", "width", "height"}.
    """
    data = fetch_x_tweet(tweet_id)
    if not data:
        return []
    media = ((data or {}).get("tweet") or {}).get("media") or {}
    out = []
    for v in media.get("videos", []):
        duration = v.get("duration")
        width, height = v.get("width"), v.get("height")
        variants = [x for x in (v.get("variants") or []) if "video/mp4" in (x.get("content_type") or "")]
        if not variants and v.get("url", "").startswith("https://video.twimg.com"):
            variants = [{"url": v["url"], "bitrate": 0}]
        for x in variants:
            out.append({
                "url": x.get("url"),
                "bitrate": x.get("bitrate") or 0,
                "duration": duration,
                "width": width,
                "height": height,
            })
    return [x for x in out if x.get("url")]


def _pick_x_video_variant(variants: list[dict], max_bytes: int = 49_000_000) -> dict | None:
    """Pick the best video variant whose estimated size fits the Telegram 50MB bot limit."""
    if not variants:
        return None
    def est(v):
        return v["bitrate"] * (v.get("duration") or 60) / 8
    fitted = [v for v in variants if est(v) <= max_bytes]
    if fitted:
        return max(fitted, key=lambda v: est(v))
    return min(variants, key=lambda v: est(v))


def x_tweet_caption(tweet_id: str) -> str:
    """Caption (text) of an X post via fxtwitter. Empty string if unavailable."""
    data = fetch_x_tweet(tweet_id)
    tweet = (data or {}).get("tweet") or {}
    return (tweet.get("text") or "").strip()


AUTHOR_LINE_TMPL = "📬 @twitteology┆ {name}"

def author_line(name: str) -> str:
    """Signature author line appended below captions (handle/emoji constant)."""
    return AUTHOR_LINE_TMPL.format(name=name.strip()) if name and name.strip() else ""


def _link_safe_truncate(text: str, limit: int = 1024) -> str:
    """Trim to [limit] chars WITHOUT cutting a URL mid-way.

    Telegram caps captions at 1024 chars; a blind [:1024] slice can amputate
    a link that ends the caption — the link then looks 'gone'. When the cut
    lands inside the last URL, the URL is dropped whole instead (the rest of
    the caption is kept). No URL = no change.
    """
    if not text or len(text) <= limit:
        return text
    cut = text[:limit]
    tail = cut[-300:]
    m = re.search(r"https?://\S+", tail)
    if m:
        drop_from = len(cut) - (len(tail) - m.start())
        cut = cut[:drop_from].rstrip()
    return cut


def _sig_parts(body: str, author: str, pro: bool) -> list:
    """Caption body + optional signature, with a BLANK LINE between them.

    Pro mode: the constant 📬 line always goes under the post, separated by
    one empty line (Enter). Quick mode: body only, no signature.
    """
    parts = [p for p in (body.strip(),) if p]
    if pro:
        sig = author_line(author)
        if sig:
            parts.append("")      # blank line: Enter between caption and 📬
            parts.append(sig)
    return parts


def x_tweet_author(tweet_id: str) -> str:
    """Author display name of an X post via fxtwitter (no @handle). '' on failure."""
    data = fetch_x_tweet(tweet_id)
    author = ((data or {}).get("tweet") or {}).get("author") or {}
    return (author.get("name") or "").strip()


def _x_thread_texts_threadreader(tweet_id: str) -> list[str]:
    """Unrolled thread texts via threadreaderapp only (best-effort)."""
    import html as _html
    try:
        req = urllib.request.Request(
            f"https://threadreaderapp.com/thread/{tweet_id}.html",
            headers={"User-Agent": BROWSER_UA})
        resp = urllib.request.urlopen(req, timeout=25)
        page = resp.read().decode("utf-8", "ignore")
        if "/authenticate" in resp.geturl():
            return []
        blocks = re.findall(
            r'data-screenname="([^"]+)" data-tweet="(\d+)" dir="auto">(.*?)</div>',
            page, re.S)
    except Exception as e:
        log.warning("threadreader failed for %s: %s", tweet_id, e)
        return []
    texts = []
    for _sn, _tid, body in blocks:
        b = re.sub(r"<br\s*/?>", "\n", body)
        b = re.sub(r"<sup class=\"tw-permalink\">.*?</sup>", "", b, flags=re.S)
        t = _html.unescape(re.sub(r"<[^>]+>", "", b)).strip()
        if t:
            texts.append(t)
    return texts

NITTER_INSTANCES = ["nitter.lol", "nitter.privacyredirect.com", "nitter.tiekoetter.com", "nitter.poast.org"]


def x_tweet_author_handle(tweet_id: str) -> str:
    """@-less screen_name of a post's author via fxtwitter ('' on failure)."""
    data = fetch_x_tweet(tweet_id)
    author = ((data or {}).get("tweet") or {}).get("author") or {}
    return (author.get("screen_name") or "").strip()


def _nitter_own_tweets(tweet_id: str, handle: str) -> list[tuple[str, str]]:
    """(status_id, text) of the AUTHOR's OWN tweets in the conversation (root excluded).

    The item's own author lives in its tweet-link permalink (/AUTHOR/status/…);
    the in-text @username is unreliable — replies echo the main handle first.
    """
    import html as _html
    if not handle:
        return []
    for inst in NITTER_INSTANCES:
        try:
            req = urllib.request.Request(
                f"https://{inst}/{handle}/status/{tweet_id}",
                headers={"User-Agent": BROWSER_UA})
            page = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
            if "not a bot" in page or "Not a robot" in page or "Just a moment" in page:
                continue
            items = re.findall(
                r'<div class="timeline-item[^"]*"[^>]*>(.*?)(?=<div class="timeline-item|<footer)',
                page, re.S)
            out = []
            for it in items:
                link = re.search(r'<a class="tweet-link" href="/([^/]+)/status/(\d+)', it)
                if not link or link.group(1).lower() != handle.lower():
                    continue
                sid = link.group(2)
                if sid == tweet_id:
                    continue
                t = re.search(r'<div class="tweet-content[^>]*>(.*?)</div>', it, re.S)
                if not t:
                    continue
                b = re.sub(r"<br\s*/?>", "\n", t.group(1))
                txt = _html.unescape(re.sub(r"<[^>]+>", "", b)).strip()
                if txt:
                    out.append((sid, txt))
            if out:
                return out
        except Exception as e:
            log.warning("nitter %s failed for %s: %s", inst, tweet_id, e)
            continue
    return []


def _nitter_thread(tweet_id: str, handle: str) -> list[str]:
    """Author's own tweets' texts in the conversation (root excluded), via nitter."""
    return [t for _, t in _nitter_own_tweets(tweet_id, handle)]


def x_self_reply_continuation(tweet_id: str) -> list[str]:
    """The author's OWN SELF-REPLIES that continue the post's caption, in order.

    A self-reply is an author tweet whose reply parent is this root post — the
    author completing their cut-off caption. Replies the author wrote to OTHER
    people's comments are excluded. Nitter provides the conversation (ids + text),
    fxtwitter resolves each reply's parent. Returns [] when none.
    """
    handle = x_tweet_author_handle(tweet_id)
    if not handle:
        return []
    pairs = _nitter_own_tweets(tweet_id, handle)
    if not pairs:
        return []
    out = []
    for sid, text in pairs:
        try:
            req = urllib.request.Request(
                f"https://api.fxtwitter.com/{handle}/status/{sid}",
                headers={"User-Agent": BROWSER_UA})
            body = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
            tw = (json.loads(body).get("tweet")) or {}
        except Exception as e:
            log.warning("fxtwitter parent check failed for %s: %s", sid, e)
            continue
        if tw.get("replying_to_status") == tweet_id:
            out.append(text)
    return out


def _x_thread_from_fxtwitter(tweet_id: str) -> list[str]:
    """Author's continuation via fxtwitter's ``thread`` field (user-routed URL).

    fxtwitter returns ``tweet.thread`` = the rest of the author's own thread when
    the post is part of one — same no-auth API per media as for captions/photos.
    """
    handle = x_tweet_author_handle(tweet_id)
    if not handle:
        return []
    try:
        req = urllib.request.Request(
            f"https://api.fxtwitter.com/{handle}/status/{tweet_id}",
            headers={"User-Agent": BROWSER_UA})
        data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore"))
    except Exception as e:
        log.warning("fxtwitter thread failed for %s: %s", tweet_id, e)
        return []
    th = (((data or {}).get("tweet")) or {}).get("thread")
    if not isinstance(th, list):
        return []
    return [t.get("text", "").strip() for t in th if (t.get("text") or "").strip()]


def x_thread_texts(tweet_id: str) -> list[str]:
    """Author's own unrolled thread texts for a post.

    Sources, in order: fxtwitter ``tweet.thread`` (author continuation, media-safe)
    → threadreader (unrolled thread) → author's self-replies on X/Nitter.
    The root tweet text is included as first element when present.
    """
    texts = _x_thread_from_fxtwitter(tweet_id)
    if not texts:
        texts = _x_thread_texts_threadreader(tweet_id)
    if not texts:
        root = x_tweet_caption(tweet_id)
        selfrep = x_self_reply_continuation(tweet_id)
        if root or selfrep:
            texts = ([root] if root else []) + selfrep
    return texts


def download_x_video(tweet_id: str, max_height: int | None = None) -> tuple[str | None, str | None]:
    """Download an X post video directly from twimg (no auth). Returns (path, error).
    max_height: if set, prefer variants at/below this height before the size-fit pick."""
    variants = x_video_variants(tweet_id)
    if not variants:
        return None, "ویدیویی برای این پست پیدا نکردم (خصوصی/حذف‌شده یا API در دسترس نیست)."
    if max_height:
        capped = [v for v in variants if (v.get("height") or 0) <= max_height]
        if capped:
            variants = capped
    variant = _pick_x_video_variant(variants)
    if not variant:
        return None, "ویدیویی برای این پست پیدا نکردم (خصوصی/حذف‌شده یا API در دسترس نیست)."
    dest = os.path.join(DOWNLOAD_DIR, f"x_{tweet_id}.mp4")
    try:
        req = urllib.request.Request(variant["url"], headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    except Exception as e:
        return None, f"دانلود ویدیو شکست خورد: {str(e)[:150]}"
    if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        return None, "فایل ویدیو خالی بود."
    return dest, None


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
        "🎬 لینک بفرست، ویدیو تحویل بگیر\n\n"
        "• ⚡ سریع: دانلود خودکار (۱۰۸۰p یا بهترین موجود) + کپشن — بدون سوال\n"
        "• 🎬 حرفه‌ای: اول کپشن و نام author پست، بعد انتخاب کیفیت\n"
        "• با دکمه‌های پایین می‌تونی حالت رو عوض کنی\n"
        "• یوتیوب / X / اینستاگرام / تیک‌تاک / ردیت و ۱۰۰۰+ سایت دیگه\n"
        "• پست‌های چندعکسی X → همهٔ عکسها یه‌جا میاد (بدون انتخاب کیفیت)\n"
        "• /mp3 قبل از لینک = فقط صدا (بدون انتخاب کیفیت)\n"
        "• /dp username یا لینک پروفایل اینستاگرام = عکس پروفایل\n"
        "• سقف تلگرام: ~۵۰MB — کیفیت‌های بالاتر ممکنه جا نشه",
        reply_markup=mode_kb(update.effective_chat.id),
    )


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


def sanitize_caption(text: str | None) -> str:
    """Make text safe as a Telegram caption: no HTML tags (message may be sent
    with parse_mode=HTML), trimmed to the 1024-char caption limit."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]*>", "", str(text)).strip()
    return clean[:1024]


def user_note(text: str | None) -> str:
    """The user's own note in a message: text with URLs and /mp3 stripped.
    Empty string if they only sent a bare link."""
    if not text:
        return ""
    stripped = re.sub(r"^/mp3\b", "", text, flags=re.I)
    stripped = URL_RE.sub("", stripped)
    return sanitize_caption(stripped)


async def ask_quality(update: Update, url: str, user_caption: str = "",
                      kind: str = "ytdlp", tweet_id: str = ""):
    """Show inline keyboard with quality choices, store pending URL + caption."""
    msg = await update.message.reply_text("📥 لینک دریافت شد. کیفیت مورد نظر؟")
    PENDING_URL[update.effective_chat.id] = {
        "url": url, "msg_id": msg.message_id, "user_caption": user_caption,
        "kind": kind, "tweet_id": tweet_id,
    }
    kb = [
        [
            InlineKeyboardButton("480p", callback_data="q:480"),
            InlineKeyboardButton("720p", callback_data="q:720"),
            InlineKeyboardButton("1080p", callback_data="q:1080"),
            InlineKeyboardButton("🎯 Best", callback_data="q:best"),
        ]
    ]
    await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(kb))


async def on_share(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """'ارسال به کانال' button: forward the exact stored message(s) to the channel."""
    if not is_owner(update):
        await update.callback_query.answer("فقط صاحب ربات می‌تونه.", show_alert=True)
        return
    key = update.callback_query.data.split(":", 1)[1]
    rec = SHARES.get(key)
    if not rec:
        await update.callback_query.answer("این پست منقضی شده (ربات ری‌استارت شده؟).", show_alert=True)
        return
    if update.effective_chat.id != rec["chat_id"]:
        await update.callback_query.answer("دکمه فقط همون چت خودش کار می‌کنه.", show_alert=True)
        return
    try:
        # Native re-post from the stored snapshot (file_id reuse — instant, no
        # re-download, and no "Forwarded from"/bot-name trace in the channel).
        await repost_to_channel(ctx.bot, rec)
    except Exception as e:
        await update.callback_query.answer(f"❌ ارسال به کانال نشد: {str(e)[:80]}", show_alert=True)
        return
    SHARES.pop(key, None)
    await update.callback_query.answer("✅ به کانال ارسال شد")
    try:
        await ctx.bot.edit_message_reply_markup(
            chat_id=rec["chat_id"], message_id=rec["message_ids"][-1], reply_markup=None)
    except Exception:
        pass


async def repost_to_channel(bot_obj, rec: dict) -> None:
    """Re-post the snapshotted payload into the channel natively (file_id reuse).

    Media/album/thread-text each re-created, NOT forwarded: no "Forwarded from"
    header and no bot signature — the channel posts as itself."""
    payload = rec["payload"]
    if rec.get("album") and len(payload) > 1:
        media = []
        for i, it in enumerate(payload):
            media.append(InputMediaPhoto(media=it["file_id"],
                                         caption=it.get("caption") if i == 0 else None))
        await bot_obj.send_media_group(SHARE_CHANNEL, media)
        return
    if rec.get("multi_text"):
        for it in payload:
            await bot_obj.send_message(SHARE_CHANNEL, text=it["text"])
        return
    it = payload[0]
    k = it["kind"]
    if k == "video":
        await bot_obj.send_video(SHARE_CHANNEL, video=it["file_id"], caption=it.get("caption"),
                                 supports_streaming=it.get("supports_streaming", True))
    elif k == "photo":
        await bot_obj.send_photo(SHARE_CHANNEL, photo=it["file_id"], caption=it.get("caption"))
    elif k == "audio":
        await bot_obj.send_audio(SHARE_CHANNEL, audio=it["file_id"], caption=it.get("caption"))
    elif k == "document":
        await bot_obj.send_document(SHARE_CHANNEL, document=it["file_id"], caption=it.get("caption"))
    else:
        await bot_obj.send_message(SHARE_CHANNEL, text=it.get("text") or "")


async def on_mode_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reply-keyboard press of ⚡ سریع / 🎬 حرفه‌ای — sticky per-chat mode switch."""
    if not is_owner(update):
        return
    chat_id = update.effective_chat.id
    MODES[chat_id] = "quick" if (update.message.text or "").strip() == MODE_QUICK else "pro"
    await update.message.reply_text(
        f"✅ حالت: {MODE_QUICK if MODES[chat_id] == 'quick' else MODE_PRO} — "
        + ("هر لینک، خودکار با کیفیت خوب (۷۲۰p) + کپشن دانلود میشه."
           if MODES[chat_id] == "quick"
           else "اول کپشن و نام author، بعد انتخاب کیفیت."),
        reply_markup=mode_kb(chat_id))


FWD_NOTE = "↩️ برای انتشار در کانال، یک «/» اول متن بفرست."


def _strip_sig(text: str) -> str:
    """Remove our bot's channel signature lines so re-posts stay clean."""
    lines = [l for l in (text or "").splitlines()
             if "@twitteology" not in l and "📬" not in l]
    return "\n".join(lines).strip()


FWD_BUF: dict[str, list] = {}      # media_group_id -> [Message, ...]
FWD_TASKS: dict[str, object] = {}  # media_group_id -> asyncio flush task


def _media_item(cm):
    """InputMedia item for a forwarded photo/video/document (file_id reuse)."""
    if cm.photo:
        return InputMediaPhoto(media=cm.photo[-1].file_id, caption=cm.caption)
    if cm.video:
        return InputMediaVideo(media=cm.video.file_id, caption=cm.caption,
                               supports_streaming=True)
    if cm.document:
        return InputMediaDocument(media=cm.document.file_id, caption=cm.caption)
    if cm.audio:
        return InputMediaAudio(media=cm.audio.file_id, caption=cm.caption)
    return None


async def _flush_fwd_group(ctx, mgid: str, chat_id: int):
    """Publish a buffered forwarded album as ONE native channel album."""
    msgs = sorted(FWD_BUF.pop(mgid, []), key=lambda m: m.message_id)
    FWD_TASKS.pop(mgid, None)
    if not msgs:
        return
    try:
        media = []
        for cm in msgs:
            it = _media_item(cm)
            if it is None:
                continue
            if media:
                it = type(it)(media=it.media)  # caption only on the first item
            media.append(it)
        # captions: cleaned of our signature line
        if media and msgs[0].caption:
            cap = _strip_sig(msgs[0].caption)
            media[0] = type(media[0])(media=media[0].media, caption=cap or None)
        if len(media) > 1:
            await ctx.bot.send_media_group(SHARE_CHANNEL, media)
        elif len(media) == 1:
            it = media[0]
            k = type(it).__name__
            if k == "InputMediaPhoto":
                await ctx.bot.send_photo(SHARE_CHANNEL, photo=it.media, caption=it.caption)
            elif k == "InputMediaVideo":
                await ctx.bot.send_video(SHARE_CHANNEL, video=it.media,
                                         caption=it.caption, supports_streaming=True)
            elif k == "InputMediaAudio":
                await ctx.bot.send_audio(SHARE_CHANNEL, audio=it.media, caption=it.caption)
            else:
                await ctx.bot.send_document(SHARE_CHANNEL, document=it.media,
                                            caption=it.caption)
        await ctx.bot.send_message(chat_id, "✅ منتشر شد")
    except Exception:
        log.exception("fwd album flush failed")
        try:
            await ctx.bot.send_message(chat_id, "❌ انتشار آلبوم شکست خورد")
        except Exception:
            pass


async def on_channel_forward(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Owner forwards a post (channel/elsewhere) into the bot DM → publish it
    into SHARE_CHANNEL natively: no 'Forwarded from' header, no bot name."""
    if not is_owner(update):
        return
    m = update.message
    if getattr(m, "forward_origin", None) is None:
        return
    chat_id = update.effective_chat.id
    # Forwarded TEXT containing a link = old workflow: download that link instead.
    if (m.text and not _media_item(m)) and extract_url(m.text):
        await on_link(update, ctx)
        return
    # Album: Telegram delivers items as separate updates — buffer, flush after 2s.
    if m.media_group_id:
        FWD_BUF.setdefault(m.media_group_id, []).append(m)
        if m.media_group_id not in FWD_TASKS:
            FWD_TASKS[m.media_group_id] = asyncio.get_running_loop().call_later(
                2.0, lambda: asyncio.ensure_future(
                    _flush_fwd_group(ctx, m.media_group_id, chat_id)))
        return
    media = _media_item(m)
    try:
        if media is not None:
            if isinstance(media, InputMediaPhoto):
                await ctx.bot.send_photo(SHARE_CHANNEL, photo=media.media,
                                         caption=_strip_sig(m.caption or "") or None)
            elif isinstance(media, InputMediaVideo):
                await ctx.bot.send_video(SHARE_CHANNEL, video=media.media,
                                         caption=_strip_sig(m.caption or "") or None,
                                         supports_streaming=True)
            elif isinstance(media, InputMediaAudio):
                await ctx.bot.send_audio(SHARE_CHANNEL, audio=media.media,
                                         caption=_strip_sig(m.caption or "") or None)
            else:
                await ctx.bot.send_document(SHARE_CHANNEL, document=media.media,
                                            caption=_strip_sig(m.caption or "") or None)
            await m.reply_text("✅ منتشر شد")
            return
        txt = _strip_sig(m.text or "")
        if not txt:
            await m.reply_text("❌ متنی برای انتشار پیدا نشد.")
        elif txt.startswith("/"):
            # escape hatch: "/..." prefix publishes the text VERBATIM
            await ctx.bot.send_message(SHARE_CHANNEL, text=txt.lstrip("/").lstrip())
            await m.reply_text("✅ منتشر شد")
        else:
            await m.reply_text(FWD_NOTE)
    except Exception as e:
        log.exception("channel forward publish failed")
        try:
            await m.reply_text(f"❌ انتشار در کانال شکست خورد: {str(e)[:120]}")
        except Exception:
            pass


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
        await do_x_photos(update, ctx, xm.group(1), user_caption=text)
        return

    mode = mode_of(update.effective_chat.id)
    if mode == "quick":
        # ⚡ quick: auto-download at good quality, caption attached, no questions.
        await do_download(update, ctx, url, quality=QUICK_QUALITY, user_caption=text)
        return

    # 🎬 pro: show caption + author first, then ask quality.
    info = await update.message.reply_text("🔎 در حال گرفتن اطلاعات پست...")
    xm2 = X_URL_RE.search(url)
    if xm2:
        tid = xm2.group(1)
        info_cap = await asyncio.to_thread(x_tweet_caption, tid)
        info_author = await asyncio.to_thread(x_tweet_author, tid)
    else:
        info_cap, info_author = await asyncio.to_thread(fetch_post_meta, url)
    lines = _sig_parts(info_cap[:500], info_author, pro=True)
    await info.edit_text("\n".join(lines) if lines else "ℹ️ اطلاعات افزوده‌ای برای این پست پیدا نشد.")
    await ask_quality(update, url, user_caption=text)


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


async def send_x_thread_text(update: Update, chat_id: int, body: str,
                             own: str, author: str) -> list:
    """Split [body] into ≤4000-char Telegram messages (pro signature at the end),
    prepend [own] to the first chunk, and send them. Returns the sent messages.

    Shared by the text-only X path and the photo X path so an author's
    continuation captions (written as self-replies/comments) are delivered the
    same way whether the tweet has media or not.
    """
    sig = author_line(author) if mode_of(chat_id) == "pro" else ""
    parts, cur = [], ""
    for para in body.split("\n\n"):
        add = (para + "\n\n") if cur else para + "\n\n"
        if len(cur) + len(add) > 4000 - len(sig):
            parts.append(cur.strip())
            cur = para + "\n\n"
        else:
            cur += add
    # Blank line before the 📬 line (pro), then the signature on the last chunk.
    cur = (cur.strip() + ("\n\n" + sig if sig else "")).strip()
    parts.append(cur)
    if own:
        parts[0] = own + "\n\n" + parts[0]
    sent = []
    for piece in parts:
        sent.append(await update.effective_chat.send_message(piece))
    return sent


async def do_x_photos(update: Update, ctx: ContextTypes.DEFAULT_TYPE, tweet_id: str,
                      user_caption: str = ""):
    """Fetch all photos of an X post and send them as one media group.
    Caption priority: text the user sent with the link > post text (fxtwitter)."""
    chat_id = update.effective_chat.id
    ACTIVE.add(chat_id)
    wait_msg = None
    tmpdir = None
    try:
        wait_msg = await update.message.reply_text("🐦 در حال بررسی پست X...")
        photos, videos = await asyncio.to_thread(x_media_urls, tweet_id)
        if not photos and videos and mode_of(chat_id) == "pro":
            # Pro mode: don't auto-pick — ask quality first (variants are height-capped).
            await wait_msg.delete()
            await ask_quality(update, f"x:{tweet_id}", user_caption=user_caption,
                              kind="xvideo", tweet_id=tweet_id)
            return
        if not photos and videos:
            # Quick mode / fallback: download the best size-fitting variant from twimg.
            await wait_msg.edit_text("🎬 ویدیوی X — ⏳ در حال دانلود...")
            path, err = await asyncio.to_thread(download_x_video, tweet_id)
            if err or not path:
                await wait_msg.edit_text(f"❌ {err or 'خطای نامشخص'}")
                return
            size = os.path.getsize(path)
            post_text = sanitize_caption(await asyncio.to_thread(x_tweet_caption, tweet_id))
            own = user_note(user_caption)
            if own:
                # user wrote their own note besides the link — use it
                caption = own
            elif post_text:
                caption = post_text
            else:
                caption = f"🐦 ویدیوی X ({size // (1024*1024)}MB)" if size > 1024 * 1024 else "🐦 ویدیوی X"
            sent = None
            with open(path, "rb") as fh:
                sent = await update.effective_chat.send_video(
                    video=fh, caption=caption, supports_streaming=True
                )
            if mode_of(chat_id) == "pro":
                try:
                    await sent.edit_reply_markup(reply_markup=await add_share(update, sent, "media"))
                except Exception:
                    log.exception("share kb attach failed")
            try:
                await wait_msg.delete()
            except Exception:
                pass
            os.remove(path)
            return
        if not photos:
            # Text-only post (or private/deleted): deliver the post text + signature line.
            # If it's a continuation thread (author's self-replies), try to merge all
            # discrete tweets into one text via threadreaderapp.
            author = await asyncio.to_thread(x_tweet_author, tweet_id)
            thread = await asyncio.to_thread(x_thread_texts, tweet_id)
            post_text = sanitize_caption(await asyncio.to_thread(x_tweet_caption, tweet_id))
            own = user_note(user_caption)
            if thread:
                # Signature goes at the end of the merged thread (last chunk) — pro only.
                body = "\n\n".join(thread)
                await wait_msg.delete()
                sent_msgs = await send_x_thread_text(update, chat_id, body, own, author)
                if mode_of(chat_id) == "pro":
                    try:
                        await sent_msgs[-1].edit_reply_markup(
                            reply_markup=await add_share(update, sent_msgs, "thread"))
                    except Exception:
                        log.exception("share kb attach failed")
                return
            if post_text:
                # Blank line between the caption and the 📬 line (pro).
                # Edit FIRST (updates wait_msg), then snapshot for share —
                # evaluating add_share inside the edit_text() args would capture
                # the pre-edit placeholder text instead of the post itself.
                # edit_text returns the UPDATED Message object (the old variable
                # keeps the placeholder text) — must rebind before snapshotting.
                wait_msg = await wait_msg.edit_text(
                    "\n".join(_sig_parts(own or post_text, author,
                                         mode_of(chat_id) == "pro")))
                if mode_of(chat_id) == "pro":
                    kb = await add_share(update, wait_msg, "text")
                    wait_msg = await wait_msg.edit_reply_markup(reply_markup=kb)
            else:
                await wait_msg.edit_text(
                    "❌ این پست ویدیو یا عکسی نداره (پست خصوصی/حذف‌شده یا API در دسترس نیست).")
            return
        total = len(photos)
        await wait_msg.edit_text(f"🐦 {total} عکس پیدا شد — ⏳ در حال دانلود...")
        paths, failed = await asyncio.to_thread(download_x_photos, photos)
        if not paths:
            await wait_msg.edit_text("❌ دانلود عکس‌ها شکست خورد (twimg در دسترس نیست).")
            return
        # Album caption: user's own note > post text from fxtwitter.
        post_text = sanitize_caption(await asyncio.to_thread(x_tweet_caption, tweet_id))
        own = user_note(user_caption)
        # The author often completes the caption in their OWN self-replies (the
        # tweet text can be cut mid-sentence). Merge those into the SINGLE album
        # caption — replies the author wrote to other people's comments are
        # excluded. One message, not two.
        selfrep = await asyncio.to_thread(x_self_reply_continuation, tweet_id)
        album_caption = own or post_text
        if selfrep:
            merged = ([album_caption] + selfrep) if album_caption else selfrep
            album_caption = "\n\n".join(merged)
        author = await asyncio.to_thread(x_tweet_author, tweet_id)
        # Pro mode: the fixed signature line goes under EVERY post — one blank
        # line after the caption, then the constant 📬 line.
        album_caption = "\n".join(_sig_parts(album_caption, author,
                                              mode_of(chat_id) == "pro"))
        # Trim to the 1024-char caption cap without amputating a link mid-URL.
        album_caption = _link_safe_truncate(album_caption)
        tmpdir = os.path.dirname(paths[0])
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)

        # Telegram albums: max 10 items per send; send in chunks of 10.
        group_msgs = []
        for chunk_start in range(0, len(paths), TG_ALBUM_LIMIT):
            chunk = paths[chunk_start:chunk_start + TG_ALBUM_LIMIT]
            handles = [open(p, "rb") for p in chunk]
            # PTB v22: InputMediaPhoto is immutable — caption must go in the constructor.
            media = [
                InputMediaPhoto(media=h, caption=(album_caption if i == 0 else None))
                for i, h in enumerate(handles)
            ]
            try:
                group_msgs.extend(await ctx.bot.send_media_group(chat_id, media))
            finally:
                for h in handles:
                    h.close()
        if group_msgs and mode_of(chat_id) == "pro":
            try:
                await group_msgs[-1].edit_reply_markup(
                    reply_markup=await add_share(update, group_msgs, "album"))
            except Exception:
                log.exception("share kb attach failed")
        failed_note = f"\n⚠️ {failed} عکس دانلود نشد." if failed else ""
        try:
            await wait_msg.delete()
        except Exception:
            pass
        # Photo posts: self-reply continuations are already merged into the single
        # album_caption above. No extra continuation messages sent.
        if failed:
            await update.message.reply_text(f"✅ عکسها ارسال شد.{failed_note}")
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


async def do_x_video_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE, tweet_id: str,
                          quality: str, edit_msg_id: int | None = None,
                          user_caption: str = ""):
    """Pro-mode X video after quality choice: capped download, caption + author."""
    chat_id = update.effective_chat.id
    ACTIVE.add(chat_id)
    wait_msg = None
    path = None
    try:
        cap_height = {"480p": 480, "720p": 720, "1080p": 1080}.get(quality)
        label = f"🎬 X — {quality}"
        if edit_msg_id:
            wait_msg = await ctx.bot.edit_message_text(
                f"{label} — ⏳ در حال دانلود...", chat_id, edit_msg_id)
        else:
            wait_msg = await update.message.reply_text(f"{label} — ⏳ در حال دانلود...")
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        path, err = await asyncio.to_thread(download_x_video, tweet_id, cap_height)
        if err:
            await wait_msg.edit_text(f"❌ {err}")
            return
        size = os.path.getsize(path)
        if size > DOC_LIMIT:
            await wait_msg.edit_text(
                f"❌ فایل {size // (1024*1024)}MB است — کیفیت پایین‌تر رو انتخاب کن.")
            os.remove(path); path = None
            return
        post_text = sanitize_caption(await asyncio.to_thread(x_tweet_caption, tweet_id))
        author = sanitize_caption(await asyncio.to_thread(x_tweet_author, tweet_id))
        own = user_note(user_caption)
        # Signature line under EVERY pro post — blank line after the caption,
        # then the constant 📬 line. Link-safe trim at the 1024-char cap.
        base = own or post_text
        caption = _link_safe_truncate(
            "\n".join(_sig_parts(base, author, mode_of(chat_id) == "pro"))
        ) or "🐦 ویدیوی X"
        with open(path, "rb") as fh:
            sent = await update.effective_chat.send_video(
                video=fh, caption=caption, supports_streaming=True)
        try:
            await sent.edit_reply_markup(reply_markup=await add_share(update, sent, "media"))
        except Exception:
            log.exception("share kb attach failed")
        try:
            await wait_msg.delete()
        except Exception:
            pass
        os.remove(path); path = None
    except Exception as e:
        log.exception("x video pick failed")
        try:
            if wait_msg:
                await wait_msg.edit_text(f"❌ خطا: {str(e)[:200]}")
        except Exception:
            pass
    finally:
        if path and os.path.isfile(path):
            try: os.remove(path)
            except Exception: pass
        ACTIVE.discard(chat_id)


async def on_mp3(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    url = extract_url(update.message.text or "")
    if not url:
        await update.message.reply_text("مثل: /mp3 https://...")
        return
    await do_download(update, ctx, url, quality="audio", user_caption=update.message.text or "")


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
    if pending.get("kind") == "xvideo":
        # X video: pick twimg variant capped at chosen height, send with caption (+author in pro).
        await do_x_video_pick(update, ctx, pending["tweet_id"], quality,
                              edit_msg_id=pending["msg_id"],
                              user_caption=pending.get("user_caption", ""))
        return
    await do_download(update, ctx, pending["url"], quality=quality, edit_msg_id=pending["msg_id"],
                      user_caption=pending.get("user_caption", ""))


async def do_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str,
                      quality: str, edit_msg_id: int | None = None,
                      user_caption: str = ""):
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
        path, err, post_caption = await asyncio.to_thread(run_ytdlp, url, quality, audio_only)
        if err:
            await wait_msg.edit_text(f"❌ {err}")
            return

        size = os.path.getsize(path)
        fallback = os.path.splitext(os.path.basename(path or ""))[0][:60]
        # user's own note (text besides the link) > post caption > filename
        caption = user_note(user_caption) or post_caption or fallback
        # Pro mode: the fixed signature line goes under EVERY post — blank line
        # after the caption, then the constant 📬 line (uploader from yt-dlp).
        # Link-safe trim at the 1024-char caption cap.
        if mode_of(chat_id) == "pro" and not audio_only:
            caption = _link_safe_truncate(
                "\n".join(_sig_parts(caption, fetch_post_meta(url)[1], pro=True)))
        if size > DOC_LIMIT:
            await wait_msg.edit_text(
                f"❌ فایل {size // (1024*1024)}MB است — بزرگ‌تر از سقف ۵۰MB تلگرام.\n"
                "کیفیت پایین‌تر رو امتحان کن."
            )
            os.remove(path)
            return

        sent = None
        with open(path, "rb") as fh:
            if audio_only:
                sent = await update.effective_chat.send_audio(audio=fh, title=caption[:64])
            elif size <= TG_FILE_LIMIT:
                sent = await update.effective_chat.send_video(
                    video=fh, caption=caption, supports_streaming=True
                )
            else:
                sent = await update.effective_chat.send_document(document=fh, caption=caption)
        if sent is not None and mode_of(chat_id) == "pro":
            try:
                await sent.edit_reply_markup(reply_markup=await add_share(update, sent, "media"))
            except Exception:
                log.exception("share kb attach failed")
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
    app.add_handler(CallbackQueryHandler(on_share, pattern=r"^share:"))
    # Reply-keyboard mode buttons (plain text, no URL) — before the link handler.
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND
            & filters.Regex(rf"^\s*({re.escape(MODE_QUICK)}|{re.escape(MODE_PRO)})\s*$"),
            on_mode_button,
        )
    )
    # Forwarded posts (text or media) → republish into the channel.
    # filters.FORWARDED only: a catch-all would swallow plain link messages.
    app.add_handler(MessageHandler(filters.FORWARDED, on_channel_forward))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND
            & (filters.Entity("url") | filters.Entity("text_link")),
            on_link,
        )
    )
    log.info(f"XD bot polling started (owner={ALLOWED_USER_ID})")
    # Explicit allowed_updates: an earlier bare getUpdates() can otherwise leave
    # the bot's subscription stuck to a partial set, silently dropping messages.
    # NOTE: channel_post deliberately EXCLUDED — receiving it makes the bot treat
    # its own/echoed channel posts as user links ("در حال بررسی…" spam in channel).
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "edited_message", "callback_query"])


if __name__ == "__main__":
    main()
