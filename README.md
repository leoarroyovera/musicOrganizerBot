# musicOrganizerBot

Bot de Telegram que complementa el supergrupo **WAV/FLAC**: le pasás el link de otro grupo o supergrupo, recorre sus mensajes y publica cada álbum en el tema de WAV/FLAC que corresponde a su género.

- **Audios sueltos** (FLAC, WAV, AIFF, APE, WV, DSF/DFF): los audios consecutivos de un mismo álbum se juntan en un **ZIP sin compresión** (como el modo ZIP de rateroBot), con nombre `Artista - Álbum (Año)`.
- **Comprimidos** (`.zip`, `.rar`, `.7z`, incluidos los multiparte `.part1.rar`, `.7z.001`, `.z01`…): se reenvían tal cual, como archivo nuevo.
- **Carátulas**: se publican antes del ZIP, con un caption `💿 Artista - Álbum (Año) / 🏷 género`. Una imagen del grupo solo se usa si se verifica que es de ese disco; si no, la que trae el disco adentro o la de internet (ver *Cómo se elige la portada*).

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
- Las imágenes a menos de 30 minutos quedan como *candidatas* del disco de antes y del de después; cuál es de cuál se decide al publicar (ver abajo). Una imagen que responde a un archivo del disco (o a la que responde un archivo), o que va en el mismo álbum de Telegram, es suya aunque esté lejos.
- Los álbumes subidos hace menos de `ESPERA_MINUTOS` (15) se dejan para la próxima corrida, por si todavía los están subiendo.

### Cómo se elige la portada

Las imágenes de un grupo se cruzan entre discos porque unos uploaders suben la portada antes del ZIP y otros después. Por eso la posición no alcanza, y la portada se elige así:

1. **Vínculo seguro**: si la imagen va en el mismo álbum de Telegram que los archivos, o responde a uno de ellos (o un archivo responde a la imagen), es de ese disco.
2. **Caption**: si la imagen dice "Artista - Álbum" y coincide con el disco, es suya. Si nombra a otro disco, se descarta aunque esté pegada.
3. **Comparación visual**: cada imagen candidata se compara, con una huella que aguanta cambios de tamaño y la recompresión de Telegram, con la carátula que trae el disco adentro (embebida en el FLAC o `cover.jpg` del ZIP). Si el disco no trae carátula, se compara con la de internet. Solo se usa si es la misma imagen; las contratapas y las portadas del disco vecino quedan afuera.
4. **Costumbre del grupo**: con los casos verificados, el bot aprende si ese grupo pone la portada antes o después del ZIP (`data/portadas.json`). Cuando no hay con qué comparar, usa la imagen de ese lado, solo si al menos 5 casos verificados lo confirman en un 80 %.
5. **La que trae el disco adentro**.
6. **De internet**: Cover Art Archive (MusicBrainz), iTunes o Deezer, de 600 a 1200 px, solo cuando la búsqueda encontró el álbum exacto. En un compilado sin artista no se publica, porque puede ser de otro compilado con el mismo título.
7. **Sin portada**: si nada de lo anterior se pudo verificar. Una portada equivocada es peor que ninguna.

Con `PORTADA_PREFERIDA=internet` se usa primero la de internet cuando existe (mejor calidad), y el resto queda de respaldo. El resultado de cada disco dice de dónde salió su portada.

### Cómo se decide el tema

El tema es el **género que devuelven las búsquedas**. Cada tema de WAV/FLAC corresponde a una familia de `generos.json` (Dance - Electrónica, Pop, Pop Latino, Cumbia y Tropical, Rock, Rock en Español, Alternativo, Hip Hop / Rap, Romántico, OST…).

1. **Fuentes**: MusicBrainz, Deezer e iTunes (primero por álbum y, si no lo encuentran, por artista), las etiquetas de los audios y el caption (`#rock`, `Género: …`). En los compilados sin artista también cuenta el título ("Flash House 3", "18 Lentos Históricos"), y en cualquier disco un título de banda sonora ("Music From The Motion Picture"). Un compilado sin artista solo se toma de las búsquedas si el disco encontrado es de "Various Artists" y con el mismo título.
2. **Votación**: cada fuente vota una vez, por la familia que más aparece en su lista.
   - Las etiquetas muy amplias ("Pop") valen medio voto, y también la fuente que encontró solo al artista, porque puede ser otro con el mismo nombre.
   - Si una fuente nombra una variante latina o regional ("rock, latin rock"), vota por esa variante.
   - En un empate ganan las familias latinas y regionales; después, MusicBrainz.
3. **Tema**: el de esa familia que ya exista en el grupo, con su nombre de `tema` o cualquiera de sus `nombres` (así "Pop en Español" cuenta como Pop Latino). Si el disco menciona un subgénero que tiene su propio tema (por ejemplo "Salsa"), va a ese. **Si no existe ningún tema para la familia, el bot lo crea.**
4. **Claude con búsqueda web** (solo si configuraste `ANTHROPIC_API_KEY`): si ninguna fuente dio un género, investiga el álbum y elige la familia.
5. **Sin género**: si ya elegiste antes un tema para ese artista, va ahí. Si no, queda **pendiente**. Te llega un mensaje con botones con todos los temas, más "➕ Crear tema «género»", "🔍 Buscar de nuevo", "🗑 Descartar" y "🌐 Buscar en Google", que abre una búsqueda con el artista y el álbum. Si no hubo conexión para buscar, el mensaje lo dice. `/rebuscar` vuelve a buscar el género de todos los pendientes y publica los que encuentra.

Los temas que no son un género (Radio, Oldies, Bailable, 70 & 80…) solo reciben lo que elegís a mano. `/temas` muestra a qué familia corresponde cada tema y cuáles se van a crear.

**Para que haya cada vez menos pendientes**, cuando elegís el tema de un pendiente el bot recuerda el artista, pero solo para cuando tampoco encuentre su género. Con `/mapear artista X = Tema` el artista va siempre a ese tema, gane el género que gane. Con `/mapear genero X = Tema` un género que no está en `generos.json` va a ese tema. Lo aprendido se ve con `/aprendido` y se corrige con `/olvidar`. Los géneros que la versión anterior "aprendía" sola de los pendientes ya no se usan, porque mandaban por ejemplo "rock" a Oldies y "reggae" a Dance.

Si un género o un nombre de tema no figura en `generos.json`, agregalo ahí y reiniciá el bot.

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
| `/rebuscar` | Vuelve a buscar el género de todos los pendientes (sin descargarlos); los que encuentra los descarga y publica. Al final manda un resumen. |
| `/estado` | Tarea en curso y tareas en cola. |
| `/cancelar` | Cancela la tarea en curso. Lo ya enviado queda y la próxima vez sigue desde ahí. |
| `/fuentes` | Grupos organizados con sus números (enviados, duplicados, pendientes, errores) y la última corrida. |
| `/temas` | Temas de WAV/FLAC (relee la lista), la familia de géneros de cada uno y los que se crearán cuando haga falta. |
| `/aprendido` | Artistas (asignados a mano o elegidos en pendientes) y géneros asignados a mano. |
| `/mapear artista\|genero <nombre> = <tema>` | Asigna a mano. Un artista mapeado gana sobre el género (`/mapear artista Soda Stereo = Rock en Español`); un género mapeado sirve para los que no están en `generos.json`. |
| `/olvidar artista\|genero <nombre>` | Borra algo aprendido. |
| `/diagnostico <link> [desde_id] [n]` | Lista los primeros `n` mensajes (default 300) como los ve el bot (remitente, tipo, minutos desde el anterior del mismo tema, respuestas), un resumen por tema del foro (título, audios, imágenes, pausa más larga) y los discos que arma, con el motivo de cada corte. Si es largo llega como .txt. Sirve para entender por qué un disco salió partido. |
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
| `PORTADA_PREFERIDA` | `grupo` | `grupo`: la imagen del grupo si se verifica, si no la de adentro o la de internet. `internet`: primero la de internet. |
| `CONTACTO_MUSICBRAINZ` | vacío | Email o URL para el User-Agent; MusicBrainz lo pide y limita menos con contacto. |
| `ANTHROPIC_API_KEY` | vacío | Activa el paso de Claude. Se cobra por uso; solo se llama para los álbumes sin género en las búsquedas gratuitas. |
| `BUSQUEDA_CLAUDE` | automático | `0` apaga Claude aunque haya clave. |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Modelo para ese paso. |

## Datos

En `data/` (no se sube al repo):

```
fuentes/<id>.json      checkpoint y números por grupo origen
pendientes.json        álbumes esperando tema o reintento
enviados.json          álbumes ya publicados (evita duplicados)
aprendido.json         artista -> tema y género -> tema (/mapear)
cache_busquedas.json   resultados de MusicBrainz/Deezer/iTunes (géneros y portadas)
portadas.json          de qué lado del ZIP pone la portada cada grupo
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
covers.py         verificar portadas (caption y huella visual)
media.py          descarga, ZIP y subida (de rateroBot)
fast_upload.py / fast_download.py   conexiones en paralelo (de rateroBot)
album_name.py     nombre del ZIP (de rateroBot)
store.py          estado en JSON
```
