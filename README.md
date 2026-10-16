# musicOrganizerBot

Bot de Telegram que complementa el supergrupo **WAV/FLAC**: le pasás el link de otro grupo o supergrupo, recorre sus mensajes y publica cada álbum en el tema de WAV/FLAC que corresponde a su género.

- **Audios sueltos** (FLAC, WAV, AIFF, APE, WV, DSF/DFF): los audios consecutivos de un mismo álbum se juntan en un **ZIP sin compresión** (como el modo ZIP de rateroBot), con nombre `Artista - Álbum (Año)`.
- **Comprimidos** (`.zip`, `.rar`, `.7z`, incluidos los multiparte `.part1.rar`, `.7z.001`, `.z01`…): se reenvían tal cual, como archivo nuevo.
- **Carátulas**: las imágenes junto a un álbum se publican antes del ZIP, con un caption `💿 Artista - Álbum (Año) / 🏷 género`. Si el álbum no trae imagen se usa la carátula embebida en el primer audio (o el `cover.jpg` que haya dentro del ZIP). Las imágenes que no están cerca de ningún álbum se ignoran.

Se basa en [cloneToTelegram](../cloneToTelegram) (recorrido del grupo, checkpoint, bot de control) y [rateroBot](../rateroBot) (descarga y subida en paralelo, ZIP, nombre del álbum).

## Setup

1. Instalar dependencias (Python 3.10+):
   ```
   pip install -r requirements.txt
   ```
2. Copiar `.env.example` a `.env` y completar:
   - `TG_API_ID` / `TG_API_HASH`: de https://my.telegram.org (pueden ser los mismos de los otros bots).
   - `TG_BOT_TOKEN`: un bot **nuevo** de @BotFather (no reutilices el token de otro bot que esté corriendo).
   - `TG_OWNER_ID`: tu user_id numérico (@userinfobot). El bot ignora a cualquier otra persona.
   - `DESTINO`: ya viene con el link de WAV/FLAC. Tu cuenta tiene que ser miembro (y poder escribir en sus temas).
   - Opcional: `ANTHROPIC_API_KEY` para que Claude, con búsqueda web, decida los álbumes que las búsquedas gratuitas no resuelven (ver abajo).
3. Login de tu cuenta, una sola vez y en una terminal interactiva:
   ```
   python login.py
   ```
   Queda en `organizer_user.session`. Es una sesión propia: no copies la de cloneToTelegram o rateroBot si esos bots siguen corriendo, porque usar la misma sesión desde dos procesos a la vez puede invalidarla.
4. Levantar el bot:
   ```
   python bot.py
   ```
   Al arrancar te escribe "🟢 Organizador listo" con el menú.
5. Recomendado antes de la primera corrida: `/temas` para ver los temas de WAV/FLAC y qué familia de géneros detecté en cada uno, y `/analizar <link>` para ver qué se mandaría y adónde sin enviar nada.

## Uso

Mandale al bot el link de un grupo:

- `t.me/usuario` o `@usuario` (público), `t.me/+invitación` o `t.me/joinchat/…` (privado), `t.me/c/1234567` (uno del que ya sos miembro).
- Un link a un mensaje (`t.me/usuario/500`) empieza desde ese mensaje la primera vez.
- Si tu cuenta no es miembro de un grupo privado, el bot pregunta antes de unirse.

Te muestra dos botones: **▶️ Organizar** o **🔍 Solo analizar**. Mientras trabaja edita un mensaje de estado con el álbum actual y el progreso (con botón **⛔ Cancelar**). Al terminar manda un resumen: enviados, duplicados, pendientes y errores.

### Cómo se arman los álbumes

En cada tema (o en todo el chat, si el grupo no tiene Temas) los mensajes se recorren en orden:

