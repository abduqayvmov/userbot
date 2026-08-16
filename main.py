import os
import asyncio
import threading
from flask import Flask
import requests
from io import BytesIO
from datetime import datetime
from telethon import TelegramClient, events
from telethon.tl.functions.channels import EditAdminRequest
from telethon.tl.types import ChatAdminRights

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = "antidelete_session"
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
TARGET_LANG = "uz"

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
private_message_cache = {}
user_tags = {}


def is_media_message(message):
    return bool(message.photo or message.video or message.video_note or message.voice or message.sticker)


async def download_to_memory(message):
    try:
        data = await client.download_media(message, file=bytes)
        if not data:
            return None
        bio = BytesIO(data)
        if message.video_note:
            bio.name = "video_note.mp4"
        elif message.voice:
            bio.name = "voice.ogg"
        elif message.sticker:
            bio.name = "sticker.webp"
        elif message.video:
            bio.name = "video.mp4"
        elif message.photo:
            bio.name = "photo.jpg"
        else:
            bio.name = "file.bin"
        return bio
    except Exception as e:
        print(f"Media yuklashda xatolik: {e}")
        return None


@client.on(events.NewMessage())
async def cache_private_messages(event):
    if event.is_private:
        media_bio = None
        is_round = False
        if event.media and is_media_message(event.message):
            is_round = bool(event.video_note)
            media_bio = await download_to_memory(event.message)

        private_message_cache[event.id] = {
            "text": event.raw_text,
            "sender_id": event.sender_id,
            "chat_id": event.chat_id,
            "media_bio": media_bio,
            "is_round": is_round,
            "date": event.date,
        }
        if len(private_message_cache) > 300:
            oldest_key = next(iter(private_message_cache))
            private_message_cache.pop(oldest_key, None)


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
        caption = (
            f"O'chirilgan shaxsiy xabar\n"
            f"Kimdan: {sender_name} ({data['sender_id']})\n"
            f"Vaqt: {data['date']}\n"
            f"Matn: {data['text'] or '(matn yoq)'}"
        )
        try:
            if data["media_bio"]:
                data["media_bio"].seek(0)
                await client.send_file(
                    LOG_CHANNEL_ID, data["media_bio"], caption=caption,
                    video_note=data["is_round"],
                )
            else:
                await client.send_message(LOG_CHANNEL_ID, caption)
        except Exception as e:
            print(f"Log kanalga yuborishda xatolik: {e}")


@client.on(events.NewMessage(outgoing=True))
async def save_media_via_reply(event):
    if not event.is_private or not LOG_CHANNEL_ID:
        return
    if not event.is_reply:
        return
    reply = await event.get_reply_message()
    if not reply or not reply.media or reply.out:
        return
    if not is_media_message(reply):
        return
    is_photo = bool(reply.photo)
    is_video = bool(reply.video)
    is_round = bool(reply.video_note)
    is_voice = bool(reply.voice)
    is_sticker = bool(reply.sticker)
    try:
        sender = await reply.get_sender()
        sender_name = getattr(sender, "first_name", "Noma'lum")
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
        caption = f"{kind} (reply orqali saqlandi)\nKimdan: {sender_name} ({reply.sender_id})"
        bio = await download_to_memory(reply)
        if not bio:
            print("Reply media: yuklab bolmadi (bo'sh)")
            return
        bio.seek(0)
        await client.send_file(LOG_CHANNEL_ID, bio, caption=caption, video_note=is_round)
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


@client.on(events.NewMessage(pattern=r"^\.delete$", outgoing=True))
async def delete_my_messages(event):
    if event.is_private:
        return await event.edit("Bu buyruq faqat guruhda ishlaydi.")
    chat = await event.get_chat()
    me = await client.get_me()
    await event.edit("Ochirilyapti...")
    deleted_count = 0
    ids_batch = []
    async for msg in client.iter_messages(chat, from_user=me.id):
        ids_batch.append(msg.id)
        if len(ids_batch) == 100:
            await client.delete_messages(chat, ids_batch)
            deleted_count += len(ids_batch)
            ids_batch = []
            await asyncio.sleep(1)
    if ids_batch:
        await client.delete_messages(chat, ids_batch)
        deleted_count += len(ids_batch)
    try:
        await client.send_message(chat, f"{deleted_count} ta xabar ochirildi.")
    except Exception:
        pass


@client.on(events.NewMessage(pattern=r"^\.kurs$", outgoing=True))
async def show_rates(event):
    await event.edit("Kurs olinmoqda...")
    try:
        usd_resp = requests.get("https://cbu.uz/oz/arkhiv-kursov-valyut/json/USD/", timeout=10)
        usd_rate = float(usd_resp.json()[0]["Rate"])
        ton_resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd",
            timeout=10,
        )
        ton_usd = ton_resp.json()["the-open-network"]["usd"]
        ton_uzs = ton_usd * usd_rate
        text = (
            f"Kurs ({datetime.now().strftime('%d.%m.%Y %H:%M')})\n\n"
            f"1$ - {usd_rate:,.0f} som\n"
            f"1 TON - {ton_uzs:,.0f} som (${ton_usd:.2f})"
        ).replace(",", " ")
        await event.edit(text)
    except Exception as e:
        await event.edit(f"Kursni olib bolmadi: {e}")


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


fake_server = Flask(__name__)


@fake_server.route("/")
def home():
    return "Bot ishlab turibdi."


def run_fake_server():
    port = int(os.getenv("PORT", 8080))
    fake_server.run(host="0.0.0.0", port=port)


async def main():
    threading.Thread(target=run_fake_server, daemon=True).start()
    await client.start()
    print("Userbot ishga tushdi.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
