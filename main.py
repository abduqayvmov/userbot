import os
import html
import time
import sqlite3
import asyncio
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from flask import Flask
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import EditAdminRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import ChatAdminRights
from telethon.tl.functions.channels import GetParticipantsRequest, EditBannedRequest
from telethon.tl.types import ChannelParticipantsSearch, ChatBannedRights


logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
logging.getLogger("telethon").setLevel(logging.INFO)

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = "antidelete_session"
SESSION_STRING = os.getenv("SESSION_STRING", "")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
TASHKENT_TZ = ZoneInfo("Asia/Tashkent")

CACHE_DIR = os.getenv(
    "CACHE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "antidelete_cache"),
)
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_DB_FILE = os.path.join(CACHE_DIR, "message_cache.db")

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
        if sender is not None and getattr(sender, "bot", False):
            continue
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
    if sender is not None and getattr(sender, "bot", False):
        return
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
    if not is_media_message(reply) or reply.sticker:
        return
    is_video = bool(reply.video)
    is_round = bool(reply.video_note)
    is_voice = bool(reply.voice)
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


# .ongift / .offgift - profildagi hadyalarni KATEGORIYA bo'yicha yoqish/o'chirish
from telethon.tl.functions.payments import GetSavedStarGiftsRequest, SaveStarGiftRequest
from telethon.tl.types import InputSavedStarGiftUser


# Kategoriya: (nom, emojilar, nom ichidagi kalit so'zlar)
# Yangi kategoriya kerak bo'lsa shu ro'yxatga qo'shib qo'yish kifoya.
GIFT_CATEGORIES = [
    ("teddy",     ["🧸", "🐻", "🐼", "🐨", "🐻‍❄️"],
                  ["bear", "teddy", "мишк", "медвед", "ayiq"]),
    ("heart",     ["💝", "❤", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "🤎",
                   "💖", "💘", "💕", "💗", "💓", "💞", "💟", "♥", "🫶", "💌"],
                  ["heart", "love", "сердц", "любов"]),
    ("rose",      ["🌹"], ["rose", "роз"]),
    ("flower",    ["💐", "🌷", "🌸", "🌺", "🌻", "🌼", "🏵", "🪻", "🪷"],
                  ["flower", "bouquet", "букет", "цвет"]),
    ("rocket",    ["🚀"], ["rocket", "ракет"]),
    ("cake",      ["🎂", "🍰", "🧁"], ["cake", "торт"]),
    ("champagne", ["🍾", "🥂", "🍷", "🍻"], ["champagne", "bottle", "шампан"]),
    ("gift",      ["🎁"], ["gift box", "present", "подар"]),
    ("diamond",   ["💎"], ["diamond", "gem", "алмаз", "брилл"]),
    ("ring",      ["💍"], ["ring", "кольц"]),
    ("trophy",    ["🏆", "🥇", "🥈", "🥉", "🏅", "🎖"], ["trophy", "cup", "award", "кубок"]),
    ("tree",      ["🎄", "🌲"], ["christmas tree", "елк"]),
    ("star",      ["⭐", "🌟", "✨", "💫", "🌠"], ["star", "звезд"]),
    ("ball",      ["⚽", "🏀", "🏈", "⚾", "🎾", "🏐"], ["ball", "мяч"]),
    ("balloon",   ["🎈"], ["balloon", "шар"]),
    ("candy",     ["🍬", "🍭", "🍫", "🍪", "🍩"], ["candy", "sweet", "конфет"]),
    ("crown",     ["👑"], ["crown", "корон"]),
    ("music",     ["🎵", "🎶", "🎸", "🎤", "🎧"], ["music", "guitar", "музык"]),
    ("car",       ["🚗", "🏎", "🚘"], ["car", "машин", "авто"]),
    ("cat",       ["🐱", "🐈", "😺", "🙀"], ["cat", "кот", "кош"]),
    ("dog",       ["🐶", "🐕", "🦮"], ["dog", "пёс", "соба"]),
    ("fire",      ["🔥"], ["fire", "flame", "огон"]),
    ("money",     ["💰", "💵", "💸", "🪙"], ["money", "coin", "деньг", "монет"]),
    ("duck",      ["🦆", "🐤", "🐥", "🐣"], ["duck", "утк"]),
    ("frog",      ["🐸"], ["frog", "pepe", "лягуш"]),
]