- Audios seguidos forman un álbum. Se corta cuando pasan más de `PAUSA_MINUTOS` (30) entre uno y otro, cuando aparece una imagen o un texto `Artista - Álbum (Año)` que presenta al siguiente, o cuando aparece otro tipo de archivo (video, mp3…). Después de bajar, si las etiquetas muestran dos álbumes distintos seguidos, salen dos ZIP.
- Las partes de un mismo comprimido (`.part1.rar`, `.part2.rar`…) van juntas.
- Cada imagen se asigna al álbum más cercano en el tiempo, antes o después, si está a menos de 30 minutos.
- Los álbumes subidos hace menos de `ESPERA_MINUTOS` (15) se dejan para la próxima corrida, por si todavía los están subiendo.

### Cómo se decide el tema

En este orden, y en cuanto un paso da un tema se usa ese:

1. **Lo aprendido**: si ya elegiste un tema para ese artista, va ahí.
2. **Etiquetas y caption**: el género de las etiquetas de los audios, hashtags (`#rock`) y líneas `Género: …` del caption.
3. **Búsqueda online gratuita**: MusicBrainz, Deezer e iTunes, primero por álbum y si no por artista.
4. **Claude con búsqueda web** (solo si configuraste `ANTHROPIC_API_KEY`): investiga el álbum y elige el tema más cercano de la lista. Responde "ninguno" si no encuentra nada confiable.
5. **Pendiente**: te llega un mensaje con los géneros encontrados y botones con todos los temas (más "➕ Crear tema «género»", "🔍 Buscar de nuevo" y "🗑 Descartar"). Si no hubo conexión para buscar, el mensaje lo dice.

Para emparejar un género con un tema se usa `generos.json`: familias (rock, metal, electrónica, latina…) con sus nombres y subgéneros en inglés y español. Así `shoegaze` cae en el tema "Rock", `bachata` en "Música Latina" y, si no hay un tema de metal, `death metal` va al de rock (familias "cercanas"). Cada género encontrado vota, y gana el tema con más votos. Si tus temas tienen nombres que no figuran ahí, agregalos a `generos.json` y reiniciá el bot.

**Para que haya cada vez menos pendientes**, cuando elegís un tema el bot aprende: el artista y los géneros de ese álbum van a ese tema la próxima vez. Lo aprendido se ve con `/aprendido`, se corrige con `/olvidar` y se carga a mano con `/mapear`.

### Duplicados y reanudación

- Cada álbum enviado se registra por el nombre y tamaño de sus archivos (`data/enviados.json`). Si el mismo álbum aparece en otro grupo, o volvés a correr el mismo, no se manda de nuevo.
- Por cada grupo origen se guarda hasta qué mensaje se procesó (`data/fuentes/<id>.json`). Si cancelás o se corta, la próxima corrida sigue desde ahí. `/actualizar` revisa solo lo nuevo de los grupos ya organizados.

## Comandos

También aparecen en el botón **Menú** de Telegram, y `/menu` los muestra como botones.

| Comando | Qué hace |
|---|---|
| `/menu` (o `/start`) | Menú con botones: Organizar grupo, Actualizar, Pendientes, Estado, Fuentes, Temas, Aprendido, Logs, Cancelar y Ayuda. |
| `/organizar <link>` | Organiza el grupo (también podés mandar el link solo). |
| `/analizar <link>` | Lista los álbumes que encontraría y a qué tema irían, sin descargar ni enviar nada. Sin etiquetas (no descarga) y sin Claude, así que algunos pueden aparecer con ❓ y resolverse igual al organizar. |
| `/actualizar` | Revisa lo nuevo de una fuente ya organizada, o de todas. |
| `/pendientes` | Muestra los álbumes que esperan que elijas el tema (o que fallaron, con botón para reintentar). |
| `/estado` | Tarea en curso y tareas en cola. |
| `/cancelar` | Cancela la tarea en curso. Lo ya enviado queda y la próxima vez sigue desde ahí. |
| `/fuentes` | Grupos organizados con sus números (enviados, duplicados, pendientes, errores) y la última corrida. |
| `/temas` | Temas de WAV/FLAC (relee la lista) y la familia de géneros que detecté en cada uno. |
| `/aprendido` | Artistas y géneros aprendidos y a qué tema van. |
| `/mapear artista\|genero <nombre> = <tema>` | Asigna a mano, p.ej. `/mapear artista Soda Stereo = Rock en Español`. |
| `/olvidar artista\|genero <nombre>` | Borra algo aprendido. |
| `/logs [n]` | Últimas `n` líneas (default 30) del log. |
| `/ayuda` | Cómo funciona y la lista de comandos. |

