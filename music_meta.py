"""Metadatos de un album: etiquetas de los audios, caratula embebida, y
artista/album/año deducidos de captions y nombres de archivo."""
import os
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import mutagen

from album_name import PATTERN, YEAR_RE, VARIOUS, from_filenames

# "#rock #indie_pop" o "Género: Rock alternativo"
HASHTAG_RE = re.compile(r"#([^\s#]{3,40})")
GENRE_LINE_RE = re.compile(r"g[eé]nero?s?\s*[:=-]\s*([^\n]+)", re.I)
GENRE_SPLIT_RE = re.compile(r"\s*[;,/|]\s*|\s+-\s+")
# Etiquetas de formato que aparecen como hashtag y no son generos.
NOT_GENRES = {"flac", "wav", "hires", "hi-res", "lossless", "24bit", "16bit", "mp3", "zip",
              "album", "discografia", "discography", "ep", "single", "cd", "vinyl", "vinilo"}
# Palabras de formato/calidad que no son parte del nombre del album.
FORMAT_WORDS_RE = re.compile(
    r"\b(?:flac|wav|alac|aiff|ape|dsd|hi-?res|lossless|web|cd|vinyl|vinilo|"
    r"\d{2}\s?-?\s?bits?|\d{2,3}(?:[.,]\d)?\s?khz|\d{2}\s?[-/]\s?\d{2,3}(?:[.,]\d)?)\b", re.I)
ID3_FRAMES = {"artist": "TPE1", "albumartist": "TPE2", "album": "TALB", "date": "TDRC",
              "originaldate": "TDOR", "genre": "TCON"}
COVER_NAMES = ("cover", "folder", "front", "portada", "caratula", "carátula")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


@dataclass
class AlbumInfo:
    artist: str | None = None
    album: str | None = None
    year: str | None = None
    genres: list[str] = field(default_factory=list)  # de etiquetas o captions

    @property
    def name(self) -> str | None:
        if not self.artist and not self.album:
            return None
        name = " - ".join(x for x in (self.artist, self.album) if x)
        return f"{name} ({self.year})" if self.year else name

    def merge(self, other: "AlbumInfo") -> "AlbumInfo":
        """Completa lo que falte con `other` (self tiene prioridad)."""
        genres = self.genres + [g for g in other.genres if g not in self.genres]
        return AlbumInfo(self.artist or other.artist, self.album or other.album,
                         self.year or other.year, genres)


def split_genres(text: str) -> list[str]:
    out = []
    for part in GENRE_SPLIT_RE.split(text or ""):
        part = part.strip(" .#")
        if part and part.lower() not in NOT_GENRES and not part.isdigit():
            out.append(part)
    return out


# ---------- etiquetas ----------


def _tag(tags, key):
    value = tags.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value).strip() if value else None


