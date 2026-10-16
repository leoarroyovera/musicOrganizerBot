"""Descarga, armado de ZIP y subida (adaptado de rateroBot/transfer.py)."""
import asyncio
import os
import shutil
import time
import zipfile
from pathlib import Path

from telethon import errors
from telethon.tl.types import MessageMediaWebPage

import config
from fast_download import download_document_fast
from fast_upload import upload_file_fast

MAX_CAPTION = 1024
FAST_DOWNLOAD_MIN_BYTES = 10 * 1024 * 1024
ALBUM_SIZE = 10  # maximo de fotos por album en Telegram
MAX_PHOTO_BYTES = 10 * 1024 * 1024
ZIP_ENTRY_OVERHEAD = 1024
MIB = 1024 * 1024


def has_file(msg) -> bool:
    return msg is not None and msg.file is not None and not isinstance(msg.media, MessageMediaWebPage)


def file_size(msg) -> int:
    return (msg.file.size or 0) if has_file(msg) else 0


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024


def ensure_disk_space(needed: int) -> None:
    os.makedirs(config.TMP_DIR, exist_ok=True)
    free = shutil.disk_usage(config.TMP_DIR).free
    if free < needed + config.DISK_MARGIN_MB * MIB:
        raise RuntimeError(f"sin espacio en disco: necesito {human(needed)} y hay {human(free)} libres")


