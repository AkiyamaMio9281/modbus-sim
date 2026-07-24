"""A minimal async Modbus TCP client, built on the same :mod:`modbus_sim.frame` codec.

Reusing the codec for both the server and the client is the point: the wire format is
defined once. The client handles transaction-id generation/echo checking and response
framing, and raises :class:`ModbusExceptionError` when the server returns an exception PDU.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools

from modbus_sim import frame


class ModbusExceptionError(Exception):
    """Raised when the server returns a Modbus exception response."""

    def __init__(self, response: frame.ExceptionResponse):
        self.function_code = response.function_code
        self.exception_code = response.exception_code
        super().__init__(
            f"function 0x{response.function_code:02x} -> exception 0x{response.exception_code:02x} "
            f"({_EXCEPTION_NAMES.get(response.exception_code, 'unknown')})"
        )


_EXCEPTION_NAMES = {
    frame.ILLEGAL_FUNCTION: "illegal function",
    frame.ILLEGAL_DATA_ADDRESS: "illegal data address",
    frame.ILLEGAL_DATA_VALUE: "illegal data value",
    frame.SERVER_DEVICE_FAILURE: "server device failure",
}


class ModbusClient:
    """An async context-managed Modbus TCP client for a single server/unit."""

    def __init__(
        self, host: str = "127.0.0.1", port: int = 5020, *, unit: int = 1, timeout: float = 5.0
    ):
        self.host = host
        self.port = port
        self.unit = unit
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tid = itertools.cycle(range(1, 0x10000))

    async def __aenter__(self) -> ModbusClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout
        )

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()
            self._reader = self._writer = None

    async def _transact(
        self, request: frame.Request, *, bit_count: int | None = None
    ) -> frame.Response:
        if self._writer is None or self._reader is None:
            raise RuntimeError("client is not connected")
        transaction_id = next(self._tid)
        self._writer.write(
            frame.build_adu(transaction_id, self.unit, frame.encode_request_pdu(request))
        )
        await self._writer.drain()

        head = await asyncio.wait_for(self._reader.readexactly(frame.MBAP_HEADER_LEN), self.timeout)
        header = frame.decode_mbap(head)
        if header.transaction_id != transaction_id:
            raise OSError(
                f"transaction id mismatch: sent {transaction_id}, got {header.transaction_id}"
            )
        pdu = await asyncio.wait_for(self._reader.readexactly(header.length - 1), self.timeout)
        response = frame.decode_response_pdu(pdu, bit_count=bit_count)
        if isinstance(response, frame.ExceptionResponse):
            raise ModbusExceptionError(response)
        return response

    # -- reads -------------------------------------------------------------------------

    async def read_coils(self, address: int, count: int) -> list[bool]:
        resp = await self._transact(frame.ReadCoilsRequest(address, count), bit_count=count)
        return list(resp.bits)

    async def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        resp = await self._transact(
            frame.ReadDiscreteInputsRequest(address, count), bit_count=count
        )
        return list(resp.bits)

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        resp = await self._transact(frame.ReadHoldingRegistersRequest(address, count))
        return list(resp.registers)

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        resp = await self._transact(frame.ReadInputRegistersRequest(address, count))
        return list(resp.registers)

    # -- writes ------------------------------------------------------------------------

    async def write_coil(self, address: int, value: bool) -> None:
        await self._transact(frame.WriteSingleCoilRequest(address, bool(value)))

    async def write_register(self, address: int, value: int) -> None:
        await self._transact(frame.WriteSingleRegisterRequest(address, value))

    async def write_coils(self, address: int, values: list[bool]) -> None:
        await self._transact(
            frame.WriteMultipleCoilsRequest(address, tuple(bool(v) for v in values))
        )

    async def write_registers(self, address: int, values: list[int]) -> None:
        await self._transact(frame.WriteMultipleRegistersRequest(address, tuple(values)))
