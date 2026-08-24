import os
import re
import time
import json
import asyncio
import threading
from datetime import datetime
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
TARGET_LANG = "uz"

CACHE_DIR = "/tmp/antidelete_cache"
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_MAX_AGE_SECONDS = 60 * 60 * 24 * 2  # 2 kun
CACHE_MAX_ENTRIES = 3000
CACHE_INDEX_FILE = os.path.join(CACHE_DIR, "cache_index.json")
WATCHED_USERNAMES_FILE = os.path.join(CACHE_DIR, "watched_usernames.json")
USERNAME_CHECK_INTERVAL = 15  # soniya
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

# Render kabi vaqtinchalik disk muhitida fayl sessiyasi har deploy'da yo'qoladi,
# shuning uchun SESSION_STRING mavjud bo'lsa o'shani ishlatamiz (generate_session.py bilan olinadi).
if SESSION_STRING:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
private_message_cache = {}


def _serialize_cache_entry(data):
    entry = dict(data)
    entry["date"] = data["date"].isoformat() if data.get("date") else None
    return entry


def _deserialize_cache_entry(entry):
    data = dict(entry)
    data["date"] = datetime.fromisoformat(entry["date"]) if entry.get("date") else None
    return data


def save_cache_to_disk():
    """Keshni diskka yozadi - jarayon qayta ishga tushganda (masalan xatolikdan
    keyin qayta ko'tarilganda) xabarlar keshi yo'qolmasligi uchun. Konteyner
    butunlay qayta qurilganda (masalan Render'da yangi deploy) diskning o'zi
    ham tozalanadi, bu holatda kesh baribir bo'sh boshlanadi."""
    try:
        with open(CACHE_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): _serialize_cache_entry(v) for k, v in private_message_cache.items()}, f)
    except Exception as e:
        print(f"Keshni diskka saqlashda xatolik: {e}")


def load_cache_from_disk():
    if not os.path.exists(CACHE_INDEX_FILE):
        return
    try:
        with open(CACHE_INDEX_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for key, entry in raw.items():
            private_message_cache[int(key)] = _deserialize_cache_entry(entry)
        print(f"Kesh diskdan tiklandi: {len(private_message_cache)} ta yozuv.")
    except Exception as e:
        print(f"Keshni diskdan yuklashda xatolik: {e}")


load_cache_from_disk()

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


load_watched_usernames()


def mention_html(name, user_id):
    safe_name = (name or "Noma'lum").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def is_media_message(message):
    return bool(message.photo or message.video or message.video_note or message.voice or message.sticker)


def message_kind_emoji(message):
    if message.video_note:
        return "⭕"
    if message.voice:
        return "🎤"
    if message.sticker:
        return "🧩"
    if message.video:
        return "🎥"
    if message.photo:
        return "🖼"
    return "💬"


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


def cleanup_old_cache():
    """Eski kesh yozuvlarini va disk fayllarini vaqti-vaqti bilan tozalaydi."""
    now = time.time()
    expired_keys = []
    for key, data in list(private_message_cache.items()):
        age = now - data.get("cached_at", now)
        if age > CACHE_MAX_AGE_SECONDS:
            expired_keys.append(key)

    for key in expired_keys:
        data = private_message_cache.pop(key, None)
        if data and data.get("media_path") and os.path.exists(data["media_path"]):
            try:
                os.remove(data["media_path"])
            except Exception:
                pass

    while len(private_message_cache) > CACHE_MAX_ENTRIES:
        oldest_key = next(iter(private_message_cache))
        data = private_message_cache.pop(oldest_key, None)
        if data and data.get("media_path") and os.path.exists(data["media_path"]):
            try:
                os.remove(data["media_path"])
            except Exception:
                pass

    save_cache_to_disk()


async def periodic_cleanup():
    while True:
        await asyncio.sleep(1800)  # har 30 daqiqada
        cleanup_old_cache()


@client.on(events.NewMessage())
async def cache_private_messages(event):
    if event.is_private:
        media_path = None
        is_round = False
        if event.media and is_media_message(event.message):
            is_round = bool(event.video_note)
            media_path = await download_to_disk(event.message)

        private_message_cache[event.id] = {
            "text": event.raw_text,
            "sender_id": event.sender_id,
            "chat_id": event.chat_id,
            "media_path": media_path,
            "is_round": is_round,
            "kind_emoji": message_kind_emoji(event.message),
            "date": event.date,
            "cached_at": time.time(),
        }
        if len(private_message_cache) > CACHE_MAX_ENTRIES:
            oldest_key = next(iter(private_message_cache))
            old_data = private_message_cache.pop(oldest_key, None)
            if old_data and old_data.get("media_path") and os.path.exists(old_data["media_path"]):
                try:
                    os.remove(old_data["media_path"])
                except Exception:
                    pass
        save_cache_to_disk()


@client.on(events.MessageDeleted())
async def on_message_deleted(event):
    if not LOG_CHANNEL_ID:
        return
    any_removed = False
    for msg_id in event.deleted_ids:
        data = private_message_cache.pop(msg_id, None)
        if not data:
            continue
        any_removed = True
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
            f"{data.get('kind_emoji', '💬')} Kimdan: {sender_link}{id_part}\n"
            f"Vaqt: {data['date']}\n"
            f"Matn: {data['text'] or '(matn yoq)'}"
        )
        try:
            if data["media_path"] and os.path.exists(data["media_path"]):
                await client.send_file(
                    LOG_CHANNEL_ID, data["media_path"], caption=caption,
                    video_note=data["is_round"], parse_mode="html",
                )
                try:
                    os.remove(data["media_path"])
                except Exception:
                    pass
            else:
                await client.send_message(LOG_CHANNEL_ID, caption, parse_mode="html")
        except Exception as e:
            print(f"Log kanalga yuborishda xatolik: {e}")

    if any_removed:
        save_cache_to_disk()


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
        await client.send_file(LOG_CHANNEL_ID, media_path, caption=caption, video_note=is_round, parse_mode="html")
        try:
            os.remove(media_path)
        except Exception:
            pass
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


async def _claim_and_notify(username):
    """Bo'shab qolgan username'ni darhol profilga o'rnatishga urinadi va natijani
    LOG_CHANNEL_ID'ga yuboradi. Boshqa kimdir bir zumda ulgurib olgan bo'lishi mumkin -
    bu holat ham xabarda ko'rsatiladi."""
    try:
        await client(UpdateUsernameRequest(username))
        status_text = f"✅ @{username} muvaffaqiyatli sizning profilingizga o'rnatildi!"
    except Exception as e:
        status_text = f"⚠️ @{username} bo'shadi, lekin avtomatik o'rnatishda xatolik: {e}"
    try:
        await client.send_message(LOG_CHANNEL_ID, f"🎉 #username_boshaldi\n{status_text}")
    except Exception as e:
        print(f".user: bildirishnoma yuborishda xatolik: {e}")


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


fake_server = Flask(__name__)


@fake_server.route("/")
def home():
    return "Bot ishlab turibdi."


def run_fake_server():
    port = int(os.getenv("PORT", 8080))
    fake_server.run(host="0.0.0.0", port=port)


async def main():
    threading.Thread(target=run_fake_server, daemon=True).start()
    asyncio.create_task(periodic_cleanup())
    asyncio.create_task(periodic_username_check())
    await client.start()
    print("Userbot ishga tushdi.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
