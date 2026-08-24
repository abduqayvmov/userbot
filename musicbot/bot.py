import os
import re
import uuid
import logging
import asyncio
import threading

import requests
import yt_dlp
import imageio_ffmpeg
from flask import Flask

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("musicbot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN muhit o'zgaruvchisi sozlanmagan.")

DOWNLOAD_DIR = "/tmp/musicbot_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
MAX_UPLOAD_BYTES = 45 * 1024 * 1024  # Telegram bot API ~50MB limitidan biroz pastroq

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

URL_RE = re.compile(r"https?://\S+")
PLATFORM_PATTERNS = {
    "Instagram": re.compile(r"instagram\.com"),
    "TikTok": re.compile(r"tiktok\.com"),
    "Pinterest": re.compile(r"pinterest\.[a-z.]+|pin\.it"),
    "YouTube": re.compile(r"youtube\.com|youtu\.be"),
}

pending_links: dict[str, str] = {}
pending_audio: dict[int, str] = {}


class TagStates(StatesGroup):
    waiting_title = State()
    waiting_artist = State()


def _cleanup(*paths):
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.warning("Faylni o'chirishda xatolik: %s", e)


def detect_platform(url: str) -> str | None:
    for name, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return name
    return None


def _ytdlp_extract(query_or_url: str, audio_only: bool):
    opts = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s_%(epoch)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG_PATH,
        "max_filesize": MAX_UPLOAD_BYTES,
    }
    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        opts["format"] = "best[ext=mp4]/best"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query_or_url, download=True)
        if "entries" in info:
            info = info["entries"][0]
        filename = ydl.prepare_filename(info)
        if audio_only:
            filename = os.path.splitext(filename)[0] + ".mp3"
        return filename, info


async def search_music(query: str):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _ytdlp_extract, f"ytsearch1:{query}", True)


async def download_media(url: str, audio_only: bool):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _ytdlp_extract, url, audio_only)


def _save_image_bytes(image_url: str) -> str:
    resp = requests.get(image_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex}.jpg")
    with open(path, "wb") as f:
        f.write(resp.content)
    return path


def _download_photo_sync(url: str) -> str:
    try:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            thumb = info.get("thumbnail")
            if not thumb and info.get("thumbnails"):
                thumb = info["thumbnails"][-1]["url"]
            if thumb:
                return _save_image_bytes(thumb)
    except Exception as e:
        logger.info("yt-dlp orqali rasm topilmadi, og:image'ga o'tilmoqda: %s", e)

    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    match = re.search(r'<meta property="og:image" content="([^"]+)"', resp.text)
    if not match:
        raise RuntimeError("Rasm topilmadi.")
    return _save_image_bytes(match.group(1))


async def download_photo(url: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_photo_sync, url)


async def convert_to_round(src_path: str, out_path: str):
    cmd = [
        FFMPEG_PATH, "-y", "-i", src_path,
        "-t", "60",
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=384:384",
        "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore")[-500:])


def set_audio_tags(path: str, title: str, artist: str):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags["TIT2"] = TIT2(encoding=3, text=title)
    tags["TPE1"] = TPE1(encoding=3, text=artist)
    tags.save(path)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Salom! 👋\n\n"
        "🎵 Musiqa nomini yozing (shaxsiy chatda) - men uni topib beraman.\n"
        "🔗 Instagram, TikTok, Pinterest yoki YouTube havolasini tashlang - "
        "video, rasm yoki musiqa qilib beraman (tanlaysiz).\n"
        "🎥 Video yuboring - aylana video (video note) qilib qaytaraman.\n"
        "🎧 Musiqa fayl yuboring - nomi va ijrochisini o'zgartirib beraman.\n\n"
        "Guruhda ishlashim uchun meni <b>admin</b> qiling (aks holda ba'zi "
        "xabarlarni ko'ra olmayman)."
    )


@dp.message(F.text.regexp(r"^\.id$"))
async def cmd_id(message: Message):
    if message.chat.type == "private":
        return await message.reply("Bu buyruq faqat guruhda, foydalanuvchiga reply qilib ishlaydi.")
    if not message.reply_to_message or not message.reply_to_message.from_user:
        return await message.reply("Foydalanuvchiga reply qilib .id deb yozing.")
    user = message.reply_to_message.from_user
    username = f"@{user.username}" if user.username else "(username yo'q)"
    await message.reply(
        f"👤 {user.full_name}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"Username: {username}"
    )


@dp.message(F.video)
async def handle_video(message: Message):
    status = await message.reply("🔄 Aylana videoga aylantirilmoqda...")
    src_path = os.path.join(DOWNLOAD_DIR, f"{message.video.file_unique_id}_src.mp4")
    out_path = os.path.join(DOWNLOAD_DIR, f"{message.video.file_unique_id}_round.mp4")
    try:
        file = await bot.get_file(message.video.file_id)
        await bot.download_file(file.file_path, destination=src_path)
        await convert_to_round(src_path, out_path)
        await message.reply_video_note(FSInputFile(out_path))
        await status.delete()
    except Exception as e:
        logger.exception("Video note konvertatsiyasida xatolik")
        await status.edit_text(f"❌ Xatolik: {e}")
    finally:
        _cleanup(src_path, out_path)


