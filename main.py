import os
import time
import asyncio
import threading
import requests
from flask import Flask
from telethon import TelegramClient, events
from telethon.tl.functions.channels import EditAdminRequest
from telethon.tl.types import ChatAdminRights

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = "antidelete_session"
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
TARGET_LANG = "uz"

CACHE_DIR = "/tmp/antidelete_cache"
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_MAX_AGE_SECONDS = 60 * 60 * 24 * 2  # 2 kun
CACHE_MAX_ENTRIES = 3000

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
private_message_cache = {}
user_tags = {}
# Chaqirish (spam) vazifalarini kuzatish uchun lug'at
active_spams = {}



def mention_html(name, user_id):
    safe_name = (name or "Noma'lum").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def is_media_message(message):
    return bool(message.photo or message.video or message.video_note or message.voice or message.sticker)


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


@client.on(events.MessageDeleted())
async def on_message_deleted(event):
    if not LOG_CHANNEL_ID:
        return
    for msg_id in event.deleted_ids:
        data = private_message_cache.pop(msg_id, None)
        if not data:
            continue
        sender = await client.get_entity(data["sender_id"]) if data["sender_id"] else None
        sender_name = getattr(sender, "first_name", "Noma'lum") if sender else "Noma'lum"
        sender_link = mention_html(sender_name, data["sender_id"]) if data["sender_id"] else sender_name

        caption = (
            f"O'chirilgan shaxsiy xabar\n"
            f"Kimdan: {sender_link}\n"
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


# Media reply orqali saqlash - endi shaxsiy chat, guruh va kanallarda ham ishlaydi
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
        sender_id = getattr(sender, "id", None) or reply.sender_id
        sender_link = mention_html(sender_name, sender_id) if sender_id else sender_name
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
        caption = f"{kind} (reply orqali saqlandi)\nKimdan: {sender_link}"
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


@client.on(events.NewMessage(pattern=r"^\.ad(?:\s+(.+))?$", outgoing=True))
async def add_admin_tag(event):
    if event.is_private:
        return await event.edit("Bu buyruq faqat guruhda ishlaydi.")
    reply = await event.get_reply_message()
    if not reply or not getattr(reply, "sender_id", None):
        return await event.edit("Reply qiling: .ad teg_matni")
    tag_text = event.pattern_match.group(1) or ""
    target_id = reply.sender_id
    try:
        rights = ChatAdminRights(
            add_admins=False, invite_users=True, change_info=False,
            ban_users=False, delete_messages=True, pin_messages=True, manage_call=True,
        )
        await client(EditAdminRequest(event.chat_id, target_id, rights, tag_text or "Admin"))
        user_tags[target_id] = tag_text
        await event.edit(f"Admin qilindi. Teg: {tag_text or '(bosh)'}")
    except Exception as e:
        await event.edit(f"Xatolik: {e}")


@client.on(events.NewMessage(pattern=r"^\.unad$", outgoing=True))
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
        user_tags.pop(target_id, None)
        await event.edit("Admin va teg olib tashlandi.")
    except Exception as e:
        await event.edit(f"Xatolik: {e}")


# .delete - o'zining barcha xabarlarini o'chiradi (guruh + shaxsiy), yakuniy xabarsiz
@client.on(events.NewMessage(pattern=r"^\.delete$", outgoing=True))
async def delete_my_messages(event):
    chat_id = event.chat_id
    me = await client.get_me()

    await event.delete()

    ids_batch = []
    async for msg in client.iter_messages(chat_id, from_user=me.id, mark_read=False):
        ids_batch.append(msg.id)
        if len(ids_batch) == 100:
            try:
                await client.delete_messages(chat_id, ids_batch)
            except Exception as e:
                print(f".delete xatolik: {e}")
            ids_batch = []
            await asyncio.sleep(1)

    if ids_batch:
        try:
            await client.delete_messages(chat_id, ids_batch)
        except Exception as e:
            print(f".delete xatolik: {e}")


@client.on(events.NewMessage(pattern=r"^\.dell$", outgoing=True))
async def delete_replied_message(event):
    reply = await event.get_reply_message()
    if not reply:
        try:
            await event.delete()
        except Exception:
            pass
        return

    try:
        await client.delete_messages(event.chat_id, [reply.id, event.id])
    except Exception:
        try:
            await event.delete()
        except Exception:
            pass


@client.on(events.NewMessage(pattern=r"^\.tarjima$", outgoing=True))
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

@client.on(events.NewMessage(pattern=r"^\.u(?:\s+(.+))?$", outgoing=True))
async def start_infinite_mention(event):
    chat_id = event.chat_id
    target = event.pattern_match.group(1)

    # Agar reply qilingan bo'lsa va ID yozilmagan bo'lsa
    if not target and event.is_reply:
        reply = await event.get_reply_message()
        if reply and getattr(reply, "sender_id", None):
            target = str(reply.sender_id)

    if not target:
        return await event.edit("Foydalanuvchini kiriting: `.u @username` yoki `.u user_id` yoki xabarga reply qiling.")

    # HTML formatni tayyorlash
    if target.isdigit():
        try:
            user_entity = await client.get_entity(int(target))
            mention_text = f'<a href="tg://user?id={target}">{user_entity.first_name}</a>'
        except Exception:
            mention_text = f'<a href="tg://user?id={target}">Foydalanuvchi</a> '
    else:
        mention_text = f"{target} "

    # Ushbu chatda chaqirishni faollashtirish
    active_spams[chat_id] = True
    await event.delete()

    # Oddiy while sikli orqali cheksiz chaqirish
    try:
        while active_spams.get(chat_id, False):
            await client.send_message(chat_id, mention_text, parse_mode="html")
            await asyncio.sleep(1.5)  # Telegram bloklamasligi uchun cheklov
    except Exception as e:
        print(f"Spam xatolik: {e}")


@client.on(events.NewMessage(pattern=r"^\.stop$", outgoing=True))
async def stop_infinite_mention(event):
    chat_id = event.chat_id
    await event.delete()

    # Agar ushbu chatda spam faol bo'lsa, uni False qilib to'xtatamiz
    if chat_id in active_spams and active_spams[chat_id]:
        active_spams[chat_id] = False
        msg = await client.send_message(chat_id, "Chaqirish muvaffaqiyatli to'xtatildi!")
        await asyncio.sleep(3)
        await msg.delete()
    else:
        msg = await client.send_message(chat_id, "Bu chatda faol chaqirish jarayoni topilmadi.")
        await asyncio.sleep(3)
        await msg.delete()


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
    await client.start()
    print("Userbot ishga tushdi.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
