# userbot

Ushbu repozitoriyda ikkita mustaqil Telegram bot bor:

- **root (`main.py`)** — shaxsiy akkount asosida ishlaydigan Telethon userbot: o'chirilgan
  xabarlarni loglash, `.tarjima`, `.ad`/`.unad`, `.delete`/`.del`, username kuzatish va
  StarGift yashirish/ko'rsatish. O'rnatish uchun `generate_session.py` bilan `SESSION_STRING`
  oling va `API_ID`, `API_HASH`, `SESSION_STRING`, `LOG_CHANNEL_ID` muhit o'zgaruvchilarini
  sozlang.
- **`musicbot/`** — @BotFather orqali yaratiladigan alohida Bot API bot: musiqa qidirish,
  Instagram/TikTok/Pinterest/YouTube havolalaridan video/rasm/musiqa yuklash, video'ni aylana
  videoga aylantirish, musiqa teglarini o'zgartirish va guruhda `.id`. Batafsil: `musicbot/README.md`.

Ikkalasi ham bir-biridan mustaqil bo'lib, alohida-alohida (masalan, alohida Render Web Service
sifatida) ishga tushiriladi.