Las tareas corren de a una; si mandás otro link mientras trabaja, queda en cola.

## Configuración (`.env`)

| Variable | Default | Qué hace |
|---|---|---|
| `DESTINO` | link de WAV/FLAC | Supergrupo destino con Temas. |
| `EXTENSIONES_AUDIO` | `flac,wav,aiff,aif,ape,wv,dsf,dff,alac` | Audios sueltos que se juntan en ZIP. Los demás (mp3, aac…) se ignoran y cortan el álbum en curso. |
| `PAUSA_MINUTOS` | 30 | Pausa entre audios que separa dos álbumes. |
| `ESPERA_MINUTOS` | 15 | Antigüedad mínima de un álbum para procesarlo. |
| `MAX_UPLOAD_MB` | automático | 4000 con Premium, 2000 sin. Los archivos más grandes quedan como pendientes con error. |
| `ZIP_MAX_MB` | límite de subida | Si el ZIP lo supera se parte en `_parte1.zip`, `_parte2.zip`… |
| `MARGEN_DISCO_MB` | 500 | Espacio libre mínimo a dejar en disco. Cada álbum necesita en `tmp/` el doble de su tamaño. |
| `CONTACTO_MUSICBRAINZ` | vacío | Email o URL para el User-Agent; MusicBrainz lo pide y limita menos con contacto. |
| `ANTHROPIC_API_KEY` | vacío | Activa el paso de Claude. Se cobra por uso; solo se llama para los álbumes que las búsquedas gratuitas no resolvieron. |
| `BUSQUEDA_CLAUDE` | automático | `0` apaga Claude aunque haya clave. |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Modelo para ese paso. |

## Datos

En `data/` (no se sube al repo):

```
fuentes/<id>.json      checkpoint y números por grupo origen
pendientes.json        álbumes esperando tema o reintento
enviados.json          álbumes ya publicados (evita duplicados)
aprendido.json         artista -> tema y género -> tema
cache_busquedas.json   resultados de MusicBrainz/Deezer/iTunes
```

Logs en `logs/bot.log`.

## Despliegue en una VPS

Igual que rateroBot, con su propio venv:

```bash
cd ~
git clone https://github.com/leoarroyovera/musicOrganizerBot.git && cd musicOrganizerBot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
# copiar .env desde la PC (scp) y loguear la cuenta:
venv/bin/python login.py
sudo cp musicorganizer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now musicorganizer
```

Logs: `journalctl -u musicorganizer -f` o `tail -f logs/bot.log`.

## Estructura

```
bot.py            bot de control: menú, comandos, cola de tareas, pendientes
organizer.py      resolver links, decidir el tema, publicar cada álbum
scanner.py        recorrer mensajes y agruparlos en álbumes
music_meta.py     etiquetas, carátula embebida, "Artista - Álbum (Año)"
topic_match.py    emparejar géneros con temas (usa generos.json)
genre_lookup.py   MusicBrainz, Deezer e iTunes
claude_pick.py    Claude con búsqueda web (opcional)
media.py          descarga, ZIP y subida (de rateroBot)
fast_upload.py / fast_download.py   conexiones en paralelo (de rateroBot)
album_name.py     nombre del ZIP (de rateroBot)
store.py          estado en JSON
```
