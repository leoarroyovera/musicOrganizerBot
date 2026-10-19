"""Deduce el nombre del ZIP a partir de los archivos del lote, con la forma
"Artista - Album (Año)" (lo usual cuando el lote es un disco de musica).

Fuentes, en orden: etiquetas de los archivos de audio (ID3, Vorbis, MP4…),
captions de los mensajes y nombres de archivo. Si ninguna da un resultado
claro devuelve None y se usa el nombre automatico de siempre.
"""
import os
import re
from collections import Counter

try:
    import mutagen
except ImportError:  # sin mutagen solo se usan captions y nombres de archivo
    mutagen = None

# "Artista - Album (2001)" / "Artista – Album [2001]"; el año es opcional.
PATTERN = re.compile(
    r"(?P<artist>[^\n\-–—]+?)\s+[-–—]\s+(?P<album>[^\n]+?)"
    r"(?:\s*[\(\[](?P<year>(?:19|20)\d{2})[\)\]])?\s*$"
)
YEAR_RE = re.compile(r"(?:19|20)\d{2}")
VARIOUS = "Varios artistas"


def clean_name(text: str) -> str:
    """Nombre de archivo valido (sin .zip ni caracteres prohibidos)."""
    # "Album: Subtitulo" -> "Album - Subtitulo" (":" no vale en un nombre de archivo).
    text = re.sub(r"\s*:\s+", " - ", text)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text).strip()
    if name.lower().endswith(".zip"):
        name = name[:-4]
    return name.strip(" .")[:100]


def _format(artist, album, year) -> str:
    name = f"{artist.strip()} - {album.strip()}"
    return f"{name} ({year})" if year else name


def _majority(values):
    """Valor mas comun si aparece en al menos la mitad de los casos."""
    values = [v for v in values if v]
    if not values:
        return None
    value, count = Counter(values).most_common(1)[0]
    return value if count * 2 >= len(values) else None


def _tag(tags, key):
    value = tags.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value).strip() if value else None


# WAV y AIFF guardan ID3, pero mutagen.File(easy=True) no lo traduce a las
# claves "album", "artist"… como hace con MP3: se mapean los frames a mano.
ID3_FRAMES = {"artist": "TPE1", "albumartist": "TPE2", "album": "TALB",
              "date": "TDRC", "originaldate": "TDOR"}


def _easy_tags(tags):
    if hasattr(tags, "getall") and not hasattr(tags, "valid_keys"):
        return {key: [str(x) for f in tags.getall(frame) for x in f.text]
                for key, frame in ID3_FRAMES.items() if tags.getall(frame)}
    return tags


def from_tags(paths) -> str | None:
    if mutagen is None:
        return None
    albums, artists, album_artists, years = [], [], [], []
    for path in paths:
        try:
            audio = mutagen.File(path, easy=True)
        except Exception:
            continue
        if audio is None or not audio.tags:
            continue
        tags = _easy_tags(audio.tags)
        albums.append(_tag(tags, "album"))
        artists.append(_tag(tags, "artist"))
        album_artists.append(_tag(tags, "albumartist"))
        date = _tag(tags, "date") or _tag(tags, "originaldate") or ""
        m = YEAR_RE.search(date)
        years.append(m.group(0) if m else None)

    album = _majority(albums)
    if not album:
        return None
    artist = _majority(album_artists) or _majority(artists)
    if not artist:
        # Un album con un artista distinto por tema: compilado.
        artist = VARIOUS if any(artists) else None
    if not artist:
        return None
    return _format(artist, album, _majority(years))


def _match(text):
    for line in (text or "").splitlines():
        m = PATTERN.search(line.strip())
        if m and len(m["album"]) > 1:
            return _format(m["artist"], m["album"], m["year"])
    return None


def from_captions(captions) -> str | None:
    # Basta con un caption con la forma completa (suele ir en el primero).
    for caption in captions:
        name = _match(caption)
        if name and YEAR_RE.search(name):
            return name
    return None


def from_filenames(names) -> str | None:
    """Solo si la mayoria de los archivos comparte el mismo "Artista - Album
    (Año)" en su nombre (p.ej. "Artista - Album (2001) - 01 - Tema.mp3")."""
    found = []
    for name in names:
        stem = os.path.splitext(os.path.basename(name))[0].replace("_", " ")
        # Se prueba cortando por " - " desde el final hasta encontrar el año.
        parts = re.split(r"\s+[-–—]\s+", stem)
        guess = None
        for end in range(len(parts), 1, -1):
            candidate = " - ".join(parts[:end])
            m = PATTERN.search(candidate)
            if m and m["year"]:
                guess = _format(m["artist"], m["album"], m["year"])
                break
        found.append(guess)
    return _majority(found) if len([f for f in found if f]) * 2 >= len(found) else None


def guess_album_name(paths, captions) -> str | None:
    """`paths`: archivos descargados; `captions`: textos de los mensajes."""
    name = from_tags(paths) or from_captions(captions) or from_filenames(paths)
    return clean_name(name) if name else None
