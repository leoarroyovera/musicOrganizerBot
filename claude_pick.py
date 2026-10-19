"""Ultimo recurso antes de dejar un album pendiente: Claude investiga en la
web el genero del album y elige la familia (tema) que le corresponde."""
import json

import anthropic

import config

SYSTEM = (
    "Clasificás discos para un grupo de Telegram de música en WAV/FLAC organizado por temas "
    "(uno por género). Te doy un álbum y la lista exacta de géneros posibles. Buscá en la web "
    "el género del álbum (o del artista) y elegí el de la lista que le corresponde; si ninguno "
    "es exacto, el más cercano por estilo. Si es un compilado de varios artistas, el género "
    "que predomina. Respondé NINGUNO si no encontrás información confiable."
)
NONE = "NINGUNO"
MAX_CONTINUATIONS = 3


class ClaudeUnavailable(Exception):
    """Sin conexion o con la API caida: el album queda pendiente."""


_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


async def pick_family(artist, album, year, genres, filenames, families: dict[str, str]):
    """Devuelve (clave de familia, genero, motivo) o None si Claude no
    encontro uno. `families`: {clave: nombre del tema} de generos.json."""
    by_name = {name: key for key, name in families.items()}
    choices = list(by_name) + [NONE]
    schema = {
        "type": "object",
        "properties": {
            "tema": {"type": "string", "enum": choices},
            "genero": {"type": "string", "description": "género principal del álbum"},
            "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
            "motivo": {"type": "string", "description": "una oración"},
        },
        "required": ["tema", "genero", "confianza", "motivo"],
        "additionalProperties": False,
    }
    lines = [f"Artista: {artist or 'desconocido'}", f"Álbum: {album or 'desconocido'}"]
    if year:
        lines.append(f"Año: {year}")
    if genres:
        lines.append("Géneros encontrados (pueden ser poco útiles): " + ", ".join(genres[:10]))
    if filenames:
        lines.append("Archivos: " + ", ".join(filenames[:15]))
    lines.append("\nGéneros posibles:\n" + "\n".join(f"- {t}" for t in choices[:-1]))
    messages = [{"role": "user", "content": "\n".join(lines)}]

    client = _get_client()
    try:
        for _ in range(MAX_CONTINUATIONS):
            response = await client.beta.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM,
                output_config={"effort": "medium",
                               "format": {"type": "json_schema", "schema": schema}},
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}],
                messages=messages,
            )
            if response.stop_reason != "pause_turn":
                break
            # La busqueda web pauso el turno: se reenvia para que siga.
            messages = messages[:1] + [{"role": "assistant", "content": response.content}]
    except (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.RateLimitError,
            anthropic.InternalServerError) as e:
        raise ClaudeUnavailable(str(e)) from e
    except anthropic.APIStatusError as e:
        print(f"  [claude] error {e.status_code}: {e.message}")
        return None

    if response.stop_reason in ("refusal", "max_tokens", "pause_turn"):
        print(f"  [claude] sin respuesta util (stop_reason={response.stop_reason})")
        return None
    texts = [b.text for b in response.content if b.type == "text"]
    if not texts:
        return None
    try:
        data = json.loads(texts[-1])
    except ValueError:
        print(f"  [claude] respuesta no es JSON: {texts[-1][:200]!r}")
        return None
    if data.get("tema") not in by_name or data.get("confianza") == "baja":
        return None
    return by_name[data["tema"]], data.get("genero", ""), data.get("motivo", "")