# Hadya ID si bo'yicha majburiy kategoriya.
# .gid buyrug'i ID larni ko'rsatadi; emoji bo'yicha noto'g'ri tushgan
# hadyani shu yerga yozib qo'yilsa, u doim shu kategoriyada bo'ladi.
GIFT_ID_OVERRIDES = {
    # --- Ayiqchalar (10 xil) ---
    5170233102089322756: "teddy",  # oddiy ayiqcha, 15 yulduz
    5974210632977745012: "teddy",  # 🧸 50 yulduz
    # Quyidagilar Telegram'da 🎁 emoji bilan keladi, lekin aslida ayiqchalar
    5956217000635139069: "teddy",
    5800655655995968830: "teddy",
    5866352046986232958: "teddy",
    5893356958802511476: "teddy",
    5969796561943660080: "teddy",
    5801108895304779062: "teddy",
    5935895822435615975: "teddy",
    6026193266406327981: "teddy",
    6046178578163303744: "teddy",  # Telegram'da 🎂 emoji bilan keladi

    # --- Yuraklar ---
    5170145012310081615: "heart",  # 💝 15 yulduz

    # --- Qolganlari ---
    5170250947678437525: "gift",       # 🎁 sovg'a qutisi, 25 yulduz
    5168103777563050263: "rose",       # 🌹 25 yulduz
    5170314324215857265: "flower",     # 💐 guldasta, 50 yulduz
    5170564780938756245: "rocket",     # 🚀 50 yulduz
    6028601630662853006: "champagne",  # 🍾 50 yulduz
    5170144170496491616: "cake",       # 🎂 50 yulduz
    5922558454332916696: "tree",       # 🎄 50 yulduz
    5168043875654172773: "trophy",     # 🏆 100 yulduz
    5170690322832818290: "ring",       # 💍 100 yulduz
    5170521118301225164: "diamond",    # 💎 100 yulduz
}


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


def _clean_emoji(text):
    """Variation selector va boshqa ko'rinmas belgilarni olib tashlaydi."""
    return (text or '').replace('️', '').replace('‍', '').strip()


def _doc_alt_emoji(doc):
    """Document (stiker) atributlaridan emoji (alt) ni oladi."""
    for attr in (getattr(doc, 'attributes', None) or []):
        alt = getattr(attr, 'alt', None)
        if alt:
            return alt
    return ''


def _gift_emoji(gift):
    """Hadya stikeridagi emojini qaytaradi (🧸, 🚀, 🌹, 🎂 ...)."""
    emoji = _doc_alt_emoji(getattr(gift, 'sticker', None))
    if emoji:
        return emoji
    # Unique (upgrade qilingan) hadyalarda stiker attributes ichida bo'ladi
    for attr in (getattr(gift, 'attributes', None) or []):
        emoji = _doc_alt_emoji(getattr(attr, 'document', None))
        if emoji:
            return emoji
    return ''


def _gift_category(gift):
    """Hadya qaysi kategoriyaga tegishli: ('teddy', '🧸') ko'rinishida."""
    emoji = _gift_emoji(gift)
    clean = _clean_emoji(emoji)
    title = (getattr(gift, 'title', None) or '').lower()

    forced = GIFT_ID_OVERRIDES.get(getattr(gift, 'id', None))
    if forced:
        return forced, emoji

    for name, emojis, keywords in GIFT_CATEGORIES:
        for e in emojis:
            ce = _clean_emoji(e)
            if ce and clean and (clean == ce or ce in clean or clean in ce):
                return name, emoji
        for kw in keywords:
            if kw and kw in title:
                return name, emoji

    # Kategoriya topilmasa emojining o'zi kategoriya bo'ladi
    return (emoji or f"id{getattr(gift, 'id', '?')}"), emoji


def _group_gifts(all_gifts):
    """Hadyalarni kategoriyalarga ajratadi. Tartib: profildagi kelish tartibi."""
    groups = {}
    for sg in all_gifts:
        gift = getattr(sg, 'gift', None)
        name, emoji = _gift_category(gift)
        g = groups.setdefault(name, {
            'name': name,
            'emojis': [],
            'items': [],
            'gift_ids': set(),
        })
        g['items'].append(sg)
        if emoji and emoji not in g['emojis']:
            g['emojis'].append(emoji)
        gift_id = getattr(gift, 'id', None)
        if gift_id is not None:
            g['gift_ids'].add(gift_id)
    return list(groups.values())


