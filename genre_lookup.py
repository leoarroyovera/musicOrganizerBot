"""Busqueda del genero (y la portada) de un album/artista en servicios
gratuitos sin clave: MusicBrainz (generos votados por la comunidad y Cover Art
Archive), Deezer e iTunes."""
import asyncio
import difflib
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

import config
from topic_match import norm

# Peso de cada fuente al votar el tema.
WEIGHTS = {"musicbrainz": 0.8, "deezer": 1.0, "itunes": 1.0}
ARTIST_WEIGHT = 0.6  # generos del artista (no del album puntual)
# Portadas: Cover Art Archive es la del lanzamiento exacto; iTunes y Deezer
# suelen ser la de la reedicion digital.
COVER_PRIORITY = ["musicbrainz", "itunes", "deezer"]
MB_MIN_INTERVAL = 1.1  # MusicBrainz permite 1 pedido por segundo


@dataclass
class LookupResult:
    candidates: list = field(default_factory=list)  # [(genero, peso, fuente)]
    answered: int = 0  # fuentes que respondieron (aunque no encontraran nada)
    failed: int = 0    # fuentes que fallaron por red/servidor
    covers: list = field(default_factory=list)  # [(fuente, url)] del album exacto

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

    # Cada fuente devuelve (generos, solo_por_artista, url_de_portada). La
    # portada solo cuando encontro el album exacto, no solo al artista.

    async def _musicbrainz(self, artist, album):
        cover = None
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
                cover = f"https://coverartarchive.org/release-group/{rg['id']}/front-1200"
                full = await self._mb_get(f"release-group/{rg['id']}", inc="genres+tags")
                names = self._mb_names(full)
                if names:
                    return names, False, cover
                break
        if not artist:
            return [], False, cover
        data = await self._mb_get("artist/", query=f'artist:"{_lucene(artist)}"', limit=3)
        for a in data.get("artists", []):
            if a.get("score", 0) >= 90 and _similar(a.get("name", ""), artist):
                full = await self._mb_get(f"artist/{a['id']}", inc="genres+tags")
                return self._mb_names(full), True, cover
        return [], False, cover

    async def _deezer(self, artist, album):
        if not album:
            return [], False, None
        q = f'artist:"{artist}" album:"{album}"' if artist else f'album:"{album}"'
        data = await self._get("https://api.deezer.com/search/album", q=q, limit=5)
        if "error" in data:
            raise httpx.HTTPError(f"deezer: {data['error']}")
        for item in data.get("data", []):
            if _album_ok(item.get("title", ""), item.get("artist", {}).get("name", ""), album, artist):
                full = await self._get(f"https://api.deezer.com/album/{item['id']}")
                names = [g["name"] for g in (full.get("genres") or {}).get("data", [])]
                return names, False, full.get("cover_xl") or item.get("cover_xl")
        return [], False, None

    async def _itunes(self, artist, album):
        if album:
            data = await self._get("https://itunes.apple.com/search", term=f"{artist} {album}".strip(),
                                   media="music", entity="album", limit=10)
            for item in data.get("results", []):
                if _album_ok(item.get("collectionName", ""), item.get("artistName", ""), album, artist):
                    art = item.get("artworkUrl100")
                    cover = art.replace("100x100bb", "1200x1200bb") if art else None
                    return ([item["primaryGenreName"]] if item.get("primaryGenreName") else []), False, cover
        if not artist:
            return [], False, None
        data = await self._get("https://itunes.apple.com/search", term=artist,
                               media="music", entity="musicArtist", limit=5)
        for item in data.get("results", []):
            if _similar(item.get("artistName", ""), artist) and item.get("primaryGenreName"):
                return [item["primaryGenreName"]], True, None
        return [], False, None

    # ---------- texto sin separar ----------

    @staticmethod
    def _split_match(text: str, artist: str, title: str) -> bool:
        """El texto es "Artista Album" (o "Album Artista") con esos nombres."""
        a, t, want = norm(artist), norm(title), norm(text)
        if not a or not t or a in VARIOUS:
            return False
        return any(difflib.SequenceMatcher(None, want, joined).ratio() >= 0.9
                   for joined in (f"{a} {t}", f"{t} {a}"))

    async def split_title(self, text: str):
        """(artista, album) para un nombre sin " - " ("Christina Aguilera
        Keeps Gettin' Better; A Decade of Hits"): se busca el texto entero en
        Deezer e iTunes y se acepta un disco cuyo artista + titulo sea ese
        texto. None si no aparece; "offline" si no respondio nadie."""
        key = f"split|{norm(text)}"
        cached = self.store.lookup_cache.get(key)
        if isinstance(cached, dict):
            return (cached["artista"], cached["album"]) if cached.get("artista") else None

        async def deezer():
            data = await self._get("https://api.deezer.com/search/album", q=text, limit=10)
            if "error" in data:
                raise httpx.HTTPError(f"deezer: {data['error']}")
            return [(i.get("artist", {}).get("name", ""), i.get("title", "")) for i in data.get("data", [])]

        async def itunes():
            data = await self._get("https://itunes.apple.com/search", term=text,
                                   media="music", entity="album", limit=10)
            return [(i.get("artistName", ""), i.get("collectionName", "")) for i in data.get("results", [])]

        outcomes = await asyncio.gather(deezer(), itunes(), return_exceptions=True)
        failed = [o for o in outcomes if isinstance(o, Exception)]
        for o in failed:
            print(f"  [busqueda] separar {text!r} fallo: {o!r}")
        found = None
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                continue
            found = next(((a, t) for a, t in outcome if self._split_match(text, a, t)), None)
            if found:
                break
        if found or not failed:
            self.store.lookup_cache[key] = {"artista": found and found[0], "album": found and found[1]}
            self.store.save_lookup_cache()
        if not found and len(failed) == len(outcomes):
            return "offline"
        return found

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
        # Las entradas viejas (una lista) no tienen portadas: se vuelve a buscar.
        if isinstance(cached, dict):
            result.candidates = [tuple(c) for c in cached["generos"]]
            result.covers = [tuple(c) for c in cached.get("portadas", [])]
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
            genres, artist_level, cover = outcome
            weight = WEIGHTS[name] * (ARTIST_WEIGHT if artist_level else 1)
            for i, g in enumerate(genres):
                # Los primeros generos de MusicBrainz son los mas votados.
                result.candidates.append((g, round(weight * (1 - 0.15 * i), 3), name))
            if cover:
                result.covers.append((name, cover))
        result.covers.sort(key=lambda c: COVER_PRIORITY.index(c[0]))
        # Solo se cachea si respondieron todas: si alguna fallo, la proxima
        # vez se vuelve a intentar.
        if result.failed == 0:
            self.store.lookup_cache[key] = {"generos": [list(c) for c in result.candidates],
                                            "portadas": [list(c) for c in result.covers]}
            self.store.save_lookup_cache()
        return result

    async def download_cover(self, url: str, dest_base) -> Path | None:
        """Baja una portada a `dest_base` (+ .jpg/.png). None si no hay o no
        es una imagen (Cover Art Archive da 404 si el disco no tiene)."""
        try:
            r = await self.http.get(url)
        except httpx.HTTPError as e:
            print(f"  [portada] no pude bajar {url}: {e!r}")
            return None
        kind = r.headers.get("content-type", "")
        if r.status_code != 200 or not kind.startswith("image/") or len(r.content) < 5000:
            return None
        path = Path(f"{dest_base}{'.png' if 'png' in kind else '.jpg'}")
        path.write_bytes(r.content)
        return path
