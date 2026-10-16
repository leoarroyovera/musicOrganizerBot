"""Ultimo recurso antes de dejar un album pendiente: Claude investiga en la
web el genero del album y elige el tema mas cercano de la lista."""
import json

import anthropic

import config

SYSTEM = (
    "Clasificás discos para un grupo de Telegram de música en WAV/FLAC organizado por temas "
    "(uno por género). Te doy un álbum y la lista exacta de temas. Si no conocés bien el "
    "álbum o el artista, buscá en la web su género. Elegí el tema que mejor le corresponde; "
    "si ninguno es exacto, el más cercano por estilo. Respondé NINGUNO solo si de verdad no "
    "encontrás información del álbum ni del artista."
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


async def pick_topic(artist, album, year, genres, filenames, topics: list[str]):
    """Devuelve (tema, genero, motivo) o None si Claude no encontro uno.
    `topics` son los titulos exactos de los temas destino."""
    choices = list(dict.fromkeys(topics)) + [NONE]
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
    lines.append("\nTemas disponibles:\n" + "\n".join(f"- {t}" for t in choices[:-1]))
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
    if data.get("tema") == NONE or data.get("confianza") == "baja":
        return None
    return data["tema"], data.get("genero", ""), data.get("motivo", "")
