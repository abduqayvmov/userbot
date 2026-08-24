# Music bot

@BotFather orqali yaratilgan alohida Telegram Bot API bot (userbot'dan mustaqil). aiogram, yt-dlp
va ffmpeg (imageio-ffmpeg orqali) asosida ishlaydi.

## Imkoniyatlari

- Shaxsiy chatda musiqa nomini yozsangiz - YouTube'dan qidirib, MP3 qilib yuboradi.
- Instagram / TikTok / Pinterest / YouTube havolasini tashlasangiz - "Video / Rasm / Musiqa"
  tugmalari chiqadi, tanlaganingizni yuklab beradi.
- Har qanday video yuborsangiz - aylana video (video note, maksimal 60 soniya) qilib qaytaradi.
- Musiqa fayl (MP3) yuborsangiz - yangi nom va ijrochini so'rab, ID3 teglarini o'zgartirib beradi.
- Guruhda `.id` - reply qilingan foydalanuvchining ID'sini chiqaradi.

Erkin matn orqali musiqa qidirish faqat shaxsiy chatda ishlaydi (guruhda har bir xabarni qidiruvga
aylantirmaslik uchun). Havola-tugmalar, aylana video va teg o'zgartirish guruhda ham ishlaydi.

## O'rnatish

```bash
cd musicbot
pip install -r requirements.txt
BOT_TOKEN=... python bot.py
```

Muhit o'zgaruvchilari:

- `BOT_TOKEN` - @BotFather'dan olingan bot tokeni (majburiy).
- `PORT` - Render kabi platformalarda health-check uchun ochiladigan port (ixtiyoriy, standart 8080).

## Guruhda ishlashi uchun

Bot guruhga qo'shilganda **admin** qilib qo'yiladi. Telegram bot API'da admin bo'lgan botlar
"privacy mode" cheklovidan qat'i nazar guruhdagi barcha xabarlarni ko'ra oladi - shu sababli
`.id`, havola aniqlash va video/musiqa funksiyalari guruhda ishlashi uchun bu shart.

## Deploy (Render)

Bu papkani alohida Web Service sifatida deploy qiling (Root Directory: `musicbot`, Start Command:
`python bot.py`). `runtime.txt` Python versiyasini belgilaydi, `requirements.txt` esa
kerakli kutubxonalarni. ffmpeg tizim paketi sifatida o'rnatilishi shart emas -
`imageio-ffmpeg` orqali statik binary avtomatik yuklab olinadi.
