import os
import time
import asyncio
import threading
import requests
import json
from flask import Flask
from telethon import TelegramClient, events, functions, types
from telethon.tl.functions.channels import EditAdminRequest
from telethon.tl.types import ChatAdminRights
from telethon.tl.functions.users import GetFullUserRequest

# --- SOZLAMALAR ---
API_ID = int(os.getenv("API_ID", "0")) 
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = "antidelete_session"
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
TARGET_LANG = "uz"

CACHE_DIR = "/tmp/antidelete_cache"
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_MAX_AGE_SECONDS = 60 * 60 * 24 * 2  # 2 kunlik kesh vaqti
CACHE_MAX_ENTRIES = 3000

# Kuzatuv ma'lumotlarini saqlash uchun fayl
WATCH_FILE = "watched_users.json"

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
private_message_cache = {}
user_tags = {}

# Kuzatuv ro'yxatini yuklash
def load_watched_users():
    if os.path.exists(WATCH_FILE):
        try:
            with open(WATCH_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

# Kuzatuv ro'yxatini saqlash
def save_watched_users(data):
    try:
        with open(WATCH_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Faylga saqlashda xatolik: {e}")

watched_users = load_watched_users()


def mention_html(name, user_id):
    safe_name = (name or "Noma'lum").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def is_media_message(message):
    return bool(message.photo or message.video or message.video_note or message.voice or message.sticker)


async def download_to_disk(message):
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


async def periodic_cleanup():
    while True:
        await asyncio.sleep(1800)
        cleanup_old_cache()


# --- AVTOMATIK KUZATISH TIZIMI (BACKGROUND WORKER) ---
async def spy_monitor_worker():
    """Har 5 daqiqada belgilangan foydalanuvchilar profilini tekshiradi."""
    await client.start()
    while True:
        await asyncio.sleep(300) # 300 soniya = 5 daqiqa
        global watched_users
        watched_users = load_watched_users()
        
        for user_id_str, old_data in list(watched_users.items()):
            try:
                user_id = int(user_id_str)
                # Foydalanuvchining to'liq ma'lumotlarini serverdan so'rash
                full_user = await client(GetFullUserRequest(user_id))
                user = full_user.users[0]
                
                current_first_name = user.first_name or ""
                current_last_name = user.last_name or ""
                current_username = user.username or ""
                current_bio = full_user.full_user.about or ""
                # Oxirgi profil rasm ID si
                current_photo_id = str(user.photo.photo_id) if user.photo else "yoq"
                
                changes = []
                
                # 1. Ism o'zgarishi
                if old_data.get("first_name") != current_first_name or old_data.get("last_name") != current_last_name:
                    changes.append(f"📝 **Ism o'zgardi:**\nEski: `{old_data.get('first_name')} {old_data.get('last_name')}`\nYangi: `{current_first_name} {current_last_name}`")
                
                # 2. Username o'zgarishi
                if old_data.get("username") != current_username:
                    changes.append(f"🔗 **Username o'zgardi:**\nEski: @{old_data.get('username') or 'yoq'}\nYangi: @{current_username or 'yoq'}")
                
                # 3. Bio (Tarjimai hol) o'zgarishi
                if old_data.get("bio") != current_bio:
                    changes.append(f"ℹ️ **Bio (Tarjimai hol) o'zgardi:**\nEski: `{old_data.get('bio') or 'bosh'}`\nYangi: `{current_bio or 'bosh'}`")
                
                # 4. Profil rasmi o'zgarishi
                if old_data.get("photo_id") != current_photo_id:
                    changes.append(f"🖼 **Profil rasmi o'zgartirildi!**")
                
                # Agar o'zgarish bo'lsa, log kanalga yoki shaxsiy saqlangan xabarlarga yuborish
                if changes:
                    msg_text = f"🔔 **KUZATUV BILDIRISHNOMASI**\nFoydalanuvchi: {mention_html(current_first_name, user_id)} (ID: `{user_id}`)\n\n" + "\n\n".join(changes)
                    
                    # Log kanalga yuborish
                    if LOG_CHANNEL_ID:
                        await client.send_message(LOG_CHANNEL_ID, msg_text, parse_mode="html")
                    else:
                        # Agar log kanal sozlunmagan bo'lsa shaxsiy "Saved Messages"ga yuboradi
                        await client.send_message("me", msg_text, parse_mode="html")
                    
                    # Yangi ma'lumotlarni bazaga saqlab qo'yish
                    watched_users[user_id_str] = {
                        "first_name": current_first_name,
                        "last_name": current_last_name,
                        "username": current_username,
                        "bio": current_bio,
                        "photo_id": current_photo_id
                    }
                    save_watched_users(watched_users)
                    
            except Exception as e:
                print(f"Kuzatishda xatolik ({user_id_str}): {e}")


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
            "date": event.date,
            "cached_at": time.time(),
        }


@client.on(events.MessageDeleted())
async def on_message_deleted(event):
    if not LOG_CHANNEL_ID:
        return
    for msg_id in event.deleted_ids:
        data = private_message_cache.pop(msg_id, None)
        if not data:
            continue
        try:
            sender = await client.get_entity(data["sender_id"]) if data["sender_id"] else None
            sender_name = getattr(sender, "first_name", "Noma'lum") if sender else "Noma'lum"
            sender_link = mention_html(sender_name, data["sender_id"]) if data["sender_id"] else sender_name
        except Exception:
            sender_link = "Noma'lum"

        caption = f"O'chirilgan shaxsiy xabar\nKimdan: {sender_link}\nVaqt: {data['date']}\nMatn: {data['text'] or '(matn yoq)'}"
        try:
            if data["media_path"] and os.path.exists(data["media_path"]):
                await client.send_file(LOG_CHANNEL_ID, data["media_path"], caption=caption, video_note=data["is_round"], parse_mode="html")
                try: os.remove(data["media_path"])
                except Exception: pass
            else:
                await client.send_message(LOG_CHANNEL_ID, caption, parse_mode="html")
        except Exception as e:
            print(f"Log kanalga yuborishda xatolik: {e}")


@client.on(events.NewMessage(outgoing=True))
async def save_media_via_reply(event):
    if not LOG_CHANNEL_ID or not event.is_reply: return
    reply = await event.get_reply_message()
    if not reply or not reply.media or reply.out or not is_media_message(reply): return
    try:
        sender = await reply.get_sender()
        sender_name = getattr(sender, "first_name", None) or "Noma'lum"
        sender_id = getattr(sender, "id", None) or reply.sender_id
        sender_link = mention_html(sender_name, sender_id) if sender_id else sender_name
        caption = f"Media (reply orqali saqlandi)\nKimdan: {sender_link}"
        media_path = await download_to_disk(reply)
        if media_path:
            await client.send_file(LOG_CHANNEL_ID, media_path, caption=caption, video_note=bool(reply.video_note), parse_mode="html")
            try: os.remove(media_path)
            except Exception: pass
    except Exception as e: print(f"Reply media xato: {e}")


# --- KUZATUV BUYRUKLARI ---
@client.on(events.NewMessage(pattern=r"^\.kuzat(?:\s+(.+))?$", outgoing=True))
async def add_to_spy(event):
    target = event.pattern_match.group(1)
    
    if not target and event.is_reply:
