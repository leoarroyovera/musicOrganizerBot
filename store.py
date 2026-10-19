"""Estado persistente en JSON: checkpoint por fuente, pendientes, albumes ya
enviados, lo aprendido de tus respuestas y la cache de busquedas online."""
import json
import os
from datetime import datetime

import config


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def save_json(path, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- fuentes ----------


def source_path(chat_id: int) -> str:
    return os.path.join(config.SOURCES_DIR, f"{chat_id}.json")


def load_source(chat_id: int) -> dict:
    st = load_json(source_path(chat_id), None)
    if st is None:
        st = {"chat_id": chat_id, "title": None, "link": None,
              # Todo mensaje con id <= checkpoint ya se proceso. Los albumes
              # procesados por encima (porque uno anterior quedo diferido) se
              # anotan por el id de su primer mensaje en done_ids.
              "checkpoint": 0, "done_ids": [], "last_run": None,
              "stats": {"enviados": 0, "duplicados": 0, "pendientes": 0, "errores": 0}}
    return st


def save_source(st: dict) -> None:
    save_json(source_path(st["chat_id"]), st)


def known_sources() -> list[dict]:
    out = []
    if os.path.isdir(config.SOURCES_DIR):
        for name in sorted(os.listdir(config.SOURCES_DIR)):
            if name.endswith(".json"):
                st = load_json(os.path.join(config.SOURCES_DIR, name), None)
                if st:
                    out.append(st)
    return out


# ---------- pendientes / enviados / aprendido ----------


class Store:
    def __init__(self):
        self.pending = load_json(config.PENDING_PATH, {"next_id": 1, "items": {}})
        self.sent = load_json(config.SENT_PATH, {})
        self.learned = load_json(config.LEARNED_PATH, {"artistas": {}})
        self.learned.setdefault("artistas", {})
        # genero -> tema, solo los que cargaste con /mapear genero. ("generos",
        # de versiones anteriores, se aprendia solo de los pendientes y metia
        # generos en temas equivocados: ya no se usa.)
        self.learned.setdefault("mapeo_generos", {})
        self.lookup_cache = load_json(config.LOOKUP_CACHE_PATH, {})

    def save_pending(self):
        save_json(config.PENDING_PATH, self.pending)

    def save_sent(self):
        save_json(config.SENT_PATH, self.sent)

    def save_learned(self):
        save_json(config.LEARNED_PATH, self.learned)

    def save_lookup_cache(self):
        save_json(config.LOOKUP_CACHE_PATH, self.lookup_cache)

    def add_pending(self, entry: dict) -> int:
        pid = self.pending["next_id"]
        self.pending["next_id"] += 1
        entry["id"] = pid
        entry["fecha"] = now_iso()
        self.pending["items"][str(pid)] = entry
        self.save_pending()
        return pid

    def get_pending(self, pid: int) -> dict | None:
        return self.pending["items"].get(str(pid))

    def drop_pending(self, pid: int) -> None:
        self.pending["items"].pop(str(pid), None)
        self.save_pending()

    def mark_sent(self, key: str, name: str, topic: str, source: str) -> None:
        self.sent[key] = {"nombre": name, "tema": topic, "fuente": source, "fecha": now_iso()}
        self.save_sent()

    def learn(self, kind: str, key: str, label: str, topic_id: int, topic: str, manual=False) -> None:
        """kind: "artistas" o "mapeo_generos"; key ya normalizada. `manual`:
        cargado con /mapear (para un artista, gana sobre el genero)."""
        self.learned[kind][key] = {"nombre": label, "topic_id": topic_id, "tema": topic, "manual": manual}
        self.save_learned()

    def forget(self, kind: str, key: str) -> bool:
        if self.learned[kind].pop(key, None) is None:
            return False
        self.save_learned()
        return True
