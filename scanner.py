"""Recorre los mensajes de un grupo origen y los agrupa en "unidades": un
album de audios sueltos (que se va a zipear) o un comprimido (zip/rar/7z,
con todas sus partes), cada uno con sus caratulas cercanas."""
import hashlib
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeSticker,
    MessageMediaWebPage,
)

import config
from music_meta import AlbumInfo, info_from_texts, parse_name

IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp")
IMAGE_EXTS = ("jpg", "jpeg", "png", "webp")
# album.zip, album.rar, album.part2.rar, album.7z.001, album.r00, album.z01
ARCHIVE_RE = re.compile(r"\.(?:zip|rar|7z)(?:\.\d{3})?$|\.part\d+\.rar$|\.[rz]\d{2}$", re.I)
ARCHIVE_PART_RE = re.compile(r"(?:\.part\d+)?\.(?:rar|zip|7z)(?:\.\d{3})?$|\.[rz]\d{2}$", re.I)


def _ext(name: str) -> str:
    return os.path.splitext(name or "")[1].lower().lstrip(".")


def kind_of(msg) -> str:
    """imagen | audio | archivo | texto | otro (audio con perdida, video,
    sticker… "otro" corta un album en curso)."""
    if msg.photo:
        return "imagen"
    doc = msg.document
    if doc is None or isinstance(msg.media, MessageMediaWebPage):
        text_only = msg.media is None or isinstance(msg.media, MessageMediaWebPage)
        return "texto" if msg.message and text_only else "otro"
    name = msg.file.name or ""
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeSticker):
            return "otro"
        if isinstance(attr, DocumentAttributeAudio) and attr.voice:
            return "otro"
    if doc.mime_type in IMAGE_MIMES or _ext(name) in IMAGE_EXTS:
        return "imagen"
    if ARCHIVE_RE.search(name):
        return "archivo"
    ext = _ext(name) or (msg.file.ext or "").lstrip(".").lower()
    if ext in config.AUDIO_EXTS:
        return "audio"
    return "otro"


def archive_base(name: str) -> str:
    return ARCHIVE_PART_RE.sub("", (name or "").lower())


def thread_of(msg, forum: bool) -> int:
    if not forum:
        return 0
    r = msg.reply_to
    if r is not None and getattr(r, "forum_topic", False):
        return r.reply_to_top_id or r.reply_to_msg_id
    return 1  # General


@dataclass
class Unit:
    kind: str  # "audio" | "archivo"
    files: list = field(default_factory=list)
    covers: list = field(default_factory=list)
    hints: list = field(default_factory=list)  # textos sueltos que lo presentan
    hint_ids: list = field(default_factory=list)
    base: str = ""

    @property
    def first_id(self):
        return self.files[0].id

    @property
    def min_id(self):
        return min([m.id for m in self.files + self.covers] + self.hint_ids)

    @property
    def last_date(self):
        return max(m.date for m in self.files + self.covers)

    @property
    def size(self):
        return sum(m.file.size or 0 for m in self.files)

    def key(self) -> str:
        """Identifica el contenido (nombres y tamaños) para no mandar dos veces
        el mismo album aunque venga de otro grupo."""
        parts = sorted(f"{(m.file.name or '').lower()}:{m.file.size}" for m in self.files)
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]

    def texts(self) -> list[str]:
        return [t for t in self.hints + [m.message for m in self.covers + self.files] if t]

    def guess(self) -> AlbumInfo:
        performers = []
        for m in self.files:
            for attr in m.document.attributes:
                if isinstance(attr, DocumentAttributeAudio) and attr.performer:
                    performers.append(attr.performer)
        names = [m.file.name or "" for m in self.files]
        if self.kind == "archivo":
            names = names[:1]  # las partes de un rar repiten el nombre
        return info_from_texts(self.texts(), names, performers)

    def label(self) -> str:
        info = self.guess()
        if info.name:
            return info.name
        if self.kind == "archivo":
            return self.files[0].file.name or f"archivo {self.first_id}"
        return f"{len(self.files)} audio(s) desde el mensaje {self.first_id}"

    def to_ref(self) -> dict:
        return {"kind": self.kind, "files": [m.id for m in self.files],
                "covers": [m.id for m in self.covers], "hints": self.hints, "hint_ids": self.hint_ids}