def _group_label(group):
    emojis = ''.join(group['emojis'][:3])
    return f"{emojis} {group['name']}".strip()


def _hidden_shown_counts(group):
    hidden = sum(1 for sg in group['items'] if getattr(sg, 'unsaved', False))
    return hidden, len(group['items']) - hidden


def _select_groups(ordered, raw_args):
    """Argumentlar bo'yicha kategoriyalarni tanlaydi.

    Qo'llab-quvvatlanadi: nom (.offgift teddy), emoji (.offgift 🧸),
    ro'yxatdagi raqam (.offgift 1), bir nechtasi probel/vergul bilan.
    Bo'sh bo'lsa -> hammasi.
    """
    if not raw_args or not raw_args.strip():
        return ordered, []

    tokens = [t for t in raw_args.replace(',', ' ').split() if t.strip()]
    if any(t.lower() in ('all', 'hammasi', 'barchasi', 'все', '*') for t in tokens):
        return ordered, []

    selected, unknown = [], []
    for token in tokens:
        t = token.strip()
        tl = t.lower()
        matched = []

        # 1) Ro'yxatdagi tartib raqami
        if t.isdigit() and 1 <= int(t) <= len(ordered):
            matched = [ordered[int(t) - 1]]

        # 2) Kategoriya nomi
        if not matched:
            matched = [g for g in ordered if g['name'].lower() == tl]

        # 3) Emoji
        if not matched:
            ct = _clean_emoji(t)
            if ct:
                matched = [g for g in ordered
                           if any(_clean_emoji(e) == ct for e in g['emojis'])]

        # 4) Nom bo'lagi
        if not matched:
            matched = [g for g in ordered if tl in g['name'].lower()]

        if not matched:
            unknown.append(t)
        for g in matched:
            if g not in selected:
                selected.append(g)

    return selected, unknown


async def _apply_gift_visibility(event, groups, unsave):
    """Tanlangan kategoriyalardagi hadyalarni yashiradi (unsave=True) yoki ko'rsatadi."""
    targets = [
        sg for g in groups for sg in g['items']
        if bool(getattr(sg, 'unsaved', False)) != unsave and getattr(sg, 'msg_id', None)
    ]

    if not targets:
        if unsave:
            return await event.edit("📊 **Natija:** Tanlangan hadyalar allaqachon yashirilgan.")
        return await event.edit("📊 **Natija:** Tanlangan hadyalar allaqachon ochiq turibdi.")

    names = ', '.join(_group_label(g) for g in groups[:6])
    if len(groups) > 6:
        names += f" va yana {len(groups) - 6} ta"
    action = "berkitilmoqda" if unsave else "qaytarilmoqda"
    await event.edit(f"📦 {names}\n🔄 {len(targets)} ta hadya {action}...")

    done, failed = 0, 0
    for i, sg in enumerate(targets, start=1):
        try:
            await client(SaveStarGiftRequest(
                stargift=InputSavedStarGiftUser(msg_id=sg.msg_id),
                unsave=unsave,
            ))
            done += 1
        except FloodWaitError as e:
            wait_time = e.seconds + 5
            try:
                await event.edit(f"⏳ FloodWait: {wait_time} soniya kutilmoqda... ({done}/{len(targets)})")
            except Exception:
                pass
            await asyncio.sleep(wait_time)
            try:
                await client(SaveStarGiftRequest(
                    stargift=InputSavedStarGiftUser(msg_id=sg.msg_id),
                    unsave=unsave,
                ))
                done += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1

        if i % 10 == 0 and i < len(targets):
            try:
                await event.edit(f"🔄 {i}/{len(targets)} ta hadya {action}...")
            except Exception:
                pass
        await asyncio.sleep(1.5)  # Telegram blokiga tushmaslik uchun

    icon = "🙈" if unsave else "🎉"
    verb = "berkitildi" if unsave else "profilda qayta ko'rinadigan qilindi"
    text = f"✅ **Tayyor!**\n{icon} `{done}` ta hadya {verb}."
    if failed:
        text += f"\n⚠️ Xato: `{failed}` ta."
    await event.edit(text)


