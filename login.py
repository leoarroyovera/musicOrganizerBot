"""Login interactivo de tu cuenta de usuario (telefono, codigo, 2FA).

Se corre una sola vez en una terminal real; deja la sesion guardada en
organizer_user.session y bot.py la reutiliza.
"""
from telethon.sync import TelegramClient

import config

with TelegramClient(config.USER_SESSION_NAME, config.API_ID, config.API_HASH) as client:
    me = client.get_me()
    print(f"Sesion lista como {me.first_name} (id {me.id}). Ya podes correr bot.py")
