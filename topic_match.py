"""Decide la familia de generos de un album y el tema de WAV/FLAC que le toca.

Cada fuente (MusicBrainz, Deezer, iTunes, etiquetas/caption, Claude) da una
lista de generos. Cada fuente vota una sola vez, por la familia que mas
aparece en su lista (los primeros generos pesan mas). Las etiquetas muy
amplias de generos.json ("Pop", "Latin") valen medio voto. Gana la familia
con mas votos; ante un empate, la de la fuente mas confiable (MusicBrainz
primero, porque sus generos son por album y votados por la comunidad),
salvo que una de las empatadas este en "ganan_empates" (las familias latinas
y regionales son mas precisas que "Pop" o "Rock"). Una fuente que encontro
al artista pero no al album vale medio voto: puede ser otro artista con el
mismo nombre.

El tema es el de esa familia: uno de subgenero si el album lo menciona
("Salsa"), si no el generico ("Cumbia y Tropical" o cualquiera de sus
nombres); si no hay ninguno, el bot lo crea con el nombre de "tema"."""
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

# Desempate entre fuentes: primero la mas confiable.
SOURCE_PRIORITY = ["manual", "musicbrainz", "deezer", "itunes", "etiquetas", "titulo", "claude"]
WEAK_VOTE = 0.5
MAPPED = "tema:"  # familia "virtual" de un genero que mapeaste a mano a un tema
ARTIST_SUFFIX = " (artista)"  # fuente que solo encontro al artista, no al album


def _source_rank(source: str) -> int:
    base = source.removesuffix(ARTIST_SUFFIX)
    rank = SOURCE_PRIORITY.index(base) if base in SOURCE_PRIORITY else len(SOURCE_PRIORITY)
    return rank + (len(SOURCE_PRIORITY) if source.endswith(ARTIST_SUFFIX) else 0)


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
    family: str | None = None  # None: tema que el bot no llena solo (Radio, Oldies…)
    generic: bool = False      # se llama como la familia (no como un subgenero)
    phrase: str = ""           # la frase de generos.json que reconoci en el titulo


@dataclass
class Verdict:
    family: str           # clave de generos.json, o "tema:<id>" si fue un mapeo manual
    genre: str            # el genero que mas aporto (para el caption)
    votes: dict = field(default_factory=dict)  # fuente -> (familia, genero, peso)


class Matcher:
    def __init__(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.families = data["familias"]
        self.weak = {norm(w) for w in data.get("debiles", [])}
        self.tie_pref = data.get("ganan_empates", [])
        self.regional = set(data.get("regionales", []))
        # "Latin" a secas dice que el disco es latino, no de que estilo: no
        # alcanza para la regla de "regionales".
        self.umbrella = {norm(w) for w in data.get("paraguas", [])}
        phrases = []
        for fam, d in self.families.items():
            for p in [fam, d.get("tema", fam)] + d.get("nombres", []):
                phrases.append((norm(p), fam, True))
            for p in d.get("subgeneros", []):
                phrases.append((norm(p), fam, False))
        seen, self.phrases = set(), []
        for p in sorted(phrases, key=lambda x: -len(x[0])):
            if p[0] and p[0] not in seen:  # la primera familia que la declara se la queda
                seen.add(p[0])
                self.phrases.append(p)

    def theme_name(self, family: str) -> str:
        return self.families[family].get("tema", family.title())

    def match(self, text: str):
        """(frase, familia, es_nombre) de la frase conocida mas larga que
        contiene `text`; a igual largo, la de mas a la derecha ("indie pop")."""
        padded, best = f" {text} ", None
        for phrase, fam, is_name in self.phrases:
            pos = padded.find(f" {phrase} ")
            if pos >= 0 and (best is None or (len(phrase), pos) > (len(best[0]), best[3])):
                best = (phrase, fam, is_name, pos)
        return best[:3] if best else None

    def family_of(self, genre: str) -> str | None:
        hit = self.match(norm(genre))
        return hit[1] if hit else None

    def index_topics(self, topics: list[tuple[int, str]]) -> list[Topic]:
        out = []
        for tid, title in topics:
            t = Topic(tid, title, norm(title))
            hit = self.match(t.norm)
            if hit:
                t.phrase, t.family, t.generic = hit
            out.append(t)
        return out

    # ---------- votacion ----------

    def classify(self, evidence: list[tuple[str, list[str]]], mapped: dict | None = None) -> Verdict | None:
        """`evidence`: [(fuente, [generos en orden])]. `mapped`: {genero
        normalizado: topic_id} de /mapear genero."""
        mapped = mapped or {}
        totals, votes = Counter(), {}
        for source, genres in evidence:
            scores, first, strong, specific = Counter(), {}, set(), set()
            for i, genre in enumerate(genres):
                key = norm(genre)
                fam = f"{MAPPED}{mapped[key]}" if key in mapped else self.family_of(genre)
                if not fam:
                    continue
                weak = key in self.weak
                scores[fam] += max(1 - 0.15 * i, 0.2) * (WEAK_VOTE if weak else 1)
                first.setdefault(fam, genre)
                if not weak:
                    strong.add(fam)
                if key not in self.umbrella:
                    specific.add(fam)
            if not scores:
                continue
            fam = max(scores, key=lambda f: (scores[f], f in strong))
            # "rock, latin rock" o "Rock, Rock Latino": la variante regional es
            # la que dice de que disco se trata, aunque venga despues.
            regional = [f for f in scores if f in self.regional and f in specific]
            if regional:
                fam = max(regional, key=lambda f: scores[f])
            weight = 1 if fam in strong else WEAK_VOTE
            if source.endswith(ARTIST_SUFFIX):
                weight *= WEAK_VOTE  # generos del artista: puede ser otro con el mismo nombre
            if any(f.startswith(MAPPED) for f in scores):
                fam = next(f for f in scores if f.startswith(MAPPED))
                weight = 100  # un genero mapeado a mano manda
            totals[fam] += weight
            votes[source] = (fam, first[fam], weight)
        if not totals:
            return None
        top = max(totals.values())
        tied = [f for f, n in totals.items() if abs(n - top) < 1e-9]

        def rank(fam):
            pref = self.tie_pref.index(fam) if fam in self.tie_pref else len(self.tie_pref)
            return pref, min(_source_rank(s) for s, v in votes.items() if v[0] == fam)

        winner = min(tied, key=rank)
        genre = next(v[1] for s, v in sorted(votes.items(), key=lambda kv: _source_rank(kv[0]))
                     if v[0] == winner)
        return Verdict(winner, genre, votes)

    def topic_for(self, verdict: Verdict, all_genres: list[str], topics: list[Topic]) -> Topic | None:
        """Tema existente para el veredicto, o None si hay que crearlo."""
        if verdict.family.startswith(MAPPED):
            tid = int(verdict.family[len(MAPPED):])
            return next((t for t in topics if t.id == tid), None)
        same = [t for t in topics if t.family == verdict.family]
        genres = [norm(g) for g in all_genres]
        specific = [t for t in same if not t.generic and any(_contains(g, t.phrase) for g in genres)]
        if specific:
            return max(specific, key=lambda t: len(t.phrase))
        generic = [t for t in same if t.generic]
        if not generic:
            return None
        theme = norm(self.theme_name(verdict.family))
        return next((t for t in generic if t.norm == theme), generic[0])