def read_tags(path) -> dict:
    """{'artist', 'albumartist', 'album', 'year', 'genres'} (vacio si no hay)."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return {}
    if audio is None or not audio.tags:
        return {}
    tags = audio.tags
    if hasattr(tags, "getall") and not hasattr(tags, "valid_keys"):
        # WAV/AIFF: ID3 crudo aunque se pida easy=True.
        tags = {key: [str(x) for f in tags.getall(frame) for x in f.text]
                for key, frame in ID3_FRAMES.items() if tags.getall(frame)}
    date = _tag(tags, "date") or _tag(tags, "originaldate") or ""
    m = YEAR_RE.search(date)
    genres = []
    for g in tags.get("genre") or []:
        genres += split_genres(str(g))
    return {"artist": _tag(tags, "artist"), "albumartist": _tag(tags, "albumartist"),
            "album": _tag(tags, "album"), "year": m.group(0) if m else None, "genres": genres}


def _majority(values):
    values = [v for v in values if v]
    if not values:
        return None
    value, count = Counter(values).most_common(1)[0]
    return value if count * 2 >= len(values) else None


def info_from_tags(tag_list: list[dict]) -> AlbumInfo:
    album = _majority(t.get("album") for t in tag_list)
    artist = _majority(t.get("albumartist") for t in tag_list) or _majority(t.get("artist") for t in tag_list)
    if album and not artist and any(t.get("artist") for t in tag_list):
        artist = VARIOUS
    genres = Counter(g for t in tag_list for g in t.get("genres", []))
    return AlbumInfo(artist, album, _majority(t.get("year") for t in tag_list),
                     [g for g, _ in genres.most_common()])


def split_by_album(items: list[tuple]) -> list[list[tuple]]:
    """`items`: [(algo, tags)] en orden. Corta en tramos consecutivos con la
    misma etiqueta de album; los que no tienen etiqueta se suman al tramo
    actual. Asi un hilo de audios sueltos con dos discos seguidos da dos ZIP."""
    groups, current, album = [], [], None
    for item in items:
        tag_album = (item[1].get("album") or "").strip().lower() or None
        if current and tag_album and album and tag_album != album:
            groups.append(current)
            current = []
        if tag_album:
            album = tag_album
        current.append(item)
    if current:
        groups.append(current)
    return groups


def extract_cover(audio_path, out_path) -> Path | None:
    """Guarda la caratula embebida en el audio (FLAC, ID3, MP4), si tiene."""
    try:
        audio = mutagen.File(audio_path)
    except Exception:
        return None
    if audio is None:
        return None
    data = None
    pictures = getattr(audio, "pictures", None)  # FLAC
    if pictures:
        front = [p for p in pictures if p.type == 3]
        data = (front or pictures)[0].data
    elif audio.tags is not None:
        tags = audio.tags
        if hasattr(tags, "getall"):  # ID3 (mp3, wav, aiff)
            apic = tags.getall("APIC")
            if apic:
                data = apic[0].data
        elif "covr" in tags:  # MP4/ALAC
            data = bytes(tags["covr"][0])
        elif "metadata_block_picture" in tags:  # Vorbis/Opus
            import base64
            from mutagen.flac import Picture
            data = Picture(base64.b64decode(tags["metadata_block_picture"][0])).data
    if not data:
        return None
    ext = ".png" if data[:4] == b"\x89PNG" else ".jpg"
    out = Path(out_path).with_suffix(ext)
    out.write_bytes(data)
    return out


def peek_zip(zip_path, work_dir, audio_exts) -> tuple[dict, Path | None]:
    """Para un .zip ya descargado: etiquetas del primer audio de adentro y una
    caratula (cover.jpg / folder.jpg… o la embebida en ese audio)."""
    tags, cover = {}, None
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            audios = sorted(n for n in names if Path(n).suffix.lower().lstrip(".") in audio_exts)
            images = [n for n in names if Path(n).suffix.lower() in IMAGE_EXTS]
            named = [n for n in images if Path(n).stem.lower() in COVER_NAMES]
            if named or images:
                pick = (named or images)[0]
                cover = Path(work_dir) / ("zipcover" + Path(pick).suffix.lower())
                cover.write_bytes(zf.read(pick))
            if audios:
                first = Path(work_dir) / ("zipaudio" + Path(audios[0]).suffix.lower())
                with zf.open(audios[0]) as src, open(first, "wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        dst.write(chunk)
                tags = read_tags(first)
                if cover is None:
                    cover = extract_cover(first, Path(work_dir) / "zipembedded")
                first.unlink(missing_ok=True)
    except (zipfile.BadZipFile, OSError, RuntimeError):  # RuntimeError: zip con clave
        pass
    return tags, cover


# ---------- captions y nombres ----------


def _clean_line(line: str) -> str:
    line = re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", line)  # [FLAC 24-96] etc.
    line = re.sub(r"[*_`🎵💿📀🎶]|#\S+", "", line)
    line = FORMAT_WORDS_RE.sub(" ", line)
    # "(FLAC)" sin el FLAC deja "( )", que despues duplicaba el año en el nombre.
    line = re.sub(r"\(\s*\)", " ", line)
    return re.sub(r"\s+", " ", line).strip(" -–—|")


def parse_name(text: str) -> AlbumInfo | None:
    """"Artista - Album (Año)" en alguna linea del texto."""
    for line in (text or "").splitlines():
        line = _clean_line(line)
        m = PATTERN.search(line)
        # "01 - Tema.flac" no es "Artista - Album".
        if m and len(m["album"].strip()) > 1 and not m["artist"].strip().isdigit():
            album = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", m["album"]).strip()
            year = m["year"] or (YEAR_RE.search(m["album"]).group(0) if YEAR_RE.search(m["album"]) else None)
            return AlbumInfo(m["artist"].strip(), album, year)
    return None


TITLE_YEAR_RE = re.compile(r"^(?P<title>.*?[A-Za-zÀ-ÿ].*?)[\s(\[-]*(?P<year>(?:19|20)\d{2})[)\]]?$")


def parse_title(text: str) -> AlbumInfo | None:
    """Compilados sin artista: "Zona De Baile (1992) (FLAC)" -> album "Zona
    De Baile", 1992. Solo si hay un año, para no tomar cualquier texto."""
    for line in (text or "").splitlines():
        m = TITLE_YEAR_RE.search(_clean_line(line))
        if m:
            title = m["title"].strip(" -–—|(.")
            if len(title) >= 3:
                return AlbumInfo(None, title, m["year"])
    return None


def genres_from_text(text: str) -> list[str]:
    out = []
    for m in GENRE_LINE_RE.finditer(text or ""):
        out += split_genres(m.group(1))
    for tag in HASHTAG_RE.findall(text or ""):
        out += split_genres(tag.replace("_", " "))
    return out


def stem_for_parse(filename: str) -> str:
    stem = re.sub(r"(?:\.part\d+)?\.(?:zip|rar|7z)(?:\.\d{3})?$", "", filename, flags=re.I)
    stem = os.path.splitext(stem)[0] if stem == filename else stem
    return stem.replace("_", " ")


def info_from_texts(texts: list[str], filenames: list[str], performers: list[str]) -> AlbumInfo:
    """Lo que se puede deducir sin descargar nada."""
    genres = []
    for t in texts:
        genres += [g for g in genres_from_text(t) if g not in genres]
    for t in texts:
        info = parse_name(t)
        if info:
            info.genres = genres
            return info
    guess = from_filenames(filenames) if len(filenames) > 1 else None
    if guess:
        info = parse_name(guess)
        if info:
            info.genres = genres
            return info
    if len(filenames) == 1:  # un comprimido: "Artista - Album (Año).zip"
        info = parse_name(stem_for_parse(filenames[0]))
        if info:
            info.genres = genres
            return info
    for t in texts + [stem_for_parse(f) for f in filenames[:1] if len(filenames) == 1]:
        info = parse_title(t)
        if info:
            info.artist, info.genres = _majority(performers), genres
            return info
    return AlbumInfo(artist=_majority(performers), genres=genres)