def group_messages(msgs: list, gap: timedelta) -> list[Unit]:
    """`msgs`: mensajes de un mismo hilo, en orden cronologico."""
    units: list[Unit] = []
    cur: Unit | None = None
    loose: list = []  # imagenes que todavia no se sabe de que album son
    hint = None  # mensaje de texto "Artista - Album (Año)"

    def place_loose(next_unit: Unit | None, next_date=None):
        # Cada imagen va con el album mas cercano en el tiempo (antes o despues),
        # si alguno esta a menos de `gap`.
        for img in loose:
            d_prev = img.date - cur.last_date if cur else None
            d_next = next_date - img.date if next_date else None
            prev_ok = d_prev is not None and d_prev <= gap
            next_ok = d_next is not None and d_next <= gap
            if prev_ok and (not next_ok or d_prev < d_next):
                cur.covers.append(img)
            elif next_ok:
                next_unit.covers.append(img)
        loose.clear()

    for m in msgs:
        k = kind_of(m)
        if k == "imagen":
            # Un cover.jpg mandado en el mismo album de Telegram que los audios.
            if cur and m.grouped_id and any(f.grouped_id == m.grouped_id for f in cur.files):
                cur.covers.append(m)
            else:
                loose.append(m)
            continue

        if k in ("audio", "archivo"):
            base = archive_base(m.file.name) if k == "archivo" else ""
            same_album = m.grouped_id and cur and any(f.grouped_id == m.grouped_id for f in cur.files)
            continues = cur is not None and cur.kind == k and (same_album or (
                not loose and hint is None and m.date - cur.last_date <= gap
                and (k == "audio" or base == cur.base)))
            if continues:
                cur.files.append(m)
                continue
            new = Unit(kind=k, files=[m], base=base)
            place_loose(new, m.date)
            if hint:
                new.hints.append(hint.message)
                new.hint_ids.append(hint.id)
                hint = None
            if cur:
                units.append(cur)
            cur = new
            continue

        if k == "texto":
            # "Artista - Album (Año)" suelto: presenta al album que sigue (las
            # imagenes de justo antes siguen esperando a ese album).
            if parse_name(m.message):
                hint = m
            continue

        # Otro tipo de media: corta el album en curso.
        place_loose(None)
        hint = None
        if cur:
            units.append(cur)
            cur = None

    place_loose(None)
    if cur:
        units.append(cur)
    return units


async def scan(client, entity, min_id: int, forum: bool):
    """Mensajes con id > min_id, agrupados por hilo (tema) y en unidades.
    Devuelve (unidades ordenadas por primer mensaje, id del ultimo mensaje)."""
    threads: dict[int, list] = {}
    last_id = min_id
    async for msg in client.iter_messages(entity, reverse=True, min_id=min_id):
        last_id = max(last_id, msg.id)
        if msg.action is not None:
            continue
        threads.setdefault(thread_of(msg, forum), []).append(msg)
    gap = timedelta(minutes=config.GAP_MINUTES)
    units = []
    for msgs in threads.values():
        units += group_messages(msgs, gap)
    units.sort(key=lambda u: u.first_id)
    return units, last_id


def summarize(units: list[Unit]) -> str:
    kinds = Counter(u.kind for u in units)
    covers = sum(1 for u in units if u.covers)
    return (f"{kinds.get('audio', 0)} álbum(es) de audios sueltos, "
            f"{kinds.get('archivo', 0)} comprimido(s), {covers} con carátula")