async def _handle_gift_command(event, unsave, raw_args):
    await event.edit("🔄 Hadyalar ro'yxati yuklanmoqda, kuting...")
    try:
        all_gifts = await _fetch_all_saved_gifts()
        if not all_gifts:
            return await event.edit("📊 **Natija:** Profilingizda hadyalar topilmadi.")

        ordered = _group_gifts(all_gifts)
        selected, unknown = _select_groups(ordered, raw_args)

        if not selected:
            cmd = ".offgift" if unsave else ".ongift"
            lines = [f"❌ Bunday kategoriya yo'q: {', '.join(unknown)}", "", "Mavjudlari:"]
            for idx, g in enumerate(ordered, start=1):
                lines.append(f"`{idx}.` {_group_label(g)} — {len(g['items'])} ta")
            lines.append(f"\nMasalan: `{cmd} {ordered[0]['name']}`")
            return await event.edit("\n".join(lines))

        await _apply_gift_visibility(event, selected, unsave)

        if unknown:
            try:
                await event.reply(f"⚠️ Tanilmagan kategoriya: {', '.join(unknown)}")
            except Exception:
                pass

    except Exception as e:
        action = "yashirishda" if unsave else "yoqishda"
        await event.edit(f"❌ Hadyalarni {action} xatolik yuz berdi: {str(e)}")


# 📋 .gifts - profildagi hadya kategoriyalari ro'yxati
@client.on(events.NewMessage(pattern=r"^\.(gifts|glist|hadyalar)$", outgoing=True))
async def list_gift_types(event):
    await event.edit("🔄 Hadyalar sanalmoqda...")
    try:
        all_gifts = await _fetch_all_saved_gifts()
        if not all_gifts:
            return await event.edit("📊 Profilingizda hadyalar topilmadi.")

        ordered = _group_gifts(all_gifts)
        total_hidden = sum(_hidden_shown_counts(g)[0] for g in ordered)

        lines = [
            f"🎁 **Hadya kategoriyalari** — jami `{len(all_gifts)}` ta "
            f"(👁 {len(all_gifts) - total_hidden} ochiq · 🙈 {total_hidden} yashirin)",
            "",
        ]
        for idx, g in enumerate(ordered, start=1):
            hidden, shown = _hidden_shown_counts(g)
            state = []
            if shown:
                state.append(f"👁 {shown}")
            if hidden:
                state.append(f"🙈 {hidden}")
            kinds = len(g['gift_ids'])
            extra = f", {kinds} xil" if kinds > 1 else ""
            lines.append(
                f"`{idx}.` {_group_label(g)} — {len(g['items'])} ta{extra} ({' · '.join(state)})"
            )

        first = ordered[0]['name']
        lines += [
            "",
            f"🙈 O'chirish: `.offgift {first}` · `.offgift teddy heart`",
            f"👁 Yoqish: `.ongift {first}`",
            "📦 Hammasi: `.offgift` / `.ongift`",
        ]
        await event.edit("\n".join(lines))
    except Exception as e:
        await event.edit(f"❌ Xatolik: {str(e)}")


# 🔎 .gid - har bir hadya turining ID si va qaysi kategoriyaga tushgani
@client.on(events.NewMessage(pattern=r"^\.(gid|gids)$", outgoing=True))
async def list_gift_ids(event):
    await event.edit("🔄 Hadya ID lari yig'ilmoqda...")
    try:
        all_gifts = await _fetch_all_saved_gifts()
        if not all_gifts:
            return await event.edit("📊 Profilingizda hadyalar topilmadi.")

        seen = {}
        for sg in all_gifts:
            gift = getattr(sg, 'gift', None)
            gid = getattr(gift, 'id', None)
            if gid in seen:
                seen[gid]['count'] += 1
                if getattr(sg, 'unsaved', False):
                    seen[gid]['hidden'] += 1
                continue
            name, emoji = _gift_category(gift)
            seen[gid] = {
                'id': gid,
                'emoji': emoji or '—',
                'stars': getattr(gift, 'stars', None),
                'title': (getattr(gift, 'title', None) or '').strip(),
                'category': name,
                'count': 1,
                'hidden': 1 if getattr(sg, 'unsaved', False) else 0,
            }

        lines = [f"🔎 **Hadya turlari** — {len(seen)} xil\n"]
        for i, g in enumerate(seen.values(), start=1):
            price = f"{g['stars']}⭐" if g['stars'] else ""
            title = f" {g['title']}" if g['title'] else ""
            if g['hidden'] == g['count']:
                state = "🙈"
            elif g['hidden']:
                state = f"🙈{g['hidden']}/{g['count']}"
            else:
                state = "👁"
            lines.append(
                f"`{i}.` {state} {g['emoji']}{title} {price} ×{g['count']}\n"
                f"     `{g['id']}` → `{g['category']}`"
            )

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3950] + "\n…"
        await event.edit(text)
    except Exception as e:
        await event.edit(f"❌ Xatolik: {str(e)}")


