"""Asyncio Modbus TCP server: stream framing, dispatch, and background generators.

The tricky part of a byte-stream protocol is reassembly. :class:`StreamFramer` isolates that
into a pure, socket-free object (SPEC §4): feed it whatever bytes arrive and it yields
complete ADU frames, buffering partial ones. That is what makes the half-packet / sticky-
packet / byte-at-a-time behaviour unit-testable without a server.

:class:`ModbusServer` wires the framer to the dispatcher, serializes each device's access
with its lock, echoes the transaction id, and runs a once-per-second task that refreshes all
generator-backed registers so the devices look alive.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterable, Iterator

from modbus_sim import frame
from modbus_sim.datastore import DataStore
from modbus_sim.dispatcher import process_pdu
from modbus_sim.generators import GeneratorBinding

logger = logging.getLogger("modbus_sim.server")

#: Background generator refresh period (SPEC §5).
GENERATOR_INTERVAL_S = 1.0
#: Default listen port (502 needs root/admin; 5020 is used for development).
DEFAULT_PORT = 5020
_READ_CHUNK = 4096

# Function codes whose 4th/5th PDU bytes are a quantity vs a single value, for log framing.
_QTY_FCS = frozenset(
    {
        frame.READ_COILS,
        frame.READ_DISCRETE_INPUTS,
        frame.READ_HOLDING_REGISTERS,
        frame.READ_INPUT_REGISTERS,
        frame.WRITE_MULTIPLE_COILS,
        frame.WRITE_MULTIPLE_REGISTERS,
    }
)


class ProtocolError(Exception):
    """A framing-level error that requires closing the connection (SPEC §4)."""


class StreamFramer:
    """Reassembles a TCP byte stream into complete Modbus ADU frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[tuple[frame.MBAPHeader, bytes]]:
        """Add bytes to the buffer and yield every complete ``(header, frame)`` now available.

        Raises :class:`ProtocolError` if a frame declares an out-of-range length field.
        """
        self._buffer.extend(data)
        while (item := self._next_frame()) is not None:
            yield item

    def _next_frame(self) -> tuple[frame.MBAPHeader, bytes] | None:
        if len(self._buffer) < frame.MBAP_HEADER_LEN:
            return None  # not even a full MBAP header yet
        header = frame.decode_mbap(self._buffer)
        if not frame.MIN_LENGTH_FIELD <= header.length <= frame.MAX_LENGTH_FIELD:
            raise ProtocolError(f"invalid MBAP length field {header.length}")
        total = 6 + header.length  # 6 bytes before the length field's own coverage + length
        if len(self._buffer) < total:
            return None  # half frame; wait for more
        adu = bytes(self._buffer[:total])
        del self._buffer[:total]
        return header, adu

    @property
    def pending(self) -> int:
        """Number of buffered bytes not yet forming a complete frame."""
        return len(self._buffer)


def _fmt_peer(peer: object) -> str:
    if isinstance(peer, tuple) and len(peer) >= 2:
        return f"{peer[0]}:{peer[1]}"
    return str(peer)


class ModbusServer:
    """An asyncio Modbus TCP server bound to a :class:`DataStore`."""

    def __init__(
        self,
        store: DataStore,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        bindings: Iterable[GeneratorBinding] = (),
    ) -> None:
        self.store = store
        self.host = host
        self.port = port
        self.bindings = list(bindings)
        self._server: asyncio.Server | None = None

    async def start(self) -> asyncio.Server:
        """Bind the listening socket without blocking (useful for tests)."""
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        for sock in self._server.sockets:
            logger.info("listening on %s", sock.getsockname())
        return self._server

    @property
    def sockets(self) -> tuple:
        return tuple(self._server.sockets) if self._server else ()

    async def serve_forever(self) -> None:
        """Start listening and serve until cancelled, running generators alongside."""
        if self._server is None:
            await self.start()
        assert self._server is not None
        gen_task = (
            asyncio.create_task(self._run_generators(), name="generators")
            if self.bindings
            else None
        )
        try:
            async with self._server:
                await self._server.serve_forever()
        finally:
            if gen_task is not None:
                gen_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await gen_task

    async def _run_generators(self) -> None:
        start = time.monotonic()
        while True:
            await asyncio.sleep(GENERATOR_INTERVAL_S)
            elapsed = time.monotonic() - start
            for binding in self.bindings:
                async with binding.device.lock:
                    binding.update(elapsed)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        framer = StreamFramer()
        logger.info("connect peer=%s", _fmt_peer(peer))
        try:
            while True:
                data = await reader.read(_READ_CHUNK)
                if not data:
                    break  # peer closed
                for header, adu in framer.feed(data):
                    await self._process_frame(header, adu, writer, peer)
        except ProtocolError as exc:
            logger.warning("protocol error peer=%s: %s (closing)", _fmt_peer(peer), exc)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await writer.wait_closed()
            logger.info("disconnect peer=%s", _fmt_peer(peer))

    async def _process_frame(
        self, header: frame.MBAPHeader, adu: bytes, writer: asyncio.StreamWriter, peer: object
    ) -> None:
        started = time.perf_counter()
        if header.protocol_id != frame.PROTOCOL_ID:
            logger.warning("drop peer=%s protocol_id=%d", _fmt_peer(peer), header.protocol_id)
            return
        device = self.store.get(header.unit_id)
        if device is None:
            logger.warning("drop peer=%s unknown unit=%d", _fmt_peer(peer), header.unit_id)
            return
        pdu = adu[frame.MBAP_HEADER_LEN :]
        async with device.lock:
            response_pdu = process_pdu(device, pdu)
        response_adu = frame.build_adu(header.transaction_id, header.unit_id, response_pdu)
        writer.write(response_adu)
        await writer.drain()
        _log_request(peer, header, pdu, response_pdu, started)


def _log_request(
    peer: object, header: frame.MBAPHeader, pdu: bytes, response_pdu: bytes, started: float
) -> None:
    dur_ms = (time.perf_counter() - started) * 1000
    fc = pdu[0] if pdu else 0
    fields = f"peer={_fmt_peer(peer)} unit={header.unit_id} fc=0x{fc:02x}"
    if len(pdu) >= 3:
        fields += f" addr={int.from_bytes(pdu[1:3], 'big')}"
    if len(pdu) >= 5:
        label = "qty" if fc in _QTY_FCS else "val"
        fields += f" {label}={int.from_bytes(pdu[3:5], 'big')}"
    if response_pdu and response_pdu[0] & frame.EXCEPTION_MASK:
        logger.warning("req %s -> exc=%02x", fields, response_pdu[1])
    else:
        logger.info("req %s -> ok bytes=%d dur_ms=%.1f", fields, len(response_pdu), dur_ms)


def configure_logging(level: str | int = "INFO") -> None:
    """Install a single-line UTC log handler on the ``modbus_sim`` logger tree (SPEC §7)."""
    logging.addLevelName(logging.WARNING, "WARN")  # match the SPEC §7 sample exactly
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    root = logging.getLogger("modbus_sim")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
