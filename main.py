import os
import re
import html
import time
import json
import sqlite3
import asyncio
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from flask import Flask
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, UsernameNotOccupiedError, UsernameInvalidError
from telethon.sessions import StringSession
from telethon.tl.functions.account import UpdateUsernameRequest
from telethon.tl.functions.channels import EditAdminRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import ChatAdminRights

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = "antidelete_session"
SESSION_STRING = os.getenv("SESSION_STRING", "")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
DAILY_SUMMARY_HOUR = 0
DAILY_SUMMARY_MINUTE = 5
TARGET_LANG = "uz"
TASHKENT_TZ = ZoneInfo("Asia/Tashkent")

CACHE_DIR = "/tmp/antidelete_cache"
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_DB_FILE = os.path.join(CACHE_DIR, "message_cache.db")
WATCHED_USERNAMES_FILE = os.path.join(CACHE_DIR, "watched_usernames.json")
WATCHED_PROFILES_FILE = os.path.join(CACHE_DIR, "watched_profiles.json")
USERNAME_CHECK_INTERVAL = 15  # soniya
PROFILE_CHECK_INTERVAL = 30  # soniya
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

# Render kabi vaqtinchalik disk muhitida fayl sessiyasi har deploy'da yo'qoladi,
# shuning uchun SESSION_STRING mavjud bo'lsa o'shani ishlatamiz (generate_session.py bilan olinadi).
if SESSION_STRING:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
db_conn = sqlite3.connect(CACHE_DB_FILE, check_same_thread=False)
db_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS messages (
        chat_id INTEGER NOT NULL,
        msg_id INTEGER NOT NULL,
        sender_id INTEGER,
        text TEXT,
        media_path TEXT,
        media_kind TEXT,
        is_private INTEGER,
        date TEXT,
        cached_at REAL,
        PRIMARY KEY (chat_id, msg_id)
    )
    """
)
db_conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_msg_private ON messages (msg_id, is_private)")
db_conn.commit()


def cache_message_row(chat_id, msg_id, sender_id, text, media_path, media_kind, is_private, date):
    """Diskdagi SQLite bazaga yozadi - RAM'ga qaramaydi va vaqt/miqdor bo'yicha
    chegaralanmagan (cheksiz) saqlaydi. Konteyner butunlay qayta qurilganda
    (masalan Render'da yangi deploy) diskning o'zi ham tozalanadi - bu holatda
    baza baribir bo'sh boshlanadi, buni faqat doimiy disk yoki tashqi baza bilan
    oldini olish mumkin."""
    db_conn.execute(
        "INSERT OR REPLACE INTO messages "
        "(chat_id, msg_id, sender_id, text, media_path, media_kind, is_private, date, cached_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chat_id, msg_id, sender_id, text, media_path, media_kind, int(bool(is_private)),
            date.isoformat() if date else None, time.time(),
        ),
    )
    db_conn.commit()


def pop_cached_message(msg_id, chat_id):
    """chat_id ma'lum bo'lsa (guruh/kanal o'chirish hodisasi) aynan shu chat
    bo'yicha, aks holda (shaxsiy xabar o'chirilganda Telegram qaysi chatligini
    bermaydi) faqat shaxsiy xabarlar orasidan msg_id bo'yicha qidiradi -
    shunda guruh xabari bilan tasodifiy id to'qnashuvi bo'lmaydi."""
    cur = db_conn.cursor()
    if chat_id is not None:
        cur.execute("SELECT * FROM messages WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id))
    else:
        cur.execute("SELECT * FROM messages WHERE msg_id = ? AND is_private = 1", (msg_id,))
    row = cur.fetchone()
    if not row:
        return None
    columns = [c[0] for c in cur.description]
    data = dict(zip(columns, row))
    db_conn.execute("DELETE FROM messages WHERE chat_id = ? AND msg_id = ?", (data["chat_id"], data["msg_id"]))
    db_conn.commit()
    data["date"] = datetime.fromisoformat(data["date"]) if data.get("date") else None
    return data


def get_cached_row(chat_id, msg_id):
    """pop_cached_message'dan farqli - o'chirmaydi, faqat o'qiydi (tahrirni
    aniqlash uchun eski matnni bilish kerak, lekin xabar o'zi hali chatda bor)."""
    cur = db_conn.cursor()
    cur.execute("SELECT * FROM messages WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id))
    row = cur.fetchone()
    if not row:
        return None
    columns = [c[0] for c in cur.description]
    data = dict(zip(columns, row))
    data["date"] = datetime.fromisoformat(data["date"]) if data.get("date") else None
    return data

watched_usernames = {}


def save_watched_usernames():
    try:
        with open(WATCHED_USERNAMES_FILE, "w", encoding="utf-8") as f:
            json.dump(watched_usernames, f)
    except Exception as e:
        print(f"Kuzatilayotgan username'larni saqlashda xatolik: {e}")


def load_watched_usernames():
    if not os.path.exists(WATCHED_USERNAMES_FILE):
        return
    try:
        with open(WATCHED_USERNAMES_FILE, "r", encoding="utf-8") as f:
            watched_usernames.update(json.load(f))
        print(f"Kuzatilayotgan username'lar tiklandi: {len(watched_usernames)} ta.")
    except Exception as e:
        print(f"Kuzatilayotgan username'larni yuklashda xatolik: {e}")


watched_profiles = {}


def save_watched_profiles():
    try:
        with open(WATCHED_PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(watched_profiles, f)
    except Exception as e:
        print(f"Kuzatilayotgan profillarni saqlashda xatolik: {e}")


def load_watched_profiles():
    if not os.path.exists(WATCHED_PROFILES_FILE):
        return
    try:
        with open(WATCHED_PROFILES_FILE, "r", encoding="utf-8") as f:
            watched_profiles.update(json.load(f))
        print(f"Kuzatilayotgan profillar tiklandi: {len(watched_profiles)} ta.")
    except Exception as e:
        print(f"Kuzatilayotgan profillarni yuklashda xatolik: {e}")


load_watched_usernames()
load_watched_profiles()


def mention_html(name, user_id):
    safe_name = html.escape(name or "Noma'lum")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'




def is_media_message(message):
    return bool(message.photo or message.video or message.video_note or message.voice or message.sticker)


MEDIA_KIND_EMOJI = {
    "video_note": "⭕",
    "voice": "🎤",
    "sticker": "🧩",
    "video": "🎥",
    "photo": "🖼",
}
BOT_API_METHOD_BY_KIND = {
    "video_note": ("sendVideoNote", "video_note"),
    "voice": ("sendVoice", "voice"),
    "sticker": ("sendSticker", "sticker"),
    "video": ("sendVideo", "video"),
    "photo": ("sendPhoto", "photo"),
}


def message_media_kind(message):
    if message.video_note:
        return "video_note"
    if message.voice:
        return "voice"
    if message.sticker:
        return "sticker"
    if message.video:
        return "video"
    if message.photo:
        return "photo"
    return None


def message_kind_emoji(message):
    return MEDIA_KIND_EMOJI.get(message_media_kind(message), "💬")


def send_log_message(caption, media_path=None, media_kind=None):
    """LOG_CHANNEL_ID'ga Bot API orqali yuboradi (Telethon/MTProto orqali emas) -
    shunda tg://user?id= mention'lari yuboruvchining maxfiylik sozlamasidan
    qat'iy nazar har doim bosiladigan bo'lib qoladi. Botni kanalga admin
    qilib qo'shish kerak."""
    if not BOT_TOKEN or not LOG_CHANNEL_ID:
        return
    try:
        if media_path and os.path.exists(media_path):
            method, field = BOT_API_METHOD_BY_KIND.get(media_kind, ("sendDocument", "document"))
            with open(media_path, "rb") as f:
                if method == "sendSticker":
                    resp = requests.post(
                        f"{BOT_API_URL}/sendSticker",
                        data={"chat_id": LOG_CHANNEL_ID},
                        files={"sticker": f},
                        timeout=60,
                    )
                else:
                    resp = requests.post(
                        f"{BOT_API_URL}/{method}",
                        data={"chat_id": LOG_CHANNEL_ID, "caption": caption, "parse_mode": "HTML"},
                        files={field: f},
                        timeout=60,
                    )
            if not resp.ok:
                print(f"Bot API {method} xatolik: {resp.status_code} {resp.text}")
            if method == "sendSticker":
                bot_api_send_message(caption)
            try:
                os.remove(media_path)
            except Exception:
                pass
        else:
            bot_api_send_message(caption)
    except Exception as e:
        print(f"Bot API'ga yuborishda xatolik: {e}")


def bot_api_send_message(text):
    try:
        resp = requests.post(
            f"{BOT_API_URL}/sendMessage",
            data={"chat_id": LOG_CHANNEL_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if not resp.ok:
            print(f"Bot API sendMessage xatolik: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Bot API sendMessage so'rovida xatolik: {e}")


async def download_to_disk(message):
    """Media faylni RAM emas, diskka yuklaydi - xotira sarfini kamaytirish uchun."""
    try:
        if message.video_note:
            ext = "mp4"
        elif message.voice:
            ext = "ogg"
        elif message.sticker:
            ext = "webp"
        elif message.video:
            ext = "mp4"
        elif message.photo:
            ext = "jpg"
        else:
            ext = "bin"

        file_path = os.path.join(CACHE_DIR, f"{message.chat_id}_{message.id}_{int(time.time())}.{ext}")
        saved_path = await client.download_media(message, file=file_path)
        return saved_path
    except Exception as e:
        print(f"Media yuklashda xatolik: {e}")
        return None


@client.on(events.NewMessage())
async def cache_messages(event):
    """Shaxsiy va guruh xabarlarini bazaga keshlaydi - anti-delete cheksiz
    muddatga ishlashi uchun (disk to'lguncha)."""
    media_path = None
    if event.media and is_media_message(event.message):
        media_path = await download_to_disk(event.message)

    cache_message_row(
        chat_id=event.chat_id,
        msg_id=event.id,
        sender_id=event.sender_id,
        text=event.raw_text,
        media_path=media_path,
        media_kind=message_media_kind(event.message) if event.media else None,
        is_private=event.is_private,
        date=event.date,
    )


@client.on(events.MessageDeleted())
async def on_message_deleted(event):
    if not LOG_CHANNEL_ID:
        return
    for msg_id in event.deleted_ids:
        data = pop_cached_message(msg_id, event.chat_id)
        if not data or not data.get("is_private"):
            continue
        sender_id = data["sender_id"]
        try:
            sender = await client.get_entity(sender_id) if sender_id else None
        except Exception as e:
            print(f"Yuboruvchini aniqlashda xatolik: {e}")
            sender = None
        sender_name = getattr(sender, "first_name", None) or "Noma'lum"
        sender_username = getattr(sender, "username", None)
        user_part = f"@{sender_username}" if sender_username else "yo'q"
        sender_link = mention_html(sender_name, sender_id) if sender_id else "Noma'lum"
        id_part = f" (id={sender_id} user={user_part})" if sender_id else ""

        caption = (
            f"#delete\n"
            f"O'chirilgan shaxsiy xabar\n"
            f"{MEDIA_KIND_EMOJI.get(data.get('media_kind'), '💬')} Kimdan: {sender_link}{id_part}\n"
            f"Vaqt: {data['date'].astimezone(TASHKENT_TZ).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Matn: {html.escape(data['text']) if data['text'] else '(matn yoq)'}"
        )
        send_log_message(caption, data["media_path"], data.get("media_kind"))


@client.on(events.MessageEdited())
async def on_message_edited(event):
    if not LOG_CHANNEL_ID:
        return
    old = get_cached_row(event.chat_id, event.id)
    new_text = event.raw_text or ""
    old_text = old["text"] if old else None

    cache_message_row(
        chat_id=event.chat_id,
        msg_id=event.id,
        sender_id=event.sender_id,
        text=new_text,
        media_path=old["media_path"] if old else None,
        media_kind=old["media_kind"] if old else (message_media_kind(event.message) if event.media else None),
        is_private=event.is_private,
        date=old["date"] if old and old.get("date") else event.date,
    )

    if not event.is_private or not old_text or old_text == new_text:
        return

    sender_id = event.sender_id
    try:
        sender = await client.get_entity(sender_id) if sender_id else None
    except Exception as e:
        print(f"Yuboruvchini aniqlashda xatolik: {e}")
        sender = None
    sender_name = getattr(sender, "first_name", None) or "Noma'lum"
    sender_username = getattr(sender, "username", None)
    user_part = f"@{sender_username}" if sender_username else "yo'q"
    sender_link = mention_html(sender_name, sender_id) if sender_id else "Noma'lum"
    id_part = f" (id={sender_id} user={user_part})" if sender_id else ""

    caption = (
        f"#edit\n"
        f"Tahrirlangan shaxsiy xabar\n"
        f"✏️ Kimdan: {sender_link}{id_part}\n"
        f"Vaqt: {datetime.now(TASHKENT_TZ).strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Eski matn: {html.escape(old_text)}\n"
        f"Yangi matn: {html.escape(new_text) if new_text else '(matn yoq)'}"
    )
    send_log_message(caption)


# Media reply orqali saqlash - shaxsiy chat, guruh va kanallarda ishlaydi
@client.on(events.NewMessage(outgoing=True))
async def save_media_via_reply(event):
    if not LOG_CHANNEL_ID:
        return
    if not event.is_reply:
        return
    reply = await event.get_reply_message()
    if not reply or not reply.media or reply.out:
        return
    if not is_media_message(reply):
        return
    is_video = bool(reply.video)
    is_round = bool(reply.video_note)
    is_voice = bool(reply.voice)
    is_sticker = bool(reply.sticker)
    try:
        sender = await reply.get_sender()
        sender_name = getattr(sender, "first_name", None) or "Noma'lum"
        sender_username = getattr(sender, "username", None)
        sender_id = getattr(sender, "id", None) or reply.sender_id
        user_part = f"@{sender_username}" if sender_username else "yo'q"
        sender_link = mention_html(sender_name, sender_id) if sender_id else "Noma'lum"
        id_part = f" (id={sender_id} user={user_part})" if sender_id else ""
        if is_round:
            kind = "Aylana video"
        elif is_voice:
            kind = "Ovozli xabar"
        elif is_sticker:
            kind = "Stiker"
        elif is_video:
            kind = "Video"
        else:
            kind = "Rasm"
        caption = f"#reply\n{kind} (reply orqali saqlandi)\n{message_kind_emoji(reply)} Kimdan: {sender_link}{id_part}"
        media_path = await download_to_disk(reply)
        if not media_path:
            print("Reply media: yuklab bolmadi (bo'sh)")
            return
        send_log_message(caption, media_path, message_media_kind(reply))
    except Exception as e:
        print(f"Reply orqali media saqlashda xatolik: {e}")


# .ad / .ад - guruh admin + teg
@client.on(events.NewMessage(pattern=r"^\.(ad|ад)(?:\s+(.+))?$", outgoing=True))
async def add_admin_tag(event):
    if event.is_private:
        return await event.edit("Bu buyruq faqat guruhda ishlaydi.")
    reply = await event.get_reply_message()
    if not reply or not getattr(reply, "sender_id", None):
        return await event.edit("Reply qiling: .ad teg_matni")
    tag_text = event.pattern_match.group(2) or ""
    target_id = reply.sender_id
    try:
        rights = ChatAdminRights(
            add_admins=False, invite_users=True, change_info=False,
            ban_users=False, delete_messages=True, pin_messages=True, manage_call=True,
        )
        await client(EditAdminRequest(event.chat_id, target_id, rights, tag_text or "Admin"))
        await event.edit(f"Admin qilindi. Teg: {tag_text or '(bosh)'}")
    except Exception as e:
        await event.edit(f"Xatolik: {e}")


# .unad / .унад
@client.on(events.NewMessage(pattern=r"^\.(unad|унад)$", outgoing=True))
async def remove_admin_tag(event):
    if event.is_private:
        return await event.edit("Bu buyruq faqat guruhda ishlaydi.")
    reply = await event.get_reply_message()
    if not reply or not getattr(reply, "sender_id", None):
        return await event.edit("Reply qiling: .unad")
    target_id = reply.sender_id
    try:
        empty_rights = ChatAdminRights(
            add_admins=False, invite_users=False, change_info=False,
            ban_users=False, delete_messages=False, pin_messages=False, manage_call=False,
        )
        await client(EditAdminRequest(event.chat_id, target_id, empty_rights, ""))
        await event.edit("Admin va teg olib tashlandi.")
    except Exception as e:
        await event.edit(f"Xatolik: {e}")


async def safe_delete_messages(chat_id, ids_batch, max_retries=3):
    """FloodWait xatoligini avtomatik kutib, qayta urinadi - .delete ketma-ket ishlashi uchun."""
    for attempt in range(max_retries):
        try:
            await client.delete_messages(chat_id, ids_batch)
            return True
        except FloodWaitError as e:
            wait_time = e.seconds + 2
            print(f".delete: FloodWait, {wait_time} soniya kutilmoqda...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            print(f".delete xatolik: {e}")
            return False
    return False


# .delete / .делете - o'zining barcha xabarlarini o'chiradi (guruh + shaxsiy), yakuniy xabarsiz
@client.on(events.NewMessage(pattern=r"^\.(delete|делете)$", outgoing=True))
async def delete_my_messages(event):
    chat_id = event.chat_id
    me = await client.get_me()

    try:
        await event.delete()
    except Exception:
        pass

    ids_batch = []
    try:
        async for msg in client.iter_messages(chat_id, from_user=me.id):
            ids_batch.append(msg.id)
            if len(ids_batch) == 100:
                await safe_delete_messages(chat_id, ids_batch)
                ids_batch = []
                await asyncio.sleep(1)
    except FloodWaitError as e:
        wait_time = e.seconds + 2
        print(f".delete: FloodWait (iter), {wait_time} soniya kutilmoqda...")
        await asyncio.sleep(wait_time)

    if ids_batch:
        await safe_delete_messages(chat_id, ids_batch)


# .del / .дел - reply qilingan xabar + buyruqning o'zi o'chadi
@client.on(events.NewMessage(pattern=r"^\.(del|дел)$", outgoing=True))
async def delete_replied_message(event):
    reply = await event.get_reply_message()
    if not reply:
        try:
            await event.delete()
        except Exception:
            pass
        return

    try:
        await safe_delete_messages(event.chat_id, [reply.id, event.id])
    except Exception:
        try:
            await event.delete()
        except Exception:
            pass


# .tarjima / .таржима
@client.on(events.NewMessage(pattern=r"^\.(tarjima|таржима)$", outgoing=True))
async def translate_message(event):
    reply = await event.get_reply_message()
    if not reply or not reply.raw_text:
        return await event.edit("Tarjima qilinadigan xabarga reply qiling.")
    await event.edit("Tarjima qilinmoqda...")
    try:
        resp = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": TARGET_LANG, "dt": "t", "q": reply.raw_text},
            timeout=10,
        )
        result = resp.json()
        translated = "".join([seg[0] for seg in result[0]])
        await event.edit(f"Tarjima:\n{translated}")
    except Exception as e:
        await event.edit(f"Tarjima xatoligi: {e}")


@client.on(events.NewMessage(pattern=r"^\.(ai|аи)(?:\s+([\s\S]+))?$", outgoing=True))
async def ask_ai(event):
    if not GEMINI_API_KEY:
        return await event.edit("GEMINI_API_KEY sozlanmagan.")

    question = event.pattern_match.group(2)
    if not question:
        reply = await event.get_reply_message()
        if reply and reply.raw_text:
            question = reply.raw_text
    if not question:
        return await event.edit("Savol yozing: .ai savolingiz")

    await event.edit("O'ylanmoqda...")
    answer = await asyncio.to_thread(call_gemini, question)
    if not answer:
        return await event.edit("AI javob bera olmadi, keyinroq urinib ko'ring.")
    await event.edit(f"🤖 {answer}"[:4096])


@client.on(events.NewMessage(pattern=r"^\.(tek|тек)(?:\s+(.+))?$", outgoing=True))
async def check_mutual_contact(event):
    from telethon.tl.functions.contacts import AddContactRequest, DeleteContactsRequest
    from telethon.tl.types import User as TgUser

    target = event.pattern_match.group(2)
    entity = None

    if not target:
        reply = await event.get_reply_message()
        if reply and reply.sender_id:
            # Reply orqali entity'ni to'g'ridan-to'g'ri olamiz -
            # bu bare ID orqali get_entity() qilishga qaraganda ishonchli,
            # chunki access_hash reply xabaridan avtomatik olinadi.
            entity = await reply.get_sender()
        else:
            return await event.edit("Foydalanish: .tek @username yoki .tek ID (yoki reply qiling)")

    await event.edit("Tekshirilmoqda...")
    temporarily_added = False
    try:
        if entity is None:
            entity = await client.get_entity(target.strip())

        if not isinstance(entity, TgUser):
            return await event.edit(
                "Bu buyruq faqat oddiy foydalanuvchilar uchun ishlaydi "
                "(kanal yoki anonim admin uchun ishlamaydi)."
            )

        full = await client(GetFullUserRequest(entity))
        user = full.users[0] if full.users else entity
        was_contact = getattr(user, "contact", False)

        # Agar hali kontaktda bo'lmasa, vaqtincha qo'shib, natijani tekshiramiz,
        # keyin darhol olib tashlaymiz - shunda doimiy ravishda hech kim qo'shilib qolmaydi.
        if not was_contact:
            try:
                add_result = await client(AddContactRequest(
                    id=entity, first_name=getattr(user, "first_name", None) or "Tekshiruv",
                    last_name="", phone="",
                ))
                if add_result.users:
                    user = add_result.users[0]
                temporarily_added = True
            except Exception as e:
                print(f".tek: vaqtincha qoshishda xatolik: {e}")

        name = getattr(user, "first_name", None) or "Noma'lum"
        username = getattr(user, "username", None)
        user_id = getattr(user, "id", None) or entity.id
        is_mutual = getattr(user, "mutual_contact", False)

        if temporarily_added:
            try:
                await client(DeleteContactsRequest(id=[entity]))
            except Exception as e:
                print(f".tek: vaqtincha kontaktni olib tashlashda xatolik: {e}")

        if is_mutual:
            status = "U sizni ham kontaktiga qo'shgan (o'zaro)."
        else:
            status = "U sizni kontaktiga qo'shmagan."

        name_link = mention_html(name, user_id)
        username_part = f"@{username}" if username else "(username yoq)"

        result_text = (
            f"{name_link}\n"
            f"Username: {username_part}\n"
            f"ID: {user_id}\n"
            f"{status}"
        )
        await event.edit(result_text, parse_mode="html")
    except Exception as e:
        if temporarily_added and entity is not None:
            try:
                await client(DeleteContactsRequest(id=[entity]))
            except Exception:
                pass
        await event.edit(f"Xatolik: {e}")


async def _resolve_username_status(username):
    """Muvaffaqiyatli qaytsa - username hali band. Bo'sh bo'lsa
    UsernameNotOccupiedError, yaroqsiz bo'lsa UsernameInvalidError ko'taradi."""
    await client(ResolveUsernameRequest(username))


# .user / .unuser - biror username bo'shab qolganida LOG_CHANNEL_ID'ga xabar beradi
@client.on(events.NewMessage(pattern=r"^\.user(?:\s+(.+))?$", outgoing=True))
async def watch_username(event):
    raw = (event.pattern_match.group(1) or "").strip().lstrip("@")
    if not raw:
        return await event.edit("Foydalanish: .user @username")
    username = raw.lower()
    if not USERNAME_RE.match(username):
        return await event.edit("Username formati noto'g'ri (5-32 belgi, harf bilan boshlanishi kerak).")
    if not LOG_CHANNEL_ID:
        return await event.edit("LOG_CHANNEL_ID sozlanmagan, bildirishnoma yuborib bo'lmaydi.")
    if username in watched_usernames:
        return await event.edit(f"@{username} allaqachon kuzatuvda.")
    try:
        await _resolve_username_status(username)
    except UsernameNotOccupiedError:
        return await event.edit(f"@{username} hozirning o'zida bo'sh ekan, kuzatuvga hojat yo'q.")
    except UsernameInvalidError:
        return await event.edit("Bunday username mavjud emas.")
    except Exception as e:
        return await event.edit(f"Xatolik: {e}")

    watched_usernames[username] = {"added_at": time.time()}
    save_watched_usernames()
    await event.edit(
        f"👀 @{username} kuzatuvga qo'shildi (hozir band). Bo'shab qolishi bilan (har "
        f"{USERNAME_CHECK_INTERVAL} soniyada tekshiraman) avtomatik profilingizga o'rnataman "
        f"va {LOG_CHANNEL_ID}'ga xabar beraman."
    )


@client.on(events.NewMessage(pattern=r"^\.unuser(?:\s+(.+))?$", outgoing=True))
async def unwatch_username(event):
    raw = (event.pattern_match.group(1) or "").strip().lstrip("@")
    username = raw.lower()
    if not username or username not in watched_usernames:
        return await event.edit("Bu username kuzatuvda emas edi.")
    watched_usernames.pop(username, None)
    save_watched_usernames()
    await event.edit(f"@{username} kuzatuvdan olib tashlandi.")


async def _resolve_watch_target(event, arg):
    """Argument (username yoki id) yoki reply orqali foydalanuvchi entity'sini topadi."""
    if arg:
        arg = arg.strip().lstrip("@")
        try:
            target = int(arg) if arg.lstrip("-").isdigit() else arg
            return await client.get_entity(target)
        except Exception:
            return None
    reply = await event.get_reply_message()
    if reply and reply.sender_id:
        try:
            return await reply.get_sender()
        except Exception:
            return None
    return None


# .watch / .unwatch - kuzatilayotgan foydalanuvchining ism/bio/profil rasmi
# o'zgarishini LOG_CHANNEL_ID'ga xabar beradi
@client.on(events.NewMessage(pattern=r"^\.watch(?:\s+(.+))?$", outgoing=True))
async def watch_profile(event):
    if not LOG_CHANNEL_ID:
        return await event.edit("LOG_CHANNEL_ID sozlanmagan, bildirishnoma yuborib bo'lmaydi.")
    entity = await _resolve_watch_target(event, event.pattern_match.group(1))
    if not entity or not getattr(entity, "id", None):
        return await event.edit("Foydalanuvchi topilmadi. Reply qiling yoki: .watch @username")

    user_id = entity.id
    try:
        full = await client(GetFullUserRequest(entity))
        bio = full.full_user.about or ""
    except Exception:
        bio = ""
    name = f"{entity.first_name or ''} {entity.last_name or ''}".strip() or "Noma'lum"
    photo_id = getattr(entity.photo, "photo_id", None) if entity.photo else None

    watched_profiles[str(user_id)] = {
        "name": name,
        "bio": bio,
        "photo_id": photo_id,
        "username": getattr(entity, "username", None),
    }
    save_watched_profiles()
    await event.edit(
        f"👀 {mention_html(name, user_id)} profili kuzatuvga qo'shildi "
        f"(ism/bio/rasm o'zgarsa {PROFILE_CHECK_INTERVAL} soniyada bir tekshirib xabar beraman).",
        parse_mode="html",
    )


@client.on(events.NewMessage(pattern=r"^\.unwatch(?:\s+(.+))?$", outgoing=True))
async def unwatch_profile(event):
    entity = await _resolve_watch_target(event, event.pattern_match.group(1))
    user_id = getattr(entity, "id", None) if entity else None
    if not user_id or str(user_id) not in watched_profiles:
        return await event.edit("Bu foydalanuvchi kuzatuvda emas edi.")
    watched_profiles.pop(str(user_id), None)
    save_watched_profiles()
    await event.edit("Profil kuzatuvidan olib tashlandi.")


async def _claim_and_notify(username):
    """Bo'shab qolgan username'ni darhol profilga o'rnatishga urinadi va natijani
    LOG_CHANNEL_ID'ga yuboradi. Boshqa kimdir bir zumda ulgurib olgan bo'lishi mumkin -
    bu holat ham xabarda ko'rsatiladi."""
    try:
        await client(UpdateUsernameRequest(username))
        status_text = f"✅ @{username} muvaffaqiyatli sizning profilingizga o'rnatildi!"
    except Exception as e:
        status_text = f"⚠️ @{username} bo'shadi, lekin avtomatik o'rnatishda xatolik: {e}"
    bot_api_send_message(f"🎉 #username_boshaldi\n{status_text}")


async def check_watched_usernames():
    if not watched_usernames or not LOG_CHANNEL_ID:
        return
    for username in list(watched_usernames.keys()):
        try:
            await _resolve_username_status(username)
        except UsernameNotOccupiedError:
            # Bo'shagan zahoti kuzatuvdan chiqaramiz va darhol o'zlashtirishga urinamiz -
            # tezlik muhim bo'lgani uchun keyingi usernamega o'tishdan oldin shu yerda bajaramiz.
            watched_usernames.pop(username, None)
            save_watched_usernames()
            await _claim_and_notify(username)
        except UsernameInvalidError:
            print(f".user: @{username} - yaroqsiz username, kuzatuvdan olib tashlandi.")
            watched_usernames.pop(username, None)
            save_watched_usernames()
        except FloodWaitError as e:
            wait_time = e.seconds + 2
            print(f".user: FloodWait, {wait_time} soniya kutilmoqda...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            print(f".user: @{username} tekshirishda xatolik: {e}")
        await asyncio.sleep(1)  # so'rovlar orasida biroz kutish, flood'ga tushmaslik uchun


async def periodic_username_check():
    while True:
        await asyncio.sleep(USERNAME_CHECK_INTERVAL)
        await check_watched_usernames()


async def check_watched_profiles():
    if not watched_profiles or not LOG_CHANNEL_ID:
        return
    for uid_str in list(watched_profiles.keys()):
        snap = watched_profiles.get(uid_str, {})
        try:
            entity = await client.get_entity(int(uid_str))
            full = await client(GetFullUserRequest(entity))
            name = f"{entity.first_name or ''} {entity.last_name or ''}".strip() or "Noma'lum"
            bio = full.full_user.about or ""
            photo_id = getattr(entity.photo, "photo_id", None) if entity.photo else None

            changes = []
            if name != snap.get("name"):
                changes.append(f"Ism: {html.escape(snap.get('name') or '-')} → {html.escape(name)}")
            if bio != snap.get("bio"):
                changes.append(f"Bio: {html.escape(snap.get('bio') or '-')} → {html.escape(bio)}")
            if photo_id != snap.get("photo_id"):
                changes.append("Profil rasmi o'zgardi")

            if changes:
                sender_link = mention_html(name, entity.id)
                bot_api_send_message(f"🔔 #profil_ozgardi\n{sender_link}:\n" + "\n".join(changes))

            watched_profiles[uid_str] = {
                "name": name, "bio": bio, "photo_id": photo_id,
                "username": getattr(entity, "username", None),
            }
            save_watched_profiles()
        except FloodWaitError as e:
            wait_time = e.seconds + 2
            print(f".watch: FloodWait, {wait_time} soniya kutilmoqda...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            print(f".watch: {uid_str} tekshirishda xatolik: {e}")
        await asyncio.sleep(1)


async def periodic_profile_check():
    while True:
        await asyncio.sleep(PROFILE_CHECK_INTERVAL)
        await check_watched_profiles()


# .goff / .gon - profildagi StarGift'larni yashirish/qayta ko'rsatish
from telethon.tl.functions.payments import GetSavedStarGiftsRequest, SaveStarGiftRequest
from telethon.tl.types import InputSavedStarGiftUser


async def _fetch_all_saved_gifts():
    """'me' profilidagi barcha saqlangan hadyalarni paketlab to'liq yuklaydi."""
    offset = ''
    all_gifts = []
    while True:
        result = await client(GetSavedStarGiftsRequest(
            peer='me',
            offset=offset,
            limit=100,
        ))
        if not result.gifts:
            break
        all_gifts.extend(result.gifts)
        if getattr(result, 'next_offset', None):
            offset = result.next_offset
        else:
            break
    return all_gifts


# 1. 🙈 NECHTA BO'LSA BARCHASINI profildan yashirish buyrug'i: .goff
@client.on(events.NewMessage(pattern=r"^\.goff$", outgoing=True))
async def hide_all_gifts(event):
    await event.edit("🔄 Barcha hadyalar ro'yxati yuklanmoqda, kuting...")

    try:
        all_gifts = await _fetch_all_saved_gifts()

        if not all_gifts:
            await event.edit("📊 **Natija:** Profilingizda umuman hadyalar topilmadi.")
            return

        await event.edit(f"📦 Jami {len(all_gifts)} ta hadya topildi. Profildan berkitish boshlandi...")
        hidden_count = 0

        # 🔐 Topilgan barcha hadyalarni bittalab yashirib chiqamiz
        for saved_gift in all_gifts:
            if getattr(saved_gift, 'unsaved', False) == False and saved_gift.msg_id:
                await client(SaveStarGiftRequest(
                    stargift=InputSavedStarGiftUser(msg_id=saved_gift.msg_id),
                    unsave=True,
                ))
                hidden_count += 1
                await asyncio.sleep(1.5)  # Telegram blokiga tushmaslik uchun 1.5 soniya kutish xavfsizroq

        if hidden_count == 0:
            await event.edit("📊 **Natija:** Profilingizdagi barcha hadyalar allaqachon yashirib bo'lingan.")
        else:
            await event.edit(f"✅ **Muvaffaqiyatli yakunlandi!**\n📦 Profilingizdagi jami `{hidden_count}` ta hadya mutloq berkitildi.")

    except Exception as e:
        await event.edit(f"❌ Hadyalarni yashirishda xatolik yuz berdi: {str(e)}")


# 2. 🐵 BARCHA yashirilgan hadyalarni profilda qayta ko'rsatish buyrug'i: .gon
@client.on(events.NewMessage(pattern=r"^\.gon$", outgoing=True))
async def show_all_gifts(event):
    await event.edit("🔄 Barcha yashirilgan hadyalar ro'yxati yuklanmoqda...")

    try:
        all_gifts = await _fetch_all_saved_gifts()

        if not all_gifts:
            await event.edit("📊 **Natija:** Profilingizda hadyalar mavjud emas.")
            return

        await event.edit(f"📦 Jami {len(all_gifts)} ta hadya topildi. Profilga qaytarish boshlandi...")
        shown_count = 0

        for saved_gift in all_gifts:
            if getattr(saved_gift, 'unsaved', False) == True and saved_gift.msg_id:
                await client(SaveStarGiftRequest(
                    stargift=InputSavedStarGiftUser(msg_id=saved_gift.msg_id),
                    unsave=False,
                ))
                shown_count += 1
                await asyncio.sleep(1.5)

        if shown_count == 0:
            await event.edit("📊 **Natija:** Profilingizda yashirilgan hadyalar topilmadi (hammasi ochiq turibdi).")
        else:
            await event.edit(f"✅ **Muvaffaqiyatli yakunlandi!**\n🎉 Jami `{shown_count}` ta hadya profilingizda qayta ko'rinadigan qilindi.")

    except Exception as e:
        await event.edit(f"❌ Hadyalarni yoqishda xatolik: {str(e)}")


# Kunlik #xulosa - yozishmalarni Gemini AI orqali tahlil qilib, log kanalga yuboradi
def _tashkent_day_bounds_utc(days_ago):
    """Berilgan necha kun oldingi Toshkent kunining boshi/oxirini UTC'da qaytaradi."""
    target_date = (datetime.now(TASHKENT_TZ) - timedelta(days=days_ago)).date()
    start_local = datetime.combine(target_date, datetime.min.time(), tzinfo=TASHKENT_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), target_date


def fetch_day_messages(start_utc, end_utc):
    cur = db_conn.cursor()
    cur.execute(
        "SELECT chat_id, sender_id, text, media_kind FROM messages WHERE date >= ? AND date < ? ORDER BY chat_id, date",
        (start_utc.isoformat(), end_utc.isoformat()),
    )
    return cur.fetchall()


def call_gemini(prompt):
    try:
        resp = requests.post(
            GEMINI_API_URL,
            params={"key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        if not resp.ok:
            print(f"Gemini API xatolik: {resp.status_code} {resp.text}")
            return None
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"Gemini API so'rovida xatolik: {e}")
        return None


async def generate_daily_summary():
    if not GEMINI_API_KEY or not LOG_CHANNEL_ID:
        return
    start_utc, end_utc, target_date = _tashkent_day_bounds_utc(days_ago=1)
    rows = fetch_day_messages(start_utc, end_utc)
    if not rows:
        return

    me = await client.get_me()
    my_id = me.id

    chats = {}
    for chat_id, sender_id, text, media_kind in rows:
        if not text and not media_kind:
            continue
        label = "Men" if sender_id == my_id else f"ID:{sender_id}"
        content = text or f"[{media_kind}]"
        chats.setdefault(chat_id, []).append(f"{label}: {content}")

    transcript_parts = []
    total_chars = 0
    max_chars = 12000
    for chat_id, lines in chats.items():
        chunk = f"--- Chat {chat_id} ---\n" + "\n".join(lines) + "\n"
        if total_chars + len(chunk) > max_chars:
            break
        transcript_parts.append(chunk)
        total_chars += len(chunk)

    prompt = (
        f"Quyida {target_date} kuni (Toshkent vaqti) bo'lgan Telegram yozishmalarim bor "
        "(\"Men\" - o'zim, \"ID:...\" - suhbatdoshlar). Menga shu kunlik faoliyatim haqida "
        "qisqa, umumiy xulosa yoz (o'zbek tilida, 5-8 gap): asosiy mavzular, kim bilan qanday "
        "muloqot bo'lgani, umumiy kayfiyat. Faqat xulosani yoz, boshqa hech narsa qo'shma.\n\n"
        + "\n".join(transcript_parts)
    )

    summary_text = call_gemini(prompt)
    if not summary_text:
        return

    message = f"#xulosa\n📅 {target_date} kunlik xulosa\n\n{html.escape(summary_text)}"
    bot_api_send_message(message[:4000])


async def daily_summary_loop():
    while True:
        now = datetime.now(TASHKENT_TZ)
        target = now.replace(hour=DAILY_SUMMARY_HOUR, minute=DAILY_SUMMARY_MINUTE, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await generate_daily_summary()
        except Exception as e:
            print(f"Kunlik xulosa yaratishda xatolik: {e}")


fake_server = Flask(__name__)


@fake_server.route("/")
def home():
    return "Bot ishlab turibdi."


def run_fake_server():
    port = int(os.getenv("PORT", 8080))
    fake_server.run(host="0.0.0.0", port=port)


async def main():
    threading.Thread(target=run_fake_server, daemon=True).start()
    asyncio.create_task(periodic_username_check())
    asyncio.create_task(periodic_profile_check())
    asyncio.create_task(daily_summary_loop())
    await client.start()
    print("Userbot ishga tushdi.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