async def safe_run(coro_fn, *args, max_retries=5, **kwargs):
    for attempt in range(1, max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except errors.FloodWaitError as e:
            print(f"  [FloodWait] esperando {e.seconds + 1}s (intento {attempt}/{max_retries})")
            await asyncio.sleep(e.seconds + 1)
    raise RuntimeError(f"Excedido max_retries ({max_retries}) tras FloodWait repetido")


def clip(text: str, limit: int = MAX_CAPTION) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------- mensaje de estado ----------


class Reporter:
    """Mensaje de estado que se edita como mucho una vez cada `interval`
    segundos (para no chocar con FloodWait)."""

    def __init__(self, status_msg, header, buttons=None, interval=4.0):
        self.status_msg = status_msg
        self.header = header
        self.buttons = buttons
        self.interval = interval
        self.last = 0.0
        self.last_text = None

    async def set(self, text, force=False):
        now = time.monotonic()
        if not force and now - self.last < self.interval:
            return
        full = f"{self.header}\n\n{text}" if text else self.header
        if full == self.last_text:
            return
        self.last, self.last_text = now, full
        try:
            await self.status_msg.edit(full, buttons=self.buttons)
        except (errors.MessageNotModifiedError, errors.FloodWaitError):
            pass

    def callback(self, verb):
        start = time.monotonic()

        async def cb(done, total):
            span = max(time.monotonic() - start, 1e-6)
            pct = f" ({done * 100 / total:.0f}%)" if total else ""
            await self.set(f"{verb}: {human(done)} / {human(total or 0)}{pct} a {human(done / span)}/s")

        return cb


# ---------- descarga ----------


def _file_name(msg) -> str:
    if msg.file.name:
        return os.path.basename(msg.file.name)
    kind = "imagen" if msg.photo else "audio" if msg.audio else "archivo"
    return f"{kind}_{msg.id}{msg.file.ext or ''}"


async def _download_once(client, msg, dest_dir, progress_cb):
    doc = msg.document
    if doc and doc.size >= FAST_DOWNLOAD_MIN_BYTES:
        path = os.path.join(dest_dir, _file_name(msg))
        try:
            return await safe_run(download_document_fast, client, msg, path,
                                  progress_callback=progress_cb)
        except errors.FileReferenceExpiredError:
            raise
        except Exception as e:
            print(f"  descarga paralela fallo ({e!r}), uso la estandar")
            if os.path.exists(path):
                os.remove(path)
    if msg.photo:
        return await safe_run(client.download_media, msg,
                              file=os.path.join(dest_dir, _file_name(msg)),
                              progress_callback=progress_cb)
    return await safe_run(client.download_media, msg, file=dest_dir + os.sep,
                          progress_callback=progress_cb)


async def download(client, msg, dest_dir, progress_cb=None):
    """Descarga `msg` a `dest_dir` y devuelve (msg, Path). Si la referencia
    del archivo vencio (~1h) relee el mensaje y reintenta."""
    os.makedirs(dest_dir, exist_ok=True)
    try:
        path = await _download_once(client, msg, dest_dir, progress_cb)
    except errors.FileReferenceExpiredError:
        msg = await client.get_messages(await msg.get_input_chat(), ids=msg.id)
        if not has_file(msg):
            raise RuntimeError("el mensaje ya no esta disponible")
        path = await _download_once(client, msg, dest_dir, progress_cb)
    if not path:
        raise RuntimeError("la descarga no devolvio archivo")
    return msg, Path(path)


# ---------- ZIP ----------


def _unique_arcname(name: str, used: set) -> str:
    stem, ext = os.path.splitext(name)
    candidate, n = name, 1
    while candidate.lower() in used:
        n += 1
        candidate = f"{stem} ({n}){ext}"
    used.add(candidate.lower())
    return candidate


def build_zips(files: list[Path], out_dir: str, base_name: str, max_bytes: int) -> list[Path]:
    """Empaqueta `files` (en orden) en uno o mas ZIP sin compresion (FLAC/WAV
    no ganan casi nada comprimiendo), partiendo cuando una parte superaria
    `max_bytes`. Bloqueante: llamar con asyncio.to_thread."""
    groups, current, size = [], [], 0
    for f in files:
        fsize = f.stat().st_size + ZIP_ENTRY_OVERHEAD
        if current and size + fsize > max_bytes:
            groups.append(current)
            current, size = [], 0
        current.append(f)
        size += fsize
    if current:
        groups.append(current)

    zips = []
    for i, group in enumerate(groups, 1):
        suffix = f"_parte{i}" if len(groups) > 1 else ""
        zip_path = Path(out_dir) / f"{base_name}{suffix}.zip"
        used = set()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for f in group:
                zf.write(f, arcname=_unique_arcname(f.name, used))
        zips.append(zip_path)
    return zips


# ---------- subida ----------


async def send_document(up_client, dest, path: Path, caption, topic_id, reporter=None):
    cb = reporter.callback(f"⬆️ subiendo {path.name}") if reporter else None
    handle = await safe_run(upload_file_fast, up_client, path, progress_callback=cb)
    await safe_run(up_client.send_file, dest, handle, caption=clip(caption or ""),
                   parse_mode=None, reply_to=topic_id, file_size=path.stat().st_size,
                   force_document=True)


def photo_ok(path: Path) -> bool:
    return path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and path.stat().st_size <= MAX_PHOTO_BYTES


async def send_covers(up_client, dest, paths: list[Path], caption, topic_id):
    """Publica las caratulas como foto (o album de fotos, con el caption en
    la primera). Las que Telegram no acepta como foto van como documento."""
    paths = [p for p in paths if photo_ok(p)][:ALBUM_SIZE]
    if not paths:
        return False
    files = [str(p) for p in paths]
    captions = [clip(caption)] + [""] * (len(files) - 1)
    try:
        await safe_run(up_client.send_file, dest, files if len(files) > 1 else files[0],
                       caption=captions if len(files) > 1 else captions[0],
                       parse_mode=None, reply_to=topic_id)
    except (errors.PhotoSaveFileInvalidError, errors.PhotoInvalidDimensionsError,
            errors.PhotoExtInvalidError):
        await safe_run(up_client.send_file, dest, files if len(files) > 1 else files[0],
                       caption=captions if len(files) > 1 else captions[0],
                       parse_mode=None, reply_to=topic_id, force_document=True)
    return True
