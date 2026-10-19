"""Busqueda del genero de un album/artista en servicios gratuitos sin clave:
MusicBrainz (generos votados por la comunidad), Deezer e iTunes."""
import asyncio
import difflib
import time
from dataclasses import dataclass, field

import httpx

import config
from topic_match import norm

# Peso de cada fuente al votar el tema.
WEIGHTS = {"musicbrainz": 0.8, "deezer": 1.0, "itunes": 1.0}
ARTIST_WEIGHT = 0.6  # generos del artista (no del album puntual)
MB_MIN_INTERVAL = 1.1  # MusicBrainz permite 1 pedido por segundo


@dataclass
class LookupResult:
    candidates: list = field(default_factory=list)  # [(genero, peso, fuente)]
    answered: int = 0  # fuentes que respondieron (aunque no encontraran nada)
    failed: int = 0    # fuentes que fallaron por red/servidor

    @property
    def offline(self) -> bool:
        return self.answered == 0 and self.failed > 0


def _similar(found: str, wanted: str) -> bool:
    """`wanted` vacio (p.ej. sin artista en un compilado) acepta cualquiera."""
    a, b = norm(found), norm(wanted)
    if not b:
        return True
    if not a:
        return False
    return a == b or a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


VARIOUS = {"various artists", "varios artistas", "various", "varios", "va", "artistas varios"}


def _album_ok(found_title: str, found_artist: str, album: str, artist: str) -> bool:
    """Sin artista (un compilado) el titulo tiene que ser identico y el disco
    encontrado tiene que ser de "Various Artists": "Dance Mania" a secas
    coincidia con el disco de Tito Puente de 1958."""
    if artist:
        return _similar(found_title, album) and _similar(found_artist, artist)
    return norm(found_title) == norm(album) and norm(found_artist) in VARIOUS


def _lucene(text: str) -> str:
    return text.replace("\\", " ").replace('"', " ")


class GenreLookup:
    def __init__(self, store):
        self.store = store
        self._mb_lock = asyncio.Lock()
        self._mb_last = 0.0
        self.http = httpx.AsyncClient(timeout=15, headers={"User-Agent": config.USER_AGENT},
                                      follow_redirects=True)

    async def close(self):
        await self.http.aclose()

    async def _get(self, url, **params):
        # 503/429 es como MusicBrainz (y a veces Deezer) piden bajar el ritmo.
        for attempt in range(3):
            r = await self.http.get(url, params=params)
            if r.status_code not in (429, 503) or attempt == 2:
                break
            await asyncio.sleep(2 * (attempt + 1))
        r.raise_for_status()
        return r.json()

    async def _mb_get(self, path, **params):
        async with self._mb_lock:
            wait = self._mb_last + MB_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._get(f"https://musicbrainz.org/ws/2/{path}", fmt="json", **params)
            finally:
                self._mb_last = time.monotonic()

    # ---------- fuentes ----------

    @staticmethod
    def _mb_names(entity, limit=4):
        genres = sorted(entity.get("genres") or [], key=lambda g: -g.get("count", 0))
        names = [g["name"] for g in genres][:limit]
        if not names:  # sin generos curados: los tags mas votados
            tags = sorted(entity.get("tags") or [], key=lambda g: -g.get("count", 0))
            names = [t["name"] for t in tags if t.get("count", 0) > 0][:limit]
        return names

    async def _musicbrainz(self, artist, album):
        if album:
            q = f'releasegroup:"{_lucene(album)}"'
            if artist:
                q += f' AND artist:"{_lucene(artist)}"'
            data = await self._mb_get("release-group/", query=q, limit=5)
            for rg in data.get("release-groups", []):
                credit = " ".join(c.get("name", "") for c in rg.get("artist-credit", []))
                # Con artista la consulta ya lo exige; el credito se mira solo en compilados.
                if rg.get("score", 0) < 85 or not _album_ok(rg.get("title", ""), credit if not artist else artist,
                                                            album, artist):
                    continue
                full = await self._mb_get(f"release-group/{rg['id']}", inc="genres+tags")
                names = self._mb_names(full)
                if names:
                    return names, False
                break
        if not artist:
            return [], False
        data = await self._mb_get("artist/", query=f'artist:"{_lucene(artist)}"', limit=3)
        for a in data.get("artists", []):
            if a.get("score", 0) >= 90 and _similar(a.get("name", ""), artist):
                full = await self._mb_get(f"artist/{a['id']}", inc="genres+tags")
                return self._mb_names(full), True
        return [], False

    async def _deezer(self, artist, album):
        if not album:
            return [], False
        q = f'artist:"{artist}" album:"{album}"' if artist else f'album:"{album}"'
        data = await self._get("https://api.deezer.com/search/album", q=q, limit=5)
        if "error" in data:
            raise httpx.HTTPError(f"deezer: {data['error']}")
        for item in data.get("data", []):
            if _album_ok(item.get("title", ""), item.get("artist", {}).get("name", ""), album, artist):
                full = await self._get(f"https://api.deezer.com/album/{item['id']}")
                names = [g["name"] for g in (full.get("genres") or {}).get("data", [])]
                return names, False
        return [], False

    async def _itunes(self, artist, album):
        if album:
            data = await self._get("https://itunes.apple.com/search", term=f"{artist} {album}".strip(),
                                   media="music", entity="album", limit=10)
            for item in data.get("results", []):
                if _album_ok(item.get("collectionName", ""), item.get("artistName", ""), album, artist):
                    return [item["primaryGenreName"]] if item.get("primaryGenreName") else [], False
        if not artist:
            return [], False
        data = await self._get("https://itunes.apple.com/search", term=artist,
                               media="music", entity="musicArtist", limit=5)
        for item in data.get("results", []):
            if _similar(item.get("artistName", ""), artist) and item.get("primaryGenreName"):
                return [item["primaryGenreName"]], True
        return [], False

    # ---------- api ----------

    async def lookup(self, artist: str | None, album: str | None) -> LookupResult:
        result = LookupResult()
        if (artist or "").lower() in ("varios artistas", "various artists", "va"):
            artist = ""
        artist = artist or ""
        if not artist and not album:
            return result
        key = f"{norm(artist)}|{norm(album or '')}"
        cached = self.store.lookup_cache.get(key)
        if cached is not None:
            result.candidates = [tuple(c) for c in cached]
            result.answered = 1
            return result

        sources = {"musicbrainz": self._musicbrainz, "deezer": self._deezer, "itunes": self._itunes}
        outcomes = await asyncio.gather(*(fn(artist, album) for fn in sources.values()),
                                        return_exceptions=True)
        for name, outcome in zip(sources, outcomes):
            if isinstance(outcome, Exception):
                print(f"  [busqueda] {name} fallo: {outcome!r}")
                result.failed += 1
                continue
            result.answered += 1
            genres, artist_level = outcome
            weight = WEIGHTS[name] * (ARTIST_WEIGHT if artist_level else 1)
            for i, g in enumerate(genres):
                # Los primeros generos de MusicBrainz son los mas votados.
                result.candidates.append((g, round(weight * (1 - 0.15 * i), 3), name))
        # Solo se cachea si respondieron todas: si alguna fallo, la proxima
        # vez se vuelve a intentar.
        if result.failed == 0:
            self.store.lookup_cache[key] = [list(c) for c in result.candidates]
            self.store.save_lookup_cache()
        return result