# 🖼 .gshow <kategoriya> - kategoriyadagi har bir hadya turini rasmi bilan ko'rsatadi
@client.on(events.NewMessage(pattern=r"^\.(gshow|gko)(?:\s+(.+))?$", outgoing=True))
async def show_gift_stickers(event):
    arg = (event.pattern_match.group(2) or "").strip()
    if not arg:
        return await event.edit("Qaysi kategoriya? Masalan: `.gshow teddy`")

    await event.edit("🔄 Yuklanmoqda...")
    try:
        all_gifts = await _fetch_all_saved_gifts()
        if not all_gifts:
            return await event.edit("📊 Hadyalar topilmadi.")

        ordered = _group_gifts(all_gifts)
        selected, unknown = _select_groups(ordered, arg)
        if not selected:
            return await event.edit(f"❌ Bunday kategoriya yo'q: {', '.join(unknown)}")

        # Har bir turdan bittadan
        uniq = {}
        for g in selected:
            for sg in g['items']:
                gift = getattr(sg, 'gift', None)
                gid = getattr(gift, 'id', None)
                if gid not in uniq:
                    uniq[gid] = (gift, g['name'])

        await event.edit(f"🖼 `{len(uniq)}` xil topildi, yuborilmoqda...")

        for gid, (gift, cat) in uniq.items():
            stars = getattr(gift, 'stars', None)
            price = f" · {stars}⭐" if stars else ""
            try:
                sent = await client.send_file(event.chat_id, gift.sticker)
                await client.send_message(
                    event.chat_id,
                    f"`{gid}` → `{cat}`{price}",
                    reply_to=sent.id,
                )
            except Exception as e:
                await client.send_message(event.chat_id, f"`{gid}` → `{cat}`{price}\n⚠️ {e}")
            await asyncio.sleep(0.5)

        await event.edit(
            f"✅ `{len(uniq)}` xil ko'rsatildi.\n"
            "Keraksizini `.delete` bilan tozalashingiz mumkin."
        )
    except Exception as e:
        await event.edit(f"❌ Xatolik: {str(e)}")


# 🙈 .offgift [kategoriya] - hadyalarni profildan yashirish (argumentsiz = hammasi)
@client.on(events.NewMessage(
    pattern=r"^\.(offgift|goff|оффгифт|гофф)(?:\s+(.+))?$", outgoing=True))
async def hide_gifts(event):
    await _handle_gift_command(event, unsave=True, raw_args=event.pattern_match.group(2) or "")


# 👁 .ongift [kategoriya] - hadyalarni profilda qayta ko'rsatish (argumentsiz = hammasi)
@client.on(events.NewMessage(
    pattern=r"^\.(ongift|gon|онгифт|гон)(?:\s+(.+))?$", outgoing=True))
async def show_gifts(event):
    await _handle_gift_command(event, unsave=False, raw_args=event.pattern_match.group(2) or "")


# ============================================================
# NAKRUTKA / FAKE AKKAUNT SKANER VA KICK QILISH
# Ushbu blokni main.py oxiriga (client.run_until_disconnected() dan OLDIN) qo'shing.
# Kerakli import'lar main.py boshiga qo'shilishi kerak (pastda ko'rsatilgan).
# ============================================================

# --- main.py FAYLINING BOSHIGA QO'SHILADIGAN IMPORT'LAR ---
# from telethon.tl.functions.channels import GetParticipantsRequest, EditBannedRequest
# from telethon.tl.types import ChannelParticipantsSearch, ChatBannedRights

# Oxirgi skanerlash natijalarini xotirada saqlab turamiz: {chat_id: [user_id, ...]}
last_fake_scan = {}


BATCH_WINDOW_SECONDS = 300  # shu oraliqda (5 daqiqa) qo'shilganlar bitta "partiya" deb hisoblanadi
BATCH_MIN_SIZE = 5          # shu partiyada kamida nechta shubhali akkaunt bo'lsa, nakrutka deb topiladi


