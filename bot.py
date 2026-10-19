import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable
from urllib.parse import quote_plus

from telethon import Button, TelegramClient, events, utils
from telethon.errors import MessageNotModifiedError
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

import config
import store as store_mod
from media import Reporter
from music_meta import AlbumInfo, parse_name, parse_title
from organizer import NeedJoin, Organizer, _entity_from_peer, join_chat, resolve_chat
from topic_match import norm

LOG_PATH = os.path.join(config.LOG_DIR, "bot.log")
LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/\S+|@[A-Za-z]\w{3,}", re.I)
MAX_MESSAGE = 4000

# Se muestran en el boton "Menu" de Telegram junto al campo de texto.
BOT_COMMANDS = [
    BotCommand("menu", "Menú principal con botones"),
    BotCommand("organizar", "Organizar un grupo: /organizar <link>"),
    BotCommand("analizar", "Ver qué se enviaría, sin enviar: /analizar <link>"),
    BotCommand("actualizar", "Revisar lo nuevo de los grupos ya organizados"),
    BotCommand("pendientes", "Álbumes esperando que elijas el tema"),
    BotCommand("rebuscar", "Volver a buscar el género de todos los pendientes"),
    BotCommand("estado", "Tarea en curso y cola"),
    BotCommand("cancelar", "Cancelar la tarea en curso"),
    BotCommand("fuentes", "Grupos organizados y sus números"),
    BotCommand("temas", "Temas de WAV/FLAC, su género y los que faltan crear"),
    BotCommand("aprendido", "Artistas y géneros asignados"),
    BotCommand("mapear", "Asignar a mano: /mapear artista|genero <nombre> = <tema>"),
    BotCommand("olvidar", "Borrar lo aprendido: /olvidar artista|genero <nombre>"),
    BotCommand("logs", "Últimas líneas del log: /logs [n]"),
    BotCommand("ayuda", "Cómo funciona y lista de comandos"),
]

HELP = (
    "🎼 Organizador de música para WAV/FLAC\n\n"
    "Mandame el link de un grupo o supergrupo (t.me/usuario, t.me/+invitación o t.me/c/…; "
    "si es un link a un mensaje, empiezo desde ahí). Recorro sus mensajes, junto los audios "
    "sueltos de cada álbum en un ZIP (los comprimidos van tal cual) y los publico con su "
    "carátula en el tema de WAV/FLAC que corresponde a su género.\n\n"
    "El tema es el género que devuelven MusicBrainz, Deezer, iTunes y las etiquetas (cada "
    "fuente vota; si no encuentran nada, Claude con búsqueda web, si está configurado). Si "
    "todavía no hay tema para ese género, lo creo. Si no encuentro el género, el álbum queda "
    "en /pendientes para que elijas, y recuerdo el artista para cuando tampoco haya género.\n\n"
    "Comandos:\n" + "\n".join(f"/{c.command} — {c.description}" for c in BOT_COMMANDS)
)


def _log(line: str) -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")
    print(line)


def _chunks(text: str, size=MAX_MESSAGE):
    lines, current = text.splitlines(), ""
    for line in lines:
        if current and len(current) + len(line) + 1 > size:
            yield current
            current = ""
        current += line[:size] + "\n"
    if current.strip():
        yield current


@dataclass
class Job:
    label: str
    run: Callable[[Reporter], Awaitable[str]]


