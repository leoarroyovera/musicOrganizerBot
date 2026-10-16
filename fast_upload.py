"""Subida paralela real, con multiples conexiones TCP al mismo datacenter.

client.send_file() de Telethon sube por upload_file(), que manda una parte a
la vez y espera el ACK antes de la siguiente (ver telethon/client/uploads.py).
Esto abre N conexiones TCP reales al datacenter de la sesion (reusando la
misma auth_key, sin necesitar ExportAuthorizationRequest: ese mecanismo es
solo para DCs distintos al de la sesion actual). Adaptado de FastTelethon
(https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6, a su vez
tomado de mautrix-telegram), la solucion que referencia el propio mantenedor
de Telethon para este caso (ver issue LonamiWebs/Telethon#1170).
"""
import asyncio
import hashlib
from pathlib import Path

from telethon import helpers, utils
from telethon.network import MTProtoSender
from telethon.tl import custom, types
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest

CONNECTIONS = 8


class _UploadSender:
    """Sube las partes index, index+stride, index+2*stride... por una
    conexion propia. next() NO es reentrante: si se le llama de nuevo antes
    de que la anterior termine, self.previous hace de freno (espera esa
    tarea antes de lanzar la nueva), asi nunca hay dos _next() en vuelo a
    la vez para el mismo sender.
    """

    def __init__(self, client, sender, file_id, part_count, big, index, stride, loop):
        self.client = client
        self.sender = sender
        if big:
            self.request = SaveBigFilePartRequest(file_id, index, part_count, b"")
        else:
            self.request = SaveFilePartRequest(file_id, index, b"")
        self.stride = stride
        self.previous = None
        self.loop = loop

    async def next(self, data):
        if self.previous:
            await self.previous
        self.previous = self.loop.create_task(self._next(data))

    async def _next(self, data):
        self.request.bytes = data
        await self.client._call(self.sender, self.request)
        self.request.file_part += self.stride

    async def disconnect(self):
        if self.previous:
            await self.previous
        return await self.sender.disconnect()


class ParallelUploader:
    def __init__(self, client, connections=CONNECTIONS):
        self.client = client
        self.loop = client.loop
        self.dc_id = client.session.dc_id
        # Reusar la auth_key de la sesion: pedir una exportada para el
        # mismo DC al que ya se esta conectado falla con DcIdInvalidError.
        self.auth_key = client.session.auth_key
        self.connections = connections
        self.senders = None

    async def _create_sender(self):
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(self.client._connection(
            dc.ip_address, dc.port, dc.id,
            loggers=self.client._log, proxy=self.client._proxy))
        return sender

    async def _create_upload_sender(self, file_id, part_count, big, index):
        return _UploadSender(self.client, await self._create_sender(), file_id,
                             part_count, big, index, self.connections, self.loop)

    async def _init(self, file_id, part_count, big):
        self.senders = [
            await self._create_upload_sender(file_id, part_count, big, 0),
            *await asyncio.gather(*[
                self._create_upload_sender(file_id, part_count, big, i)
                for i in range(1, self.connections)
            ])
        ]

    async def _upload_round(self, parts):
        """Una parte por sender: next() en si retorna rapido (solo espera
        la ronda anterior de ESE sender), asi que el gather de esta linea
        no bloquea hasta que las respuestas de red lleguen, sino que las
        deja corriendo en background mientras arranca la ronda siguiente
        -- eso es lo que logra que varias partes viajen en paralelo."""
        await asyncio.gather(*[
            self.senders[i].next(part) for i, part in enumerate(parts)
        ])

    async def _finish(self):
        await asyncio.gather(*(s.disconnect() for s in self.senders))

    async def _abort(self):
        """Corte por error o cancelacion (p.ej. el usuario detuvo el envio):
        se descartan las partes en vuelo y se cierran las conexiones igual."""
        for s in self.senders or []:
            if s.previous:
                s.previous.cancel()
        await asyncio.gather(*(s.sender.disconnect() for s in self.senders or []),
                             return_exceptions=True)

    async def upload(self, path: Path, progress_callback=None):
        file_size = path.stat().st_size
        part_size = int(utils.get_appropriated_part_size(file_size) * 1024)
        part_count = (file_size + part_size - 1) // part_size
        is_big = file_size > 10 * 1024 * 1024
        file_id = helpers.generate_random_long()
        self.connections = min(self.connections, part_count) or 1

        await self._init(file_id, part_count, is_big)

        hash_md5 = hashlib.md5()
        try:
            await self._upload_parts(path, part_size, is_big, file_size, hash_md5, progress_callback)
        except BaseException:
            await self._abort()
            raise
        await self._finish()

        if is_big:
            return types.InputFileBig(file_id, part_count, path.name)
        return custom.InputSizedFile(
            file_id, part_count, path.name, md5=hash_md5, size=file_size)

    async def _upload_parts(self, path, part_size, is_big, file_size, hash_md5, progress_callback):
        done = 0
        with open(path, "rb") as f:
            while True:
                round_parts = []
                for _ in range(self.connections):
                    data = f.read(part_size)
                    if not data:
                        break
                    if not is_big:
                        hash_md5.update(data)
                    round_parts.append(data)
                if not round_parts:
                    break
                await self._upload_round(round_parts)
                done += sum(len(p) for p in round_parts)
                if progress_callback:
                    await helpers._maybe_await(progress_callback(done, file_size))


async def upload_file_fast(client, path: Path, progress_callback=None,
                           connections=CONNECTIONS):
    """Sube `path` usando `connections` conexiones TCP paralelas al DC de la
    sesion. Devuelve un InputFileBig/InputSizedFile usable en
    client.send_file(file=...)."""
    uploader = ParallelUploader(client, connections=connections)
    return await uploader.upload(path, progress_callback=progress_callback)