def _looks_fake(user):
    """Faqat profil belgilariga qarab: rasm yo'q, username yo'q, bot/deleted emas.
    Bu yolg'iz holda YETARLI EMAS - ko'p haqiqiy odamlar ham shu tavsifga tushadi
    (masalan, maxfiylikni yopiq qilganlar). Shuning uchun pastda join-vaqti
    klasterlash bilan birga ishlatiladi."""
    if getattr(user, "bot", False):
        return False
    if getattr(user, "deleted", False):
        return False
    has_photo = bool(getattr(user, "photo", None))
    has_username = bool(getattr(user, "username", None))
    return not has_photo and not has_username


def _find_batch_joined_fakes(users, participants):
    """Asosiy mezon: bir necha daqiqa ichida ommaviy qo'shilgan va profil
    belgilari shubhali bo'lgan akkauntlar - bu nakrutkaning eng aniq izi,
    chunki haqiqiy odamlar vaqt bo'ylab tarqoq qo'shiladi, nakrutka esa
    to'lovdan keyin bir zumda partiya-partiya qo'shiladi."""
    user_by_id = {u.id: u for u in users}

    # user_id -> qo'shilgan vaqt (faqat _looks_fake bo'lganlar uchun)
    candidates = []  # (date, user)
    for p in participants:
        uid = getattr(p, "user_id", None)
        join_date = getattr(p, "date", None)
        user = user_by_id.get(uid)
        if not user or not join_date or not _looks_fake(user):
            continue
        candidates.append((join_date, user))

    candidates.sort(key=lambda x: x[0])

    flagged = []
    i = 0
    n = len(candidates)
    while i < n:
        j = i
        window_end = candidates[i][0] + timedelta(seconds=BATCH_WINDOW_SECONDS)
        while j < n and candidates[j][0] <= window_end:
            j += 1
        batch = candidates[i:j]
        if len(batch) >= BATCH_MIN_SIZE:
            flagged.extend(u for _, u in batch)
        i = j

    # takrorlanishlarni olib tashlash (chegaralar ustma-ust tushib qolsa)
    seen = set()
    unique_flagged = []
    for u in flagged:
        if u.id not in seen:
            seen.add(u.id)
            unique_flagged.append(u)
    return unique_flagged


@client.on(events.NewMessage(pattern=r"^\.(nakrutka|накрутка)$", outgoing=True))
async def scan_fake_accounts(event):
    if event.is_private:
        return await event.edit("Bu buyruq faqat guruhda ishlaydi.")

    chat_id = event.chat_id
    await event.edit("🔍 Guruh a'zolari skanerlanmoqda, kuting...")

    all_users = []
    all_participants = []
    total = 0
    offset = 0
    limit = 200

    try:
        while True:
            result = await client(GetParticipantsRequest(
                channel=chat_id,
                filter=ChannelParticipantsSearch(""),
                offset=offset,
                limit=limit,
                hash=0,
            ))
            if not result.users:
                break
            total += len(result.users)
            all_users.extend(result.users)
            all_participants.extend(result.participants)
            offset += len(result.users)
            if len(result.users) < limit:
                break
            await asyncio.sleep(1)  # flood-wait'dan saqlanish
    except FloodWaitError as e:
        await event.edit(f"FloodWait: {e.seconds} soniya kutish kerak, keyinroq urinib ko'ring.")
        return
    except Exception as e:
        await event.edit(f"Skanerlashda xatolik: {e}\n\n(Eslatma: bu komanda faqat supergroup/kanal turidagi guruhlarda ishlaydi, oddiy kichik guruhda ishlamasligi mumkin.)")
        return

    suspicious = _find_batch_joined_fakes(all_users, all_participants)
    last_fake_scan[chat_id] = [u.id for u in suspicious]

    if not suspicious:
        return await event.edit(f"✅ Skanerlash tugadi. {total} a'zodan shubhali (nakrutka) akkaunt topilmadi.")

    lines = [f"🕵️ #nakrutka_skaner\nGuruh: {chat_id}\nJami a'zo: {total}\nShubhali topildi: {len(suspicious)}\n"]
    for u in suspicious[:50]:  # xabar juda uzun bo'lib ketmasligi uchun birinchi 50 tasi
        name = f"{u.first_name or ''} {u.last_name or ''}".strip() or "Noma'lum"
        lines.append(f"• {mention_html(name, u.id)} (id={u.id})")
    if len(suspicious) > 50:
        lines.append(f"... va yana {len(suspicious) - 50} ta (to'liq ro'yxat .kickfake bilan chiqariladi)")

    caption = "\n".join(lines)
    send_log_message(caption)

    await event.edit(
        f"✅ Skanerlash tugadi.\nJami a'zo: {total}\nShubhali (nakrutka) topildi: {len(suspicious)}\n\n"
        f"To'liq ro'yxat LOG kanalga yuborildi.\n"
        f"Hammasini chiqarish uchun: .kickfake"
    )