class OrganizerBot:
    def __init__(self, bot: TelegramClient, user: TelegramClient):
        self.bot = bot
        self.user = user
        self.store = store_mod.Store()
        self.org = Organizer(user, self.store, _log, self.send_pending_card)
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.waiting: list[Job] = []
        self.current: tuple[Job, asyncio.Task] | None = None
        # Los links van en el callback_data como un numero (limite de 64 bytes).
        self.links: dict[int, str] = {}
        self.join_hashes: dict[int, tuple[str, str, bool]] = {}
        self.next_token = 1

    def _token(self, table, value) -> int:
        tok = self.next_token
        self.next_token += 1
        table[tok] = value
        return tok

    async def say(self, text, **kwargs):
        return await self.bot.send_message(config.OWNER_ID, text, **kwargs)

    # ---------- cola de tareas ----------

    async def enqueue(self, job: Job, event=None):
        self.waiting.append(job)
        await self.queue.put(job)
        busy = self.current is not None or len(self.waiting) > 1
        text = f"🕐 En cola: {job.label}" if busy else f"▶️ Arrancando: {job.label}"
        if event is not None:
            await event.respond(text)

    async def worker(self):
        while True:
            job = await self.queue.get()
            if job not in self.waiting:
                continue  # cancelada mientras esperaba
            self.waiting.remove(job)
            status = await self.say(f"▶️ {job.label}")
            reporter = Reporter(status, f"▶️ {job.label}",
                                buttons=[Button.inline("⛔ Cancelar", b"m:cancel")])
            task = asyncio.create_task(job.run(reporter))
            self.current = (job, task)
            _log(f"INICIO {job.label}")
            await asyncio.wait([task])
            self.current = None
            try:
                summary = task.result()
                _log(f"OK {job.label}")
                try:
                    await status.edit(f"✔️ {job.label}", buttons=None)
                except MessageNotModifiedError:
                    pass
                for chunk in _chunks(summary or "Listo."):
                    await self.say(chunk)
            except asyncio.CancelledError:
                _log(f"CANCELADO {job.label}")
                await status.edit(f"⛔ Cancelado: {job.label}\nLo ya enviado queda; la próxima vez sigo desde ahí.",
                                  buttons=None)
            except NeedJoin as e:
                if e.request_needed:
                    await status.edit(f"🔒 «{e.title}» requiere aprobación de un admin para entrar. "
                                      f"Pedí unirte desde Telegram y volvé a mandar el link.", buttons=None)
                else:
                    tok = self._token(self.join_hashes, (e.invite_hash, e.title, "analizar" in job.label.lower()))
                    await status.edit(f"🚪 Tu cuenta no es miembro de «{e.title}». ¿Me uno?",
                                      buttons=[[Button.inline("✅ Unirme y seguir", f"jn:{tok}".encode())]])
            except Exception as e:
                _log(f"ERROR {job.label}: {e!r}")
                await status.edit(f"❌ {job.label}\n{e}", buttons=None)

    def source_job(self, link: str, analyze=False) -> Job:
        verb = "Analizar" if analyze else "Organizar"

        async def run(reporter):
            entity, start = await resolve_chat(self.user, link)
            title = getattr(entity, "title", None) or link
            reporter.header = f"{'🔍' if analyze else '📥'} {title}"
            return await self.org.run_source(entity, link, reporter, analyze=analyze, start_id=start)

        return Job(f"{verb} {link}", run)

    def join_job(self, invite_hash, title, analyze) -> Job:
        async def run(reporter):
            entity = await join_chat(self.user, invite_hash)
            reporter.header = f"📥 {title}"
            return await self.org.run_source(entity, f"https://t.me/+{invite_hash}", reporter, analyze=analyze)

        return Job(f"{'Analizar' if analyze else 'Organizar'} {title}", run)

    def update_job(self, st: dict) -> Job:
        async def run(reporter):
            real_id, peer_cls = utils.resolve_id(st["chat_id"])
            entity = await _entity_from_peer(self.user, peer_cls(real_id))
            reporter.header = f"🔄 {st['title']}"
            return await self.org.run_source(entity, st.get("link"), reporter)

        return Job(f"Actualizar {st['title']}", run)

    def pending_job(self, pid: int, topic) -> Job:
        entry = self.store.get_pending(pid) or {}

        async def run(reporter):
            reporter.header = f"⏳ #{pid} {entry.get('nombre', '')}"
            outcomes = await self.org.retry_pending(pid, topic, reporter)
            icons = {"enviado": "✅", "duplicado": "♻️", "pendiente": "⏳", "error": "❌"}
            return "\n".join(f"{icons[o.status]} {o.name} {('— ' + o.detail) if o.detail else ''}"
                             for o in outcomes)

        dest = f" → {topic.title}" if topic else ""
        return Job(f"Pendiente #{pid} {entry.get('nombre', '')}{dest}", run)

    def research_job(self, pids: list[int]) -> Job:
        """Vuelve a buscar el genero de los pendientes con lo que ya se sabe de
        cada uno (sin descargar). Solo baja y publica los que encuentra."""
        async def run(reporter):
            resolved, still, failed = [], 0, []
            for i, pid in enumerate(pids, 1):
                entry = self.store.get_pending(pid)
                if entry is None:
                    continue
                await reporter.set(f"🔎 {i}/{len(pids)}: {entry['nombre']}")
                info = parse_name(entry["nombre"]) or parse_title(entry["nombre"]) \
                    or AlbumInfo(entry.get("artista"), entry.get("album"), entry.get("anio"))
                info.genres = list(entry.get("generos") or [])
                res = await self.org.resolve_topic(info, [])
                if res.topic is None:
                    still += 1
                    continue
                reporter.header = f"🔁 #{pid} {entry['nombre']} → {res.topic.title}"
                try:
                    self.org.silent = True
                    outcomes = await self.org.retry_pending(pid, res.topic, reporter)
                except Exception as e:
                    failed.append(f"#{pid} {entry['nombre']}: {e}")
                    continue
                finally:
                    self.org.silent = False
                if any(o.status == "enviado" for o in outcomes):
                    resolved.append(f"✅ {entry['nombre']} → {res.topic.title} ({res.genre})")
                else:
                    failed.append(f"#{pid} {entry['nombre']}: " + ", ".join(o.detail for o in outcomes))
            lines = [f"🔁 Pendientes revisados: {len(pids)}",
                     f"✅ Resueltos: {len(resolved)}   ⏳ Siguen sin género: {still}   ❌ Fallaron: {len(failed)}"]
            lines += resolved + [f"❌ {f}" for f in failed]
            if still:
                lines.append("\nLos que siguen están en /pendientes.")
            return "\n".join(lines)

        return Job(f"Volver a buscar {len(pids)} pendiente(s)", run)

    # ---------- menus ----------

    def main_menu(self):
        n_pend = len(self.store.pending["items"])
        return [
            [Button.inline("📥 Organizar grupo", b"m:org"), Button.inline("🔄 Actualizar", b"m:upd")],
            [Button.inline(f"⏳ Pendientes ({n_pend})", b"m:pend"), Button.inline("📊 Estado", b"m:est")],
            [Button.inline("📚 Fuentes", b"m:src"), Button.inline("🗂 Temas WAV/FLAC", b"m:temas")],
            [Button.inline("🧠 Aprendido", b"m:apr"), Button.inline("📜 Logs", b"m:logs")],
            [Button.inline("⛔ Cancelar tarea", b"m:cancel"), Button.inline("❓ Ayuda", b"m:help")],
        ]

    def menu_text(self):
        cur = f"Ahora: {self.current[0].label}" if self.current else "Sin tareas en curso."
        extra = f" ({len(self.waiting)} en cola)" if self.waiting else ""
        return f"🎼 Organizador WAV/FLAC\n{cur}{extra}\n\nMandame un link o elegí una opción:"

    def update_menu(self):
        sources = store_mod.known_sources()
        rows = [[Button.inline("🔄 Todas", b"up:all")]] if sources else []
        rows += [[Button.inline(st["title"] or str(st["chat_id"]), f"up:{i}".encode())]
                 for i, st in enumerate(sources)]
        rows.append([Button.inline("« Menú", b"m")])
        return rows

    def status_text(self):
        lines = []
        if self.current:
            lines.append(f"▶️ {self.current[0].label}")
        lines += [f"🕐 {job.label}" for job in self.waiting]
        return "\n".join(lines) or "No hay tareas en curso ni en cola."

    def sources_text(self):
        sources = store_mod.known_sources()
        if not sources:
            return "Todavía no organicé ningún grupo."
        lines = []
        for st in sources:
            s = st["stats"]
            lines.append(f"📚 {st['title']}\n   ✅ {s.get('enviados', 0)}  ♻️ {s.get('duplicados', 0)}  "
                         f"⏳ {s.get('pendientes', 0)}  ❌ {s.get('errores', 0)} · "
                         f"último: {st.get('last_run') or '—'}")
        return "\n".join(lines)

    def topics_text(self):
        if not self.org.topics:
            return "El grupo destino no tiene temas (aparte de General)."
        m = self.org.matcher
        lines = [f"🗂 Temas de {self.org.dest.title}:"]
        for t in self.org.topics:
            if t.family:
                kind = "" if t.generic else f" (subgénero «{t.phrase}»)"
                lines.append(f"• {t.title} → {m.theme_name(t.family)}{kind}")
            else:
                lines.append(f"• {t.title} → ⚠️ no es un género: solo recibe lo que elijas a mano")
        used = {t.family for t in self.org.topics if t.family}
        missing = [m.theme_name(f) for f in m.families if f not in used]
        if missing:
            lines.append("\n🆕 Se crean cuando llegue un álbum de ese género: " + ", ".join(missing))
        lines.append("\nLos géneros y nombres están en generos.json.")
        return "\n".join(lines)

    def learned_text(self):
        lrn = self.store.learned
        if not lrn["artistas"] and not lrn["mapeo_generos"]:
            return ("Todavía no hay nada. Cuando elegís el tema de un pendiente recuerdo el artista; "
                    "con /mapear asignás artistas o géneros a mano.")
        lines = []
        manual = [v for v in lrn["artistas"].values() if v.get("manual")]
        chosen = [v for v in lrn["artistas"].values() if not v.get("manual")]
        if manual:
            lines.append("👤 Artistas asignados con /mapear (ganan sobre el género):")
            lines += [f"• {v['nombre']} → {v['tema']}" for v in manual]
        if chosen:
            lines.append("👤 Artistas elegidos en pendientes (solo si no encuentro el género):")
            lines += [f"• {v['nombre']} → {v['tema']}" for v in chosen]
        if lrn["mapeo_generos"]:
            lines.append("🏷 Géneros asignados con /mapear:")
            lines += [f"• {v['nombre']} → {v['tema']}" for v in lrn["mapeo_generos"].values()]
        lines.append("\nPara corregir: /olvidar artista|genero <nombre> o /mapear …")
        return "\n".join(lines)

    # ---------- pendientes ----------

    def pending_text(self, e: dict) -> str:
        lines = [f"⏳ Pendiente #{e['id']}", f"💿 {e['nombre']}", f"📍 De: {e.get('fuente') or '—'}"]
        if e.get("generos"):
            lines.append("🏷 Géneros encontrados: " + ", ".join(e["generos"][:8]))
        lines.append(f"❔ {e['motivo']}")
        if e["tipo"] == "tema":
            lines.append("\n¿A qué tema lo mando?")
        return "\n".join(lines)

    @staticmethod
    def google_button(e: dict):
        query = " ".join(x for x in (e.get("artista"), e.get("album")) if x) \
            or re.sub(r"\.(zip|rar|7z)$", "", e["nombre"], flags=re.I)
        return Button.url("🌐 Buscar en Google", "https://www.google.com/search?q=" + quote_plus(f"{query} álbum"))

    def pending_buttons(self, e: dict, page=0):
        pid = e["id"]
        if e["tipo"] != "tema":
            return [[Button.inline("🔁 Reintentar", f"pr:{pid}".encode()),
                     Button.inline("🗑 Descartar", f"pd:{pid}".encode())],
                    [self.google_button(e)]]
        topics = self.org.topics
        per = config.TOPICS_PER_PAGE
        pages = max(1, -(-len(topics) // per))
        page = max(0, min(page, pages - 1))
        chunk = topics[page * per:(page + 1) * per]
        rows = [[Button.inline(t.title[:40], f"pt:{pid}:{t.id}".encode()) for t in chunk[i:i + 2]]
                for i in range(0, len(chunk), 2)]
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(Button.inline("◀️", f"pp:{pid}:{page - 1}".encode()))
            nav.append(Button.inline(f"{page + 1}/{pages}", b"noop"))
            if page < pages - 1:
                nav.append(Button.inline("▶️", f"pp:{pid}:{page + 1}".encode()))
            rows.append(nav)
        for i, g in enumerate(e.get("generos", [])[:2]):
            rows.append([Button.inline(f"➕ Crear tema «{g.title()[:30]}»", f"pc:{pid}:{i}".encode())])
        rows.append([Button.inline("🔍 Buscar de nuevo", f"pr:{pid}".encode()),
                     Button.inline("🗑 Descartar", f"pd:{pid}".encode())])
        rows.append([self.google_button(e)])
        return rows

    async def send_pending_card(self, pid: int):
        e = self.store.get_pending(pid)
        if e:
            await self.say(self.pending_text(e), buttons=self.pending_buttons(e))

    async def show_pending(self, event):
        items = list(self.store.pending["items"].values())
        if not items:
            await event.respond("✨ No hay pendientes.")
            return
        await event.respond(f"⏳ {len(items)} pendiente(s):", buttons=[[
            Button.inline("🔁 Volver a buscar el género de todos", b"m:rebuscar")]])
        for e in items[:15]:
            await event.respond(self.pending_text(e), buttons=self.pending_buttons(e))
        if len(items) > 15:
            await event.respond(f"…y {len(items) - 15} más. Resolvé estos y volvé a abrir /pendientes.")

    def learn_from(self, entry: dict, topic) -> list[str]:
        """Solo el artista, y solo para cuando no aparezca el genero: aprender
        generos de un pendiente mandaba "rock" o "reggae" a temas equivocados."""
        artist = entry.get("artista")
        known = self.store.learned["artistas"].get(norm(artist or ""))
        if not artist or norm(artist) in ("varios artistas", "various artists", "va") \
                or (known and known.get("manual")):
            return []
        self.store.learn("artistas", norm(artist), artist, topic.id, topic.title)
        return [f"artista {artist}"]

    async def choose_topic(self, event, pid: int, topic):
        entry = self.store.get_pending(pid)
        if entry is None:
            await event.edit("Ese pendiente ya no existe.", buttons=None)
            return
        learned = self.learn_from(entry, topic)
        note = ("\n🧠 Aprendí: " + ", ".join(learned) + f" → {topic.title}") if learned else ""
        await event.edit(f"{self.pending_text(entry)}\n\n✅ → {topic.title} (en cola){note}", buttons=None)
        await self.enqueue(self.pending_job(pid, topic))

    # ---------- comandos ----------

    def find_topic(self, name: str):
        n = norm(name)
        exact = [t for t in self.org.topics if t.norm == n]
        if exact:
            return exact[0]
        partial = [t for t in self.org.topics if n and n in t.norm]
        return partial[0] if len(partial) == 1 else None

    async def on_menu(self, event):
        await event.respond(self.menu_text(), buttons=self.main_menu())

    async def on_help(self, event):
        await event.respond(HELP)

    async def on_source_cmd(self, event):
        analyze = event.pattern_match.group(1).lower() == "analizar"
        link = (event.pattern_match.group(2) or "").strip()
        if not link:
            await event.respond(f"Mandame el link: /{'analizar' if analyze else 'organizar'} <link>")
            return
        await self.enqueue(self.source_job(link, analyze), event)

    async def on_update(self, event):
        if not store_mod.known_sources():
            await event.respond("Todavía no organicé ningún grupo. Mandame un link primero.")
            return
        await event.respond("¿Qué grupo reviso?", buttons=self.update_menu())

    async def on_pending(self, event):
        await self.show_pending(event)

    async def on_status(self, event):
        await event.respond(self.status_text())

    async def on_cancel(self, event):
        if not self.current:
            await event.respond("No hay ninguna tarea en curso.")
            return
        await event.respond(f"⛔ Cancelando: {self.current[0].label}…")
        self.current[1].cancel()

    async def on_sources(self, event):
        for chunk in _chunks(self.sources_text()):
            await event.respond(chunk)

    async def on_topics(self, event):
        await self.org.refresh_topics()
        for chunk in _chunks(self.topics_text()):
            await event.respond(chunk)

    async def on_learned(self, event):
        for chunk in _chunks(self.learned_text()):
            await event.respond(chunk)

    async def on_map(self, event):
        m = re.match(r"/mapear\s+(artista|g[eé]nero)\s+(.+?)\s*=\s*(.+)$", event.raw_text.strip(), re.I)
        if not m:
            await event.respond("Uso: /mapear artista Soda Stereo = Rock en Español\n"
                                "     /mapear genero synthwave = Dance - Electronica")
            return
        kind = "artistas" if m[1].lower() == "artista" else "mapeo_generos"
        topic = self.find_topic(m[3])
        if topic is None:
            await event.respond(f"No encontré un único tema que se llame «{m[3]}». Mirá /temas.")
            return
        self.store.learn(kind, norm(m[2]), m[2].strip(), topic.id, topic.title, manual=True)
        await event.respond(f"🧠 Listo: {m[1].lower()} {m[2].strip()} → {topic.title}")

    async def on_forget(self, event):
        m = re.match(r"/olvidar\s+(artista|g[eé]nero)\s+(.+)$", event.raw_text.strip(), re.I)
        if not m:
            await event.respond("Uso: /olvidar artista <nombre> o /olvidar genero <nombre>")
            return
        kind = "artistas" if m[1].lower() == "artista" else "mapeo_generos"
        ok = self.store.forget(kind, norm(m[2]))
        await event.respond("🗑 Olvidado." if ok else "No tenía eso aprendido.")

    async def on_research(self, event):
        pids = [e["id"] for e in self.store.pending["items"].values() if e["tipo"] == "tema"]
        if not pids:
            await event.respond("No hay pendientes por tema.")
            return
        await self.enqueue(self.research_job(pids), event)

    async def on_logs(self, event):
        n = int(event.pattern_match.group(1) or 30)
        await self.respond_logs(event, n)

    async def respond_logs(self, event, n=30):
        if not os.path.exists(LOG_PATH):
            await event.respond("Todavía no hay logs.")
            return
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            text = "".join(f.readlines()[-n:])[-3500:] or "(vacío)"
        await event.respond(f"```\n{text}\n```")

    async def on_message(self, event):
        text = event.raw_text or ""
        if text.startswith("/"):
            return
        m = LINK_RE.search(text)
        if not m:
            await event.respond("Mandame el link de un grupo para organizarlo, o /menu.")
            return
        tok = self._token(self.links, m.group(0))
        await event.respond(f"🔗 {m.group(0)}", buttons=[[
            Button.inline("▶️ Organizar", f"go:{tok}".encode()),
            Button.inline("🔍 Solo analizar", f"an:{tok}".encode())]])

    async def on_callback(self, event):
        data = event.data.decode()
        await event.answer()
        head, _, rest = data.partition(":")

        if data == "m":
            await event.edit(self.menu_text(), buttons=self.main_menu())
        elif data == "noop":
            pass
        elif head == "m":
            actions = {
                "org": lambda: event.respond("📥 Mandame el link del grupo o supergrupo (t.me/…)."),
                "upd": lambda: self.on_update(event),
                "pend": lambda: self.show_pending(event),
                "est": lambda: event.respond(self.status_text()),
                "src": lambda: self.on_sources(event),
                "temas": lambda: self.on_topics(event),
                "apr": lambda: self.on_learned(event),
                "logs": lambda: self.respond_logs(event),
                "cancel": lambda: self.on_cancel(event),
                "rebuscar": lambda: self.on_research(event),
                "help": lambda: event.respond(HELP),
            }
            if rest in actions:
                await actions[rest]()
        elif head in ("go", "an"):
            link = self.links.get(int(rest))
            if link is None:
                await event.respond("Ese botón venció, mandame el link de nuevo.")
                return
            await event.edit(f"🔗 {link}", buttons=None)
            await self.enqueue(self.source_job(link, analyze=head == "an"), event)
        elif head == "jn":
            info = self.join_hashes.pop(int(rest), None)
            if info is None:
                await event.respond("Ese botón venció, mandame el link de nuevo.")
                return
            await event.edit(f"🚪 Uniéndome a «{info[1]}»…", buttons=None)
            await self.enqueue(self.join_job(*info), event)
        elif head == "up":
            sources = store_mod.known_sources()
            chosen = sources if rest == "all" else [sources[int(rest)]] if rest.isdigit() and int(rest) < len(sources) else []
            if not chosen:
                await event.respond("No encontré esa fuente, abrí /actualizar de nuevo.")
                return
            await event.edit("🔄 Revisando: " + ", ".join(st["title"] for st in chosen), buttons=None)
            for st in chosen:
                await self.enqueue(self.update_job(st), event)
        elif head == "pt":
            pid, tid = (int(x) for x in rest.split(":"))
            topic = self.org.topic_by_id(tid)
            if topic is None:
                await event.respond("Ese tema ya no existe; abrí /pendientes de nuevo.")
                return
            await self.choose_topic(event, pid, topic)
        elif head == "pp":
            pid, page = (int(x) for x in rest.split(":"))
            entry = self.store.get_pending(pid)
            if entry:
                await event.edit(self.pending_text(entry), buttons=self.pending_buttons(entry, page))
        elif head == "pc":
            pid, idx = (int(x) for x in rest.split(":"))
            entry = self.store.get_pending(pid)
            if entry is None or idx >= len(entry.get("generos", [])):
                await event.respond("Ese pendiente ya no existe.")
                return
            title = entry["generos"][idx].title()
            existing = self.find_topic(title)
            topic = existing if existing and existing.norm == norm(title) else await self.org.create_topic(title)
            await self.choose_topic(event, pid, topic)
        elif head == "pr":
            pid = int(rest)
            entry = self.store.get_pending(pid)
            if entry is None:
                await event.edit("Ese pendiente ya no existe.", buttons=None)
                return
            await event.edit(f"{self.pending_text(entry)}\n\n🔁 Reintentando (en cola)…", buttons=None)
            await self.enqueue(self.pending_job(pid, None))
        elif head == "pd":
            pid = int(rest)
            entry = self.store.get_pending(pid)
            self.store.drop_pending(pid)
            await event.edit(f"🗑 Descartado: {entry['nombre'] if entry else '#' + rest}", buttons=None)

    def register(self):
        owner = dict(from_users=config.OWNER_ID)
        handlers = [
            (self.on_menu, r"^/(?:start|menu)\b"),
            (self.on_help, r"^/(?:ayuda|help)\b"),
            (self.on_source_cmd, r"^/(organizar|analizar)\b\s*(.*)"),
            (self.on_update, r"^/actualizar\b"),
            (self.on_pending, r"^/pendientes\b"),
            (self.on_research, r"^/rebuscar\b"),
            (self.on_status, r"^/estado\b"),
            (self.on_cancel, r"^/cancelar\b"),
            (self.on_sources, r"^/fuentes\b"),
            (self.on_topics, r"^/temas\b"),
            (self.on_learned, r"^/aprendido\b"),
            (self.on_map, r"^/mapear\b"),
            (self.on_forget, r"^/olvidar\b"),
            (self.on_logs, r"^/logs\b\s*(\d+)?"),
        ]
        for handler, pattern in handlers:
            self.bot.add_event_handler(handler, events.NewMessage(pattern=pattern, **owner))
        self.bot.add_event_handler(self.on_message, events.NewMessage(**owner))
        self.bot.add_event_handler(
            self.on_callback, events.CallbackQuery(func=lambda e: e.sender_id == config.OWNER_ID))


async def main() -> None:
    if not config.BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN debe estar definido en .env")
    if config.OWNER_ID is None:
        raise RuntimeError("TG_OWNER_ID debe estar definido en .env para restringir el bot a tu cuenta")
    if not os.path.exists(config.USER_SESSION_NAME + ".session"):
        raise RuntimeError("Falta la sesion de usuario: corre primero `python login.py`")

    os.makedirs(config.TMP_DIR, exist_ok=True)
    user = TelegramClient(config.USER_SESSION_NAME, config.API_ID, config.API_HASH)
    bot = TelegramClient(config.BOT_SESSION_NAME, config.API_ID, config.API_HASH)

    # Sin "async with": su start() sin argumentos pediria telefono por consola.
    await user.connect()
    if not await user.is_user_authorized():
        raise RuntimeError("La sesion de usuario no esta autorizada: corre `python login.py`")
    await bot.start(bot_token=config.BOT_TOKEN)
    me = await bot.get_me()
    if not me.bot or str(me.id) != config.BOT_TOKEN.split(":", 1)[0]:
        raise RuntimeError(f"La sesion '{config.BOT_SESSION_NAME}.session' no es la del bot de "
                           f"TG_BOT_TOKEN: borrala y volve a arrancar.")

    try:
        await bot(SetBotCommandsRequest(scope=BotCommandScopeDefault(), lang_code="", commands=BOT_COMMANDS))
    except Exception as e:
        _log(f"No pude registrar el menu de comandos: {e!r}")

    app = OrganizerBot(bot, user)
    try:
        await app.org.setup()
        app.register()
        worker = asyncio.create_task(app.worker())
        _log(f"Bot iniciado (Claude: {'si' if config.USE_CLAUDE else 'no'})")
        await app.say("🟢 Organizador listo.", buttons=app.main_menu())
        await bot.run_until_disconnected()
        worker.cancel()
    finally:
        await app.org.close()
        await user.disconnect()
        await bot.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
