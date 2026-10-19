"""Decidir si una imagen del grupo es la portada de un disco.

Una imagen cerca de un ZIP puede ser de ese disco o del vecino. Para saberlo:
- el caption: si dice "Artista - Album" se compara con el disco;
- la huella visual (dHash): se compara con la caratula que trae el disco
  adentro (embebida en el FLAC o cover.jpg del ZIP) o, si no trae, con la
  de internet. La huella aguanta cambios de tamaño y de compresion (Telegram
  recomprime las fotos), pero no otra imagen distinta.
"""
import difflib

from music_meta import AlbumInfo, parse_name, parse_title
from topic_match import norm

try:
    from PIL import Image
except ImportError:  # sin Pillow no hay comparacion visual
    Image = None

HASH_SIZE = 8
# Bits distintos (de 64) hasta los que dos imagenes se consideran la misma.
MAX_DISTANCE = 10


def image_hash(path) -> int | None:
    """dHash: la imagen en grises a 9x8 y, por fila, si cada pixel es mas
    claro que el siguiente."""
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            small = img.convert("L").resize((HASH_SIZE + 1, HASH_SIZE), Image.LANCZOS)
            px = list(small.getdata())
    except Exception:
        return None
    bits = 0
    for row in range(HASH_SIZE):
        for col in range(HASH_SIZE):
            left = px[row * (HASH_SIZE + 1) + col]
            right = px[row * (HASH_SIZE + 1) + col + 1]
            bits = (bits << 1) | (left > right)
    return bits


def same_image(a: int | None, b: int | None) -> bool:
    return a is not None and b is not None and bin(a ^ b).count("1") <= MAX_DISTANCE


def _close(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    return a == b or a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def caption_verdict(caption: str, info: AlbumInfo) -> bool | None:
    """True si el caption nombra a este disco, False si nombra a otro, None
    si no dice nada util (sin caption, o sin datos del disco)."""
    if not caption or not (info.album or info.artist):
        return None
    named = parse_name(caption) or parse_title(caption)
    if named is None or not named.album:
        return None
    if info.album:
        return _close(named.album, info.album) or _close(caption, info.album)
    return _close(named.artist or "", info.artist or "")
