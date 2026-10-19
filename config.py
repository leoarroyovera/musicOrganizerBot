import os

from dotenv import load_dotenv

load_dotenv()

API_ID = os.environ.get("TG_API_ID")
API_HASH = os.environ.get("TG_API_HASH")

if not API_ID or not API_HASH:
    raise RuntimeError("TG_API_ID y TG_API_HASH deben estar definidos en .env")

API_ID = int(API_ID)

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")

_owner_id = os.environ.get("TG_OWNER_ID")
OWNER_ID = int(_owner_id) if _owner_id else None

# Supergrupo destino (con Temas): link de invitacion, @usuario o id.
DEST_CHAT = os.environ.get("DESTINO", "https://t.me/+--D9DFr8cqgzZDAx").strip()

USER_SESSION_NAME = "organizer_user"
BOT_SESSION_NAME = "organizer_bot"
TMP_DIR = "tmp"
LOG_DIR = "logs"
DATA_DIR = "data"
SOURCES_DIR = os.path.join(DATA_DIR, "fuentes")
PENDING_PATH = os.path.join(DATA_DIR, "pendientes.json")
SENT_PATH = os.path.join(DATA_DIR, "enviados.json")
LEARNED_PATH = os.path.join(DATA_DIR, "aprendido.json")
LOOKUP_CACHE_PATH = os.path.join(DATA_DIR, "cache_busquedas.json")
COVER_SIDES_PATH = os.path.join(DATA_DIR, "portadas.json")
# Familias de generos y sinonimos para emparejar con los temas (editable).
GENRES_PATH = "generos.json"


def _csv(name, default):
    raw = os.environ.get(name, default)
    return {x.strip().lower().lstrip(".") for x in raw.split(",") if x.strip()}


# Audios sueltos que se juntan en un ZIP. El grupo destino es WAV/FLAC, asi
# que por defecto solo formatos sin perdida (mp3, aac, ogg… se ignoran).
AUDIO_EXTS = _csv("EXTENSIONES_AUDIO", "flac,wav,aiff,aif,ape,wv,dsf,dff,alac")
ARCHIVE_EXTS = {"zip", "rar", "7z"}

# Dos audios separados por mas de esto (minutos) no son del mismo album.
GAP_MINUTES = int(os.environ.get("PAUSA_MINUTOS", "30"))
# Los albumes cuyo ultimo mensaje tiene menos de esto (minutos) se dejan para
# la proxima corrida: puede que todavia los esten subiendo.
MIN_AGE_MINUTES = int(os.environ.get("ESPERA_MINUTOS", "15"))

# Limite de subida en MiB. Vacio = automatico (4000 si es Premium, 2000 si no).
_max_upload = os.environ.get("MAX_UPLOAD_MB", "").strip()
MAX_UPLOAD_MB = int(_max_upload) if _max_upload else None
_zip_max = os.environ.get("ZIP_MAX_MB", "").strip()
ZIP_MAX_MB = int(_zip_max) if _zip_max else None
DISK_MARGIN_MB = int(os.environ.get("MARGEN_DISCO_MB", "500"))

# MusicBrainz pide un User-Agent con nombre de la app y un contacto.
MB_CONTACT = os.environ.get("CONTACTO_MUSICBRAINZ", "").strip()
USER_AGENT = f"musicOrganizerBot/1.0 ( {MB_CONTACT} )" if MB_CONTACT else "musicOrganizerBot/1.0"

# Ultimo recurso antes de dejar un album pendiente: Claude con busqueda web.
# Se activa si hay ANTHROPIC_API_KEY (o BUSQUEDA_CLAUDE=1); BUSQUEDA_CLAUDE=0
# lo apaga.
_claude = os.environ.get("BUSQUEDA_CLAUDE", "").strip()
USE_CLAUDE = _claude == "1" or (_claude != "0" and bool(os.environ.get("ANTHROPIC_API_KEY")))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5").strip()

# Portada: "grupo" (default) usa la imagen del grupo si se verifica que es de
# ese disco, y si no la que trae adentro o la de internet; "internet" usa
# primero la de internet (mejor calidad) cuando se encuentra el album exacto.
COVER_PREF = os.environ.get("PORTADA_PREFERIDA", "grupo").strip().lower()

TOPICS_PER_PAGE = 8
