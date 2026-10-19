"""Orquestacion: resolver el grupo origen, escanearlo, y por cada album
descargarlo, armar el ZIP, decidir el tema de WAV/FLAC y publicarlo."""
import asyncio
import os
import random
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from telethon import utils
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    CreateForumTopicRequest,
    GetForumTopicsRequest,
    ImportChatInviteRequest,
)
from telethon.tl.types import (
    ChatInviteAlready,
    ChatInvitePeek,
    MessageActionTopicCreate,
    PeerChannel,
)

import config
import store as store_mod
from album_name import clean_name, guess_album_name
from claude_pick import ClaudeUnavailable, pick_family
from covers import caption_verdict, image_hash, same_image
from genre_lookup import WEIGHTS as SOURCE_WEIGHTS
from genre_lookup import GenreLookup
from media import (
    MIB,
    build_zips,
    clip,
    download,
    ensure_disk_space,
    human,
    safe_run,
    send_covers,
    send_document,
)
from music_meta import AlbumInfo, extract_cover, info_from_tags, peek_zip, read_tags, split_by_album
from scanner import Unit, scan, summarize
from topic_match import ARTIST_SUFFIX, Matcher, Topic, norm

INVITE_RE = re.compile(r"(?:t|telegram)\.me/(?:\+|joinchat/)([\w-]+)", re.I)
PRIVATE_RE = re.compile(r"(?:t|telegram)\.me/c/(\d+)(?:/(\d+))?(?:/(\d+))?", re.I)
PUBLIC_RE = re.compile(r"(?:(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?|@)([A-Za-z]\w{3,})(?:/(\d+))?", re.I)
GENERAL_TOPIC_ID = 1
TITLE_FAMILIES_ALWAYS = {"bandas sonoras", "navidad"}
# Costumbre de un grupo (portada antes o despues del ZIP): hacen falta al
# menos estos casos verificados, y que esta proporcion sea del mismo lado.
COVER_HABIT_MIN = 5
COVER_HABIT_SHARE = 0.8
# Album que es solo un numero de volumen: "2", "Volumen 4", "Vol. 3".
SERIES_RE = re.compile(r"^(?:vol(?:umen|ume)?\s*)?\d+$")


class NeedJoin(Exception):
    def __init__(self, title, invite_hash, request_needed=False):
        super().__init__(title)
        self.title = title
        self.invite_hash = invite_hash
        self.request_needed = request_needed


@dataclass
class Resolution:
    topic: Topic | None
    genre: str = ""
    via: str = ""
    genres_seen: list = field(default_factory=list)
    offline: bool = False
    create: str = ""  # en /analizar: el tema que se crearia


@dataclass
class Images:
    strong: list = field(default_factory=list)      # [ruta] con vinculo seguro
    candidates: list = field(default_factory=list)  # [(mensaje, lado, ruta)]


@dataclass
class Outcome:
    status: str  # enviado | duplicado | pendiente | error
    name: str
    detail: str = ""


async def _entity_from_peer(client, peer):
    try:
        return await client.get_entity(peer)
    except ValueError:
        # El access_hash no esta en la cache de la sesion: recorrer los
        # dialogos lo carga.
        await client.get_dialogs()
        return await client.get_entity(peer)


async def resolve_chat(client, link: str):
    """Devuelve (entidad, id de mensaje desde donde empezar o None). Si es un
    link de invitacion a un grupo del que no sos miembro lanza NeedJoin."""
    link = link.strip()
    m = INVITE_RE.search(link)
    if m:
        r = await client(CheckChatInviteRequest(m[1]))
        if isinstance(r, (ChatInviteAlready, ChatInvitePeek)):
            return r.chat, None
        raise NeedJoin(r.title, m[1], bool(getattr(r, "request_needed", False)))
    m = PRIVATE_RE.search(link)
    if m:
        start = int(m[3] or m[2]) if m[2] else None
        return await _entity_from_peer(client, PeerChannel(int(m[1]))), start
    m = PUBLIC_RE.search(link)
    if m:
        return await client.get_entity(m[1]), int(m[2]) if m[2] else None
    if re.fullmatch(r"-?\d+", link):
        cid = int(link.removeprefix("-100").lstrip("-"))
        return await _entity_from_peer(client, PeerChannel(cid)), None
    raise ValueError("no reconozco el link (usá t.me/usuario, t.me/+invitación o t.me/c/123…)")


