"""Empareja generos musicales con los temas del grupo destino.

Cada genero candidato (de etiquetas, captions o busquedas online) vota por
el tema que mejor le corresponde:
  100  mismo nombre ("Rock" == "rock")
   90  uno contiene al otro ("alternative rock" -> tema "Rock")
   70  misma familia y el tema es el generico ("shoegaze" -> tema "Rock";
       66 si el generico tiene mas palabras, como "Rock en Español")
   60  nombre parecido (errores de tipeo)
   40  misma familia pero el tema es de un subgenero ("bachata" -> tema "Salsa")
   25  familia cercana ("metal" -> tema "Rock" si no hay tema de metal)
Gana el tema con mas puntos sumando todos los candidatos (cada uno con un
peso segun de donde salio)."""
import difflib
import json
import re
import unicodedata
from dataclasses import dataclass, field


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("&", " and ").replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _contains(haystack: str, needle: str) -> bool:
    return f" {needle} " in f" {haystack} "


@dataclass
class Topic:
    id: int
    title: str
    norm: str
    generic: set = field(default_factory=set)   # familias para las que es el tema generico
    primary: set = field(default_factory=set)   # ...y se llama exactamente como la familia ("Rock")
    specific: set = field(default_factory=set)  # familias de las que es un subgenero


@dataclass
class Match:
    topic: Topic
    genre: str   # el candidato que mas aporto
    score: float


class Matcher:
    def __init__(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.related = data.get("cercanas", {})
        # (frase normalizada, familia, es_nombre_generico), largas primero
        phrases = []
        for fam, d in data["familias"].items():
            for p in [fam] + d.get("nombres", []):
                phrases.append((norm(p), fam, True))
            for p in d.get("subgeneros", []):
                phrases.append((norm(p), fam, False))
        # Nombre principal de cada familia: desempata entre "Rock" y "Rock en
        # Español" cuando el genero solo dice la familia ("shoegaze").
        self.primary_names = {fam: {norm(fam)} | {norm(n) for n in d.get("nombres", [])[:1]}
                              for fam, d in data["familias"].items()}
        seen, self.phrases = set(), []
        for p in sorted(phrases, key=lambda x: -len(x[0])):
            if p[0] and (p[0], p[1]) not in seen:
                seen.add((p[0], p[1]))
                self.phrases.append(p)

    def _hits(self, text: str):
        padded = f" {text} "
        for phrase, fam, is_name in self.phrases:
            pos = padded.find(f" {phrase} ")
            if pos >= 0:
                yield pos, len(phrase), fam, is_name

    def family_of(self, genre: str) -> str | None:
        """Familia de un genero: la frase conocida mas larga que contiene; a
        igual largo, la de mas a la derecha ("indie pop" -> pop)."""
        best = None
        for pos, length, fam, _ in self._hits(norm(genre)):
            if best is None or (length, pos) > (best[0], best[1]):
                best = (length, pos, fam)
        return best[2] if best else None

    def index_topics(self, topics: list[tuple[int, str]]) -> list[Topic]:
        out = []
        for tid, title in topics:
            t = Topic(tid, title, norm(title))
            for _, _, fam, is_name in self._hits(t.norm):
                (t.generic if is_name else t.specific).add(fam)
            t.specific -= t.generic
            t.primary = {fam for fam in t.generic if t.norm in self.primary_names[fam]}
            out.append(t)
        return out

    def score(self, genre: str, topic: Topic) -> int:
        g, t = norm(genre), topic.norm
        if not g or not t:
            return 0
        if g == t:
            return 100
        fam = self.family_of(g)
        if len(g) >= 3 and len(t) >= 3 and (_contains(t, g) or _contains(g, t)):
            # "latin pop" contiene "pop", pero es de la familia latina: si el
            # tema es de otra familia vale menos que el tema "Latina".
            if fam and (topic.generic or topic.specific) and fam not in topic.generic | topic.specific:
                return 50
            return 90
        if fam and fam in topic.generic:
            return 70 if fam in topic.primary else 66
        if difflib.SequenceMatcher(None, g, t).ratio() >= 0.85:
            return 60
        if fam and fam in topic.specific:
            return 40
        related = self.related.get(fam, []) if fam else []
        if any(rel in topic.primary for rel in related):
            return 25
        if any(rel in topic.generic for rel in related):
            return 22
        return 0

    def best(self, candidates: list[tuple[str, float]], topics: list[Topic],
             learned: dict | None = None) -> Match | None:
        """`candidates`: [(genero, peso)]. `learned`: {genero_normalizado:
        topic_id} de lo que respondiste antes (pesa mas que cualquier regla)."""
        learned = learned or {}
        by_id = {t.id: t for t in topics}
        totals, top_single, reason = {}, {}, {}
        for genre, weight in candidates:
            tid = learned.get(norm(genre))
            if tid in by_id:
                scored = [(120, by_id[tid])]
            else:
                scored = [(self.score(genre, t), t) for t in topics]
                scored = [(s, t) for s, t in scored if s > 0]
            if not scored:
                continue
            top = max(s for s, _ in scored)
            for s, t in scored:
                if s != top:
                    continue  # cada genero vota solo por su(s) mejor(es) tema(s)
                totals[t.id] = totals.get(t.id, 0) + s * weight
                if s > top_single.get(t.id, 0):
                    top_single[t.id], reason[t.id] = s, genre
        if not totals:
            return None
        ranking = sorted(totals, key=lambda tid: (totals[tid], top_single[tid]), reverse=True)
        first = ranking[0]
        if len(ranking) > 1:
            second = ranking[1]
            if (totals[second], top_single[second]) == (totals[first], top_single[first]):
                return None  # empate real: mejor preguntar
        return Match(by_id[first], reason[first], totals[first])