@dp.message(F.audio | (F.document & F.document.mime_type.startswith("audio/")))
async def handle_audio(message: Message, state: FSMContext):
    audio = message.audio or message.document
    file = await bot.get_file(audio.file_id)
    path = os.path.join(DOWNLOAD_DIR, f"{audio.file_unique_id}.mp3")
    await bot.download_file(file.file_path, destination=path)
    pending_audio[message.from_user.id] = path
    await state.set_state(TagStates.waiting_title)
    await message.reply("🎼 Yangi nom (sarlavha)ni yuboring:")


@dp.message(TagStates.waiting_title)
async def receive_title(message: Message, state: FSMContext):
    if not message.text:
        return await message.reply("Iltimos, matn ko'rinishida nom yuboring.")
    await state.update_data(new_title=message.text.strip())
    await state.set_state(TagStates.waiting_artist)
    await message.reply("🎤 Endi ijrochi (muallif) nomini yuboring:")


@dp.message(TagStates.waiting_artist)
async def receive_artist(message: Message, state: FSMContext):
    if not message.text:
        return await message.reply("Iltimos, matn ko'rinishida ijrochi nomini yuboring.")
    data = await state.get_data()
    title = data.get("new_title", "")
    artist = message.text.strip()
    path = pending_audio.pop(message.from_user.id, None)
    await state.clear()
    if not path or not os.path.exists(path):
        return await message.reply("Xatolik: fayl topilmadi, musiqani qaytadan yuboring.")
    try:
        set_audio_tags(path, title, artist)
        await message.reply_audio(FSInputFile(path), title=title, performer=artist)
    except Exception as e:
        logger.exception("Teglarni o'zgartirishda xatolik")
        await message.reply(
            f"❌ Teglarni o'zgartirib bo'lmadi (faqat MP3 qo'llab-quvvatlanadi): {e}"
        )
    finally:
        _cleanup(path)


async def handle_link(message: Message, url: str):
    platform = detect_platform(url)
    if not platform:
        return await message.reply(
            "Bu havola qo'llab-quvvatlanmaydi. Instagram, TikTok, Pinterest yoki "
            "YouTube havolasini yuboring."
        )
    token = uuid.uuid4().hex[:12]
    pending_links[token] = url
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎥 Video", callback_data=f"dl:video:{token}"),
        InlineKeyboardButton(text="🖼 Rasm", callback_data=f"dl:photo:{token}"),
        InlineKeyboardButton(text="🎵 Musiqa", callback_data=f"dl:audio:{token}"),
    ]])
    await message.reply(f"{platform} havolasi aniqlandi. Nimani yuklab beray?", reply_markup=kb)


@dp.callback_query(F.data.startswith("dl:"))
async def on_download_choice(callback: CallbackQuery):
    _, media_type, token = callback.data.split(":", 2)
    url = pending_links.pop(token, None)
    if not url:
        return await callback.answer("Havola muddati tugagan, qaytadan yuboring.", show_alert=True)

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    status = await callback.message.reply("⏳ Yuklab olinmoqda...")
    path = None
    try:
        if media_type == "audio":
            path, info = await download_media(url, audio_only=True)
            await callback.message.answer_audio(
                FSInputFile(path), title=info.get("title"), performer=info.get("uploader")
            )
        elif media_type == "video":
            path, info = await download_media(url, audio_only=False)
            await callback.message.answer_video(FSInputFile(path))
        else:
            path = await download_photo(url)
            await callback.message.answer_photo(FSInputFile(path))
        await status.delete()
    except Exception as e:
        logger.exception("Yuklab olishda xatolik")
        await status.edit_text(f"❌ Yuklab bo'lmadi: {e}")
    finally:
        _cleanup(path)


@dp.message(F.text)
async def handle_text(message: Message):
    text = message.text.strip()
    if text.startswith("/") or text.startswith("."):
        return

    urls = URL_RE.findall(text)
    if urls:
        return await handle_link(message, urls[0])

    if message.chat.type != "private":
        return  # guruhda spam bo'lmasligi uchun erkin matn orqali qidiruv o'chirilgan

    status = await message.reply("🔎 Qidirilmoqda...")
    path = None
    try:
        path, info = await search_music(text)
        await message.reply_audio(
            FSInputFile(path), title=info.get("title"), performer=info.get("uploader")
        )
        await status.delete()
    except Exception as e:
        logger.exception("Musiqa qidirishda xatolik")
        await status.edit_text(f"❌ Topilmadi: {e}")
    finally:
        _cleanup(path)


fake_server = Flask(__name__)


@fake_server.route("/")
def home():
    return "Music bot ishlab turibdi."


def run_fake_server():
    port = int(os.getenv("PORT", 8080))
    fake_server.run(host="0.0.0.0", port=port)


async def main():
    threading.Thread(target=run_fake_server, daemon=True).start()
    logger.info("Music bot ishga tushdi.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
