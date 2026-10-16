"""Descarga paralela real, con multiples conexiones TCP al datacenter donde
esta guardado el archivo. Contraparte de fast_upload.py: client.download_media()
pide una parte a la vez y espera la respuesta antes de la siguiente.

Cada conexion pide partes distintas (upload.getFile con offset propio) y las
escribe directo en su posicion del archivo, asi no hace falta ordenarlas en
memoria. Si el archivo vive en otro DC distinto al de la sesion, se exporta
la autorizacion a ese DC (igual que hace Telethon con sus "borrowed senders"
y FastTelethon, de donde se adapta esto).
"""
import asyncio
import copy

from telethon import helpers, utils
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest
from telethon.tl.functions.upload import GetFileRequest

CONNECTIONS = 8
# Maximo que acepta upload.getFile por pedido; offset y limit deben ser
# multiplos de 4KB y 1MB divisible por limit.
PART_SIZE = 1024 * 1024


class ParallelDownloader:
    def __init__(self, client, dc_id, connections=CONNECTIONS):
        self.client = client
        self.dc_id = dc_id or client.session.dc_id
        self.connections = connections
        # Mismo DC que la sesion: se reusa su auth_key. Otro DC: se exporta
        # la autorizacion al crear la primera conexion y las demas reusan esa key.
        self.auth_key = client.session.auth_key if self.dc_id == client.session.dc_id else None
        self.senders = []

    async def _create_sender(self):
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(self.client._connection(
            dc.ip_address, dc.port, dc.id,
            loggers=self.client._log, proxy=self.client._proxy))
        if not self.auth_key:
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            # Copia: no tocar el _init_request que usa el propio cliente.
            init = copy.copy(self.client._init_request)
            init.query = ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
            await sender.send(InvokeWithLayerRequest(LAYER, init))
            self.auth_key = sender.auth_key
        return sender

    async def _init(self, n):
        # La primera sola (puede tener que exportar la autorizacion), el resto en paralelo.
        self.senders = [await self._create_sender()]
        self.senders += await asyncio.gather(*[self._create_sender() for _ in range(n - 1)])

    async def download(self, location, size, path, progress_callback=None):
        part_count = (size + PART_SIZE - 1) // PART_SIZE
        n = max(1, min(self.connections, part_count))
        pending = asyncio.Queue()
        for i in range(part_count):
            pending.put_nowait(i)
        done = 0

        async def worker(sender, f):
            nonlocal done
            while True:
                try:
                    index = pending.get_nowait()
                except asyncio.QueueEmpty:
                    return
                result = await self.client._call(
                    sender, GetFileRequest(location, offset=index * PART_SIZE, limit=PART_SIZE))
                f.seek(index * PART_SIZE)
                f.write(result.bytes)
                done += len(result.bytes)
                if progress_callback:
                    await helpers._maybe_await(progress_callback(min(done, size), size))

        try:
            await self._init(n)
            with open(path, "wb") as f:
                tasks = [asyncio.create_task(worker(s, f)) for s in self.senders]
                try:
                    await asyncio.gather(*tasks)
                except BaseException:
                    for t in tasks:
                        t.cancel()
                    raise
        finally:
            await asyncio.gather(*(s.disconnect() for s in self.senders), return_exceptions=True)
        return path


async def download_document_fast(client, msg, path, progress_callback=None,
                                 connections=CONNECTIONS):
    """Descarga el documento de `msg` (video, archivo, audio…) a `path`
    usando `connections` conexiones paralelas. No sirve para fotos."""
    dc_id, location = utils.get_input_location(msg.media)
    downloader = ParallelDownloader(client, dc_id, connections=connections)
    return await downloader.download(location, msg.document.size, path,
                                     progress_callback=progress_callback)