async def join_chat(client, invite_hash: str):
    updates = await client(ImportChatInviteRequest(invite_hash))
    return updates.chats[0]


async def list_topics(client, entity) -> list[tuple[int, str]]:
    topics = []
    offset_date, offset_id, offset_topic = None, 0, 0
    while True:
        result = await safe_run(client, GetForumTopicsRequest(
            peer=entity, offset_date=offset_date, offset_id=offset_id,
            offset_topic=offset_topic, limit=100))
        page = [t for t in result.topics if hasattr(t, "title")]
        topics += [(t.id, t.title) for t in page]
        if len(result.topics) < 100 or not page:
            break
        last = result.topics[-1]
        offset_topic, offset_id = last.id, last.top_message
        offset_date = next((m.date for m in result.messages if m.id == last.top_message), None)
    return topics


class Organizer:
    def __init__(self, user, store: store_mod.Store, log, notify_pending):
        self.user = user
        self.store = store
        self.log = log
        # corrutina notify_pending(pid) que avisa en el chat del bot
        self.notify_pending = notify_pending
        self.matcher = Matcher(config.GENRES_PATH)
        self.lookup = GenreLookup(store)
        self.dest = None
        self.dest_id = None
        # True mientras /rebuscar reprocesa pendientes: los que siguen sin
        # genero no mandan una tarjeta cada uno (va un resumen al final).
        self.silent = False
        # (chat_id, id de imagen) ya verificadas como portada de un disco: no
        # se ofrecen al vecino.
        self.used_covers: set = set()
        self.topics: list[Topic] = []
        self.upload_limit = (config.MAX_UPLOAD_MB or 2000) * MIB
        self.zip_max = (config.ZIP_MAX_MB or config.MAX_UPLOAD_MB or 2000) * MIB

    async def setup(self):
        me = await self.user.get_me()
        premium = bool(getattr(me, "premium", False))
        limit_mb = config.MAX_UPLOAD_MB or (4000 if premium else 2000)
        self.upload_limit = limit_mb * MIB
        self.zip_max = (config.ZIP_MAX_MB or limit_mb) * MIB
        self.log(f"Limite de subida: {limit_mb} MiB ({'Premium' if premium else 'cuenta normal'})")

        try:
            self.dest, _ = await resolve_chat(self.user, config.DEST_CHAT)
        except NeedJoin as e:
            raise RuntimeError(f"Tu cuenta no es miembro del grupo destino '{e.title}'") from e
        if not getattr(self.dest, "forum", False):
            raise RuntimeError(f"El grupo destino '{self.dest.title}' no tiene Temas activados")
        self.dest_id = utils.get_peer_id(self.dest)
        await self.refresh_topics()
        self.log(f"Destino: {self.dest.title} ({len(self.topics)} temas)")

    async def close(self):
        await self.lookup.close()

    # ---------- temas destino ----------

    async def refresh_topics(self):
        raw = await list_topics(self.user, self.dest)
        # El General no es un genero: no participa del emparejamiento.
        self.topics = self.matcher.index_topics([t for t in raw if t[0] != GENERAL_TOPIC_ID])

    def topic_by_id(self, tid) -> Topic | None:
        return next((t for t in self.topics if t.id == tid), None)

    async def create_topic(self, title: str) -> Topic:
        updates = await safe_run(self.user, CreateForumTopicRequest(
            peer=self.dest, title=title[:128], random_id=random.randint(0, 2**63 - 1)))
        tid = None
        for update in getattr(updates, "updates", []):
            msg = getattr(update, "message", None)
            if msg is not None and isinstance(getattr(msg, "action", None), MessageActionTopicCreate):
                tid = msg.id
        if tid is None:
            raise RuntimeError("Telegram no devolvio el id del tema nuevo")
        await self.refresh_topics()
        return self.topic_by_id(tid) or Topic(tid, title, norm(title))

    # ---------- decidir el tema ----------

    async def _evidence(self, info: AlbumInfo):
        """[(fuente, [generos])] de etiquetas/caption y de las busquedas, y si
        las busquedas no pudieron hacerse por falta de conexion."""
        evidence = [("etiquetas", list(info.genres))] if info.genres else []
        # El titulo de un compilado suele decir el genero ("Flash House 3", "18
        # Lentos Historicos"); el de un disco de artista solo si es una banda
        # sonora ("Music From The Motion Picture").
        title = info.album or ""
        series = bool(info.artist and SERIES_RE.match(norm(title)))
        if series:  # "16 Dance Hits From Europe - Volumen 4": el "artista" es el titulo
            title = f"{info.artist} {title}"
        hit = self.matcher.match(norm(title))
        # Una palabra amplia ("World Explosion '94") no alcanza para decidir.
        if hit and hit[0] not in self.matcher.weak:
            phrase, family, _ = hit
            exact = phrase == norm(title)  # "Lo Mejor Del Año - Rock": el album es un genero
            if not info.artist or series or exact or family in TITLE_FAMILIES_ALWAYS:
                evidence.append(("titulo", [title]))
        queries = []
        if info.artist or info.album:
            queries.append((info.artist, info.album))
        if info.artist and info.album:
            # Series de compilados ("Pump Dance - 2"): el "artista" es parte del titulo.
            queries.append(("", f"{info.artist} {info.album}"))
        offline = False
        for artist, album in queries:
            res = await self.lookup.lookup(artist, album)
            offline = offline or res.offline
            by_source, artist_level = {}, {}
            for genre, weight, source in res.candidates:
                # El primer genero de cada fuente trae su peso completo salvo que
                # haya encontrado solo al artista (ver genre_lookup.ARTIST_WEIGHT).
                if source not in artist_level:
                    artist_level[source] = weight < SOURCE_WEIGHTS.get(source, 1) * 0.9
                key = source + (ARTIST_SUFFIX if artist_level[source] else "")
                by_source.setdefault(key, []).append(genre)
            if by_source:
                return evidence + list(by_source.items()), False
        return evidence, offline

    async def resolve_topic(self, info: AlbumInfo, filenames, use_claude=True, allow_create=True) -> Resolution:
        """El tema sale del genero que devuelven las busquedas (y las
        etiquetas). Si no hay tema para ese genero se crea. Sin genero, el
        artista que ya asignaste antes; si tampoco, queda pendiente."""
        learned = self.store.learned
        artist_hit = learned["artistas"].get(norm(info.artist)) if info.artist else None
        artist_topic = artist_hit and self.topic_by_id(artist_hit["topic_id"])
        if artist_topic and artist_hit.get("manual"):
            return Resolution(artist_topic, "", f"artista asignado con /mapear ({info.artist})")

        evidence, offline = await self._evidence(info)
        seen = list(dict.fromkeys(g for _, genres in evidence for g in genres))
        mapped = {k: v["topic_id"] for k, v in learned.get("mapeo_generos", {}).items()}
        verdict = self.matcher.classify(evidence, mapped)

        if verdict is None and use_claude and config.USE_CLAUDE:
            families = {k: self.matcher.theme_name(k) for k in self.matcher.families}
            try:
                picked = await pick_family(info.artist, info.album, info.year, seen, filenames, families)
            except ClaudeUnavailable as e:
                self.log(f"  Claude no disponible: {e}")
                offline = True
            else:
                if picked:
                    verdict = self.matcher.classify([("claude", [families[picked[0]]])])
                    if verdict:
                        verdict.genre = picked[1] or verdict.genre
                        seen.append(f"{picked[1]} (Claude: {picked[2]})")

        if verdict:
            via = "votos: " + ", ".join(f"{s} {v[1]}" for s, v in verdict.votes.items())
            topic = self.matcher.topic_for(verdict, seen, self.topics)
            if topic:
                return Resolution(topic, verdict.genre, via, seen)
            if verdict.family.startswith("tema:"):  # el tema mapeado ya no existe
                return Resolution(None, verdict.genre, "", seen, offline)
            theme = self.matcher.theme_name(verdict.family)
            if not allow_create:
                return Resolution(None, verdict.genre, via, seen, create=theme)
            topic = await self.create_topic(theme)
            self.log(f"  Tema nuevo creado: {theme}")
            return Resolution(topic, verdict.genre, f"{via} · tema nuevo", seen)

        if artist_topic:
            return Resolution(artist_topic, "", f"sin género; artista elegido antes ({info.artist})", seen)
        return Resolution(None, "", "", seen, offline)

    # ---------- pendientes ----------

    async def _add_pending(self, kind, chat_id, source_title, ref, name, info, reason,
                           genres=(), key=None, topic_id=None) -> int:
        pid = self.store.add_pending({
            "tipo": kind, "chat_id": chat_id, "fuente": source_title, "ref": ref,
            "nombre": name, "artista": info.artist, "album": info.album, "anio": info.year,
            "generos": list(genres)[:12], "motivo": reason, "key": key, "topic_id": topic_id,
        })
        if self.silent:
            return pid
        try:
            await self.notify_pending(pid)
        except Exception as e:
            self.log(f"No pude avisar el pendiente #{pid}: {e!r}")
        return pid

    async def unit_from_pending(self, entry) -> tuple[object, Unit]:
        real_id, peer_cls = utils.resolve_id(entry["chat_id"])
        entity = await _entity_from_peer(self.user, peer_cls(real_id))
        ref = entry["ref"]
        candidates = ref.get("candidates", [])
        ids = ref["files"] + ref["covers"] + [i for i, _ in candidates]
        msgs = await self.user.get_messages(entity, ids=ids) if ids else []
        by_id = {m.id: m for m in msgs if m is not None}
        files = [by_id[i] for i in ref["files"] if i in by_id]
        if not files:
            raise RuntimeError("los mensajes originales ya no existen")
        unit = Unit(ref["kind"], files, [by_id[i] for i in ref["covers"] if i in by_id],
                    [(by_id[i], side) for i, side in candidates if i in by_id],
                    ref.get("hints", []), ref.get("hint_ids", []))
        return entity, unit

    async def retry_pending(self, pid: int, topic: Topic | None, reporter) -> list[Outcome]:
        """Reprocesa un pendiente; con `topic` lo manda ahi sin preguntar."""
        entry = self.store.get_pending(pid)
        if entry is None:
            raise RuntimeError(f"el pendiente #{pid} ya no existe")
        entity, unit = await self.unit_from_pending(entry)
        if topic is None and entry.get("topic_id"):
            topic = self.topic_by_id(entry["topic_id"])
        self.store.drop_pending(pid)
        return await self.process_unit(entity, entry.get("fuente") or "", unit, reporter, forced=topic)

    # ---------- procesar un album ----------

    async def process_unit(self, entity, source_title, unit: Unit, reporter, forced: Topic | None = None):
        chat_id = utils.get_peer_id(entity)
        key = unit.key()
        if forced is None and key in self.store.sent:
            return [Outcome("duplicado", unit.label(), self.store.sent[key]["tema"])]
        big = [m for m in unit.files if (m.file.size or 0) > self.upload_limit]
        if big:
            reason = f"{big[0].file.name} pesa {human(big[0].file.size)}, supera el límite de subida"
            await self._add_pending("error", chat_id, source_title, unit.to_ref(), unit.label(),
                                    unit.guess(), reason, key=key)
            return [Outcome("error", unit.label(), reason)]

        work = os.path.join(config.TMP_DIR, uuid.uuid4().hex)
        try:
            ensure_disk_space(2 * unit.size)
            n = len(unit.files)
            downloaded = []
            for i, m in enumerate(unit.files, 1):
                await reporter.set(f"⬇️ {i}/{n}: {m.file.name or 'archivo'}", force=True)
                downloaded.append(await download(self.user, m, os.path.join(work, str(i)),
                                                 reporter.callback(f"⬇️ {i}/{n}")))
            images = Images()
            for j, c in enumerate(unit.covers):
                try:
                    images.strong.append((await download(self.user, c, os.path.join(work, f"c{j}")))[1])
                except Exception as e:
                    self.log(f"  no pude bajar la caratula {c.id}: {e!r}")
            for j, (c, side) in enumerate(unit.candidates):
                if (chat_id, c.id) in self.used_covers:
                    continue  # ya se verifico como portada de otro disco
                try:
                    path = (await download(self.user, c, os.path.join(work, f"k{j}")))[1]
                    images.candidates.append((c, side, path))
                except Exception as e:
                    self.log(f"  no pude bajar la imagen {c.id}: {e!r}")
            if unit.kind == "audio":
                return await self._publish_audio(entity, source_title, unit, downloaded, images,
                                                 work, reporter, forced)
            return [await self._publish_archive(entity, source_title, unit, downloaded, images,
                                                work, reporter, forced)]
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log(f"  ERROR con {unit.label()}: {e!r}")
            await self._add_pending("error", chat_id, source_title, unit.to_ref(), unit.label(),
                                    unit.guess(), f"error: {e}", key=key,
                                    topic_id=forced.id if forced else None)
            return [Outcome("error", unit.label(), str(e))]
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _choose(self, entity, source_title, ref, name, info, filenames, key, forced):
        """Tema destino o None (y en ese caso queda pendiente)."""
        if forced:
            return forced, Resolution(forced, "", "elegido por vos")
        res = await self.resolve_topic(info, filenames)
        if res.topic:
            return res.topic, res
        reason = ("sin conexión para buscar el género" if res.offline
                  else "no encontré el género (ni en etiquetas ni en MusicBrainz/Deezer/iTunes)")
        await self._add_pending("tema", utils.get_peer_id(entity), source_title, ref, name, info,
                                reason, genres=res.genres_seen, key=key)
        return None, res

    # ---------- portada ----------

    async def _internet_cover(self, info: AlbumInfo, work, tag):
        """Portada del album exacto (Cover Art Archive, iTunes o Deezer), o None."""
        queries = [(info.artist, info.album)]
        if info.artist and info.album:
            queries.append(("", f"{info.artist} {info.album}"))
        for artist, album in queries:
            if not (artist or album):
                continue
            res = await self.lookup.lookup(artist, album)  # ya esta en la cache casi siempre
            for source, url in res.covers:
                path = await self.lookup.download_cover(url, os.path.join(work, f"net{tag}_{source}"))
                if path:
                    return path, source
        return None

    def _side_habit(self, chat_id) -> str | None:
        """"antes" o "despues" si ese grupo pone casi siempre la portada del
        mismo lado del ZIP (segun los casos ya verificados)."""
        counts = self.store.cover_sides.get(str(chat_id), {})
        total = sum(counts.values())
        if total < COVER_HABIT_MIN:
            return None
        side, n = max(counts.items(), key=lambda kv: kv[1])
        return side if n / total >= COVER_HABIT_SHARE else None

    async def _pick_covers(self, chat_id, info: AlbumInfo, strong, candidates, reference, work, tag):
        """Devuelve ([imagenes a publicar], motivo). `strong`: con vinculo
        seguro (album de Telegram o respuesta). `candidates`: [(mensaje,
        lado, ruta)] cercanas en el tiempo, que pueden ser del disco vecino.
        `reference`: la caratula que trae el disco adentro, o None."""
        # Un compilado sin artista puede coincidir con otro del mismo titulo: su
        # portada de internet sirve para comparar, pero no se publica.
        can_publish_net = bool(info.artist)
        if config.COVER_PREF == "internet" and can_publish_net:
            net = await self._internet_cover(info, work, tag)
            if net:
                return [net[0]], f"de internet ({net[1]})"

        net = None
        ref_hash = await asyncio.to_thread(image_hash, reference) if reference else None
        ref_label = "la que trae el disco adentro"
        if ref_hash is None and candidates:
            # Sin caratula adentro: la de internet sirve para comparar.
            net = await self._internet_cover(info, work, tag)
            if net:
                ref_hash = await asyncio.to_thread(image_hash, net[0])
                ref_label = f"la de internet ({net[1]})"

        chosen, why = list(strong), "del grupo, vinculada al disco" if strong else ""
        for msg, side, path in candidates:
            verdict = caption_verdict(msg.message, info)
            if verdict is False:
                continue  # su caption nombra a otro disco
            if verdict is None:
                verdict = same_image(await asyncio.to_thread(image_hash, path), ref_hash)
                reason = f"del grupo, igual a {ref_label}" if verdict else ""
            else:
                reason = "del grupo, su caption nombra al disco"
            if verdict:
                chosen.append(path)
                why = why or reason
                self.used_covers.add((chat_id, msg.id))
                sides = self.store.cover_sides.setdefault(str(chat_id), {})
                sides[side] = sides.get(side, 0) + 1
                self.store.save_cover_sides()

        if not chosen and candidates and ref_hash is None:
            # Nada con que comparar: solo si el grupo tiene una costumbre clara.
            habit = self._side_habit(chat_id)
            pick = [(m, p) for m, side, p in candidates if side == habit]
            if pick:
                chosen = [pick[0][1]]
                why = f"del grupo, publicada {habit} del ZIP como hace siempre ese grupo"
                self.used_covers.add((chat_id, pick[0][0].id))
        if chosen:
            return chosen, why
        if reference:
            return [reference], "la que trae el disco adentro"
        if can_publish_net:
            net = net or await self._internet_cover(info, work, tag)
            if net:
                return [net[0]], f"de internet ({net[1]})"
        return [], "sin portada (ninguna imagen se pudo verificar)"

    @staticmethod
    def _cover_caption(name, res: Resolution, details: str) -> str:
        lines = [f"💿 {name}"]
        if res.genre:
            lines.append(f"🏷 {res.genre}")
        lines.append(details)
        return "\n".join(lines)

    async def _publish_audio(self, entity, source_title, unit, downloaded, images, work, reporter, forced):
        await reporter.set("🏷 leyendo etiquetas…", force=True)
        tags = await asyncio.to_thread(lambda: [read_tags(p) for _, p in downloaded])
        groups = split_by_album(list(zip(downloaded, tags)))
        guess = unit.guess()
        outcomes = []
        for gi, group in enumerate(groups):
            msgs = [m for (m, _), _ in group]
            paths = [p for (_, p), _ in group]
            sub = unit if len(groups) == 1 else Unit("audio", msgs, unit.covers if gi == 0 else [],
                                                     unit.candidates)
            key = sub.key()
            info = info_from_tags([t for _, t in group])
            if gi == 0:
                info = info.merge(guess)
            elif not info.artist:
                info.artist = guess.artist
            if forced is None and key in self.store.sent:
                outcomes.append(Outcome("duplicado", info.name or sub.label(), self.store.sent[key]["tema"]))
                continue

            texts = unit.texts() if gi == 0 else [m.message for m in msgs if m.message]
            name = (clean_name(info.name) if info.name else None) or guess_album_name(paths, texts) \
                or f"album_{msgs[0].id}"
            ref = sub.to_ref() if len(groups) > 1 else unit.to_ref()
            topic, res = await self._choose(entity, source_title, ref, name, info,
                                            [p.name for p in paths], key, forced)
            if topic is None:
                outcomes.append(Outcome("pendiente", name, res.genres_seen and ", ".join(res.genres_seen[:5]) or ""))
                continue

            embedded = await asyncio.to_thread(extract_cover, paths[0], os.path.join(work, f"emb{gi}"))
            await reporter.set("🖼 eligiendo la portada…", force=True)
            group_covers, cover_note = await self._pick_covers(
                utils.get_peer_id(entity), info, images.strong if gi == 0 else [], images.candidates,
                embedded, work, gi)
            exts = sorted({p.suffix.lstrip(".").upper() for p in paths})
            details = f"🎧 {len(paths)} pista(s) · {'/'.join(exts)} · {human(sum(p.stat().st_size for p in paths))}"
            caption = self._cover_caption(name, res, details)
            await reporter.set(f"📤 publicando en «{topic.title}»…", force=True)
            sent_cover = await send_covers(self.user, self.dest, group_covers, caption, topic.id)

            await reporter.set("🗜️ armando ZIP…", force=True)
            zips = await asyncio.to_thread(build_zips, paths, work, name, self.zip_max)
            for zi, z in enumerate(zips, 1):
                head = f"🗜️ {name}" + (f" — parte {zi}/{len(zips)}" if len(zips) > 1 else "")
                zip_caption = head if sent_cover else caption + "\n" + head
                listing = "\n".join(f"• {p.name}" for p in paths)
                await send_document(self.user, self.dest, z, clip(f"{zip_caption}\n\n{listing}"),
                                    topic.id, reporter)
                z.unlink(missing_ok=True)
            self.store.mark_sent(key, name, topic.title, source_title)
            outcomes.append(Outcome("enviado", name, f"{topic.title} ({res.via}; portada: {cover_note})"))
        return outcomes

    async def _publish_archive(self, entity, source_title, unit, downloaded, images, work, reporter, forced):
        key = unit.key()
        info = unit.guess()
        first = downloaded[0][1]
        zip_cover = None
        if first.suffix.lower() == ".zip":
            await reporter.set("🔎 mirando adentro del ZIP…", force=True)
            tags, zip_cover = await asyncio.to_thread(peek_zip, first, work, config.AUDIO_EXTS)
            if tags:
                info = info.merge(info_from_tags([tags]))
        name = clean_name(info.name) if info.name else first.stem
        topic, res = await self._choose(entity, source_title, unit.to_ref(), name, info,
                                        [p.name for _, p in downloaded], key, forced)
        if topic is None:
            return Outcome("pendiente", name, ", ".join(res.genres_seen[:5]))

        await reporter.set("🖼 eligiendo la portada…", force=True)
        covers, cover_note = await self._pick_covers(utils.get_peer_id(entity), info, images.strong,
                                                     images.candidates, zip_cover, work, "z")
        details = f"📦 {len(downloaded)} archivo(s) · {human(sum(p.stat().st_size for _, p in downloaded))}"
        caption = self._cover_caption(name, res, details)
        await reporter.set(f"📤 publicando en «{topic.title}»…", force=True)
        sent_cover = await send_covers(self.user, self.dest, covers, caption, topic.id)
        for msg, path in downloaded:
            text = (msg.message or "").strip() if sent_cover else caption + "\n\n" + (msg.message or "")
            await send_document(self.user, self.dest, path, text.strip() or name, topic.id, reporter)
        self.store.mark_sent(key, name, topic.title, source_title)
        return Outcome("enviado", name, f"{topic.title} ({res.via}; portada: {cover_note})")

    # ---------- una fuente completa ----------

    async def run_source(self, entity, link, reporter, analyze=False, start_id=None) -> str:
        chat_id = utils.get_peer_id(entity)
        if chat_id == self.dest_id:
            raise RuntimeError("ese es el grupo destino")
        title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(chat_id)
        st = store_mod.load_source(chat_id)
        st["title"], st["link"] = title, st.get("link") or link
        min_id = st["checkpoint"] or (start_id - 1 if start_id else 0)

        await reporter.set("🔎 leyendo mensajes…", force=True)
        units, last_id = await scan(self.user, entity, min_id, bool(getattr(entity, "forum", False)))
        done = set(st["done_ids"])
        units = [u for u in units if u.first_id not in done]
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=config.MIN_AGE_MINUTES)
        ready = [u for u in units if u.last_date <= cutoff]
        deferred = [u for u in units if u.last_date > cutoff]

        if analyze:
            return await self._analyze(title, ready, deferred, reporter)

        counts = {"enviados": 0, "duplicados": 0, "pendientes": 0, "errores": 0}
        status_key = {"enviado": "enviados", "duplicado": "duplicados",
                      "pendiente": "pendientes", "error": "errores"}
        processed = set()
        try:
            for i, unit in enumerate(ready, 1):
                reporter.header = f"📥 {title} — {i}/{len(ready)}: {unit.label()}"
                for out in await self.process_unit(entity, title, unit, reporter):
                    counts[status_key[out.status]] += 1
                    st["stats"][status_key[out.status]] = st["stats"].get(status_key[out.status], 0) + 1
                processed.add(unit.first_id)
                st["done_ids"].append(unit.first_id)
                store_mod.save_source(st)
        finally:
            # El checkpoint avanza hasta justo antes del primer album que quedo
            # sin procesar (diferido por reciente o cortado por /cancelar).
            left = [u for u in ready if u.first_id not in processed] + deferred
            checkpoint = min(u.min_id for u in left) - 1 if left else last_id
            st["checkpoint"] = max(st["checkpoint"], checkpoint)
            st["done_ids"] = sorted(i for i in set(st["done_ids"]) if i > st["checkpoint"])
            st["last_run"] = store_mod.now_iso()
            store_mod.save_source(st)

        lines = [f"🏁 {title}: {summarize(ready)}",
                 f"✅ Enviados: {counts['enviados']}   ♻️ Duplicados: {counts['duplicados']}",
                 f"⏳ Pendientes: {counts['pendientes']}   ❌ Errores: {counts['errores']}"]
        if deferred:
            lines.append(f"⏸ {len(deferred)} álbum(es) muy recientes quedan para la próxima "
                         f"(menos de {config.MIN_AGE_MINUTES} min)")
        return "\n".join(lines)

    async def _analyze(self, title, ready, deferred, reporter) -> str:
        lines = [f"🔍 {title}: {summarize(ready)} (sin enviar nada)", ""]
        for i, unit in enumerate(ready, 1):
            await reporter.set(f"🔍 analizando {i}/{len(ready)}…")
            label = unit.label()
            if unit.key() in self.store.sent:
                lines.append(f"♻️ {label} (ya enviado)")
                continue
            info = unit.guess()
            # Sin descargar no hay etiquetas, y sin Claude para no gastar: los
            # que aca quedan con ❓ pueden resolverse igual al organizar.
            res = await self.resolve_topic(info, [m.file.name or "" for m in unit.files],
                                           use_claude=False, allow_create=False)
            icon = "📦" if unit.kind == "archivo" else "🎧"
            cover = " 🖼" if unit.covers else ""
            if res.topic:
                lines.append(f"{icon}{cover} {label} → {res.topic.title} ({res.via})")
            elif res.create:
                lines.append(f"{icon}{cover} {label} → 🆕 {res.create} ({res.via})")
            else:
                lines.append(f"{icon}{cover} {label} → ❓")
        if deferred:
            lines.append(f"\n⏸ {len(deferred)} álbum(es) muy recientes no se cuentan todavía")
        return "\n".join(lines)
