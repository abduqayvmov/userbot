"""
Bir martalik skript: SESSION_STRING generatsiya qilish uchun.

Buni FAQAT o'z kompyuteringizda ishga tushiring (Render'da yoki serverda emas) —
telefon raqamingiz va SMS/Telegram kodini so'raydi.

Ishlatish:
    pip install telethon
    API_ID=... API_HASH=... python generate_session.py

Natijada chiqqan qatorni Render'dagi xizmatning SESSION_STRING muhit
o'zgaruvchisiga qo'ying. Bu qatorni hech kimga bermang va git'ga
commit qilmang - u to'liq akkount kirish huquqini beradi.
"""
import os

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(os.getenv("API_ID") or input("API_ID: "))
api_hash = os.getenv("API_HASH") or input("API_HASH: ")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    session_string = client.session.save()
    print("\nSESSION_STRING (buni Render'ning muhit o'zgaruvchilariga qo'ying):\n")
    print(session_string)