@client.on(events.NewMessage(pattern=r"^\.(kickfake|кикфейк)$", outgoing=True))
async def kick_fake_accounts(event):
    if event.is_private:
        return await event.edit("Bu buyruq faqat guruhda ishlaydi.")

    chat_id = event.chat_id
    ids = last_fake_scan.get(chat_id)
    if not ids:
        return await event.edit("Avval .nakrutka bilan skanerlash qiling.")

    await event.edit(f"🚫 {len(ids)} ta shubhali akkaunt chiqarilmoqda...")

    banned_rights = ChatBannedRights(until_date=None, view_messages=True)
    unban_rights = ChatBannedRights(until_date=None, view_messages=False)  # darhol qayta qo'shilishi mumkin bo'lishi uchun

    kicked = 0
    failed = 0
    for uid in ids:
        try:
            await client(EditBannedRequest(chat_id, uid, banned_rights))
            await asyncio.sleep(0.5)
            await client(EditBannedRequest(chat_id, uid, unban_rights))  # kick, ban emas
            kicked += 1
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 2)
        except Exception as e:
            print(f".kickfake: {uid} chiqarishda xatolik: {e}")
            failed += 1
        await asyncio.sleep(1)  # flood-wait'dan saqlanish uchun har biri orasida pauza

    last_fake_scan.pop(chat_id, None)
    await event.edit(f"✅ Yakunlandi.\nChiqarildi: {kicked}\nXato: {failed}")




fake_server = Flask(__name__)


@fake_server.route("/")
def home():
    return "Bot ishlab turibdi."


def run_fake_server():
    port = int(os.getenv("PORT", 8080))
    fake_server.run(host="0.0.0.0", port=port)


async def main():
    threading.Thread(target=run_fake_server, daemon=True).start()
    while True:
        try:
            await client.start()
            print("Userbot ishga tushdi.")
            await client.run_until_disconnected()
        except Exception as e:
            print(f"Telegram ulanishida xatolik: {e}")
        print("Telegram bilan aloqa uzildi, 5 soniyadan keyin qayta ulanishga urinaman...")
        await asyncio.sleep(5)


# .odel / .одел - guruhdagi barcha "deleted account"larni chiqaradi
@client.on(events.NewMessage(pattern=r"^\.(odel|одел)$", outgoing=True))
async def kick_deleted_accounts(event):
    if event.is_private:
        return await event.edit("Bu buyruq faqat guruhda ishlaydi.")

    chat = await event.get_chat()
    await event.edit("🔍 O'chirilgan akkauntlar qidirilmoqda...")

    deleted_users = []
    try:
        async for user in client.iter_participants(chat):
            if user.deleted:
                deleted_users.append(user)
    except Exception as e:
        return await event.edit(f"Xatolik: {e}")

    if not deleted_users:
        return await event.edit("✅ O'chirilgan akkaunt topilmadi.")

    await event.edit(f"🗑 {len(deleted_users)} ta o'chirilgan akkaunt topildi. Chiqarilmoqda...")

    banned_rights = ChatBannedRights(until_date=None, view_messages=True)

    removed = 0
    failed = 0
    for i, user in enumerate(deleted_users, start=1):
        try:
            await client(EditBannedRequest(chat, user.id, banned_rights))
            removed += 1
        except FloodWaitError as e:
            wait_time = e.seconds + 5
            try:
                await event.edit(
                    f"⏳ FloodWait: {wait_time} soniya kutilmoqda... "
                    f"({removed}/{len(deleted_users)} chiqarildi)"
                )
            except Exception:
                pass
            await asyncio.sleep(wait_time)
        except Exception:
            failed += 1
        # Har bir akkaunt orasida sekin harakat qilamiz, va har 15 tadan
        # keyin qo'shimcha uzunroq tanaffus beramiz - bu FloodWait'ga
        # tushish ehtimolini sezilarli kamaytiradi.
        await asyncio.sleep(2.5)
        if i % 15 == 0:
            await asyncio.sleep(10)

    await event.edit(f"✅ Tugadi.\nChiqarildi: {removed} ta\nXato: {failed} ta")


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
