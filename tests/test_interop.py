"""Interoperability tests: the industry-standard ``pymodbus`` client against our server.

This is the headline correctness gate (SPEC §9): a battle-tested third-party implementation
must round-trip all eight function codes and correctly interpret our exception responses.

``pymodbus`` is the *only* place a Modbus library is allowed — never in the core.

Note on the qty=0 case: pymodbus validates quantities client-side and refuses to *encode*
``count=0`` at all, so that request cannot originate from pymodbus. We therefore trigger it
at the wire level and decode the server's reply with pymodbus's own PDU decoder, which still
proves a standard implementation reads our ``IllegalDataValue`` exception frame.
"""

import asyncio
import contextlib

import pytest

pytest.importorskip("pymodbus")

from pymodbus.client import AsyncModbusTcpClient  # noqa: E402
from pymodbus.pdu import DecodePDU  # noqa: E402
from pymodbus.pdu.mei_message import ReadDeviceInformationRequest  # noqa: E402

from modbus_sim import frame as f  # noqa: E402
from modbus_sim.datastore import DataStore, build_device  # noqa: E402
from modbus_sim.server import ModbusServer  # noqa: E402


def demo_store():
    store = DataStore(
        [build_device(1, coils=32, discrete_inputs=32, holding_registers=32, input_registers=32)]
    )
    device = store.get(1)
    device.coils.write(0, [True, False, True, True, False])
    device.discrete_inputs.write(0, [False, True, True, False, True])
    device.holding_registers.write(0, [10, 20, 30, 40])
    device.input_registers.write(0, [111, 222, 333, 444])
    return store


@contextlib.asynccontextmanager
async def running_server():
    server = ModbusServer(demo_store(), host="127.0.0.1", port=0)
    await server.start()
    task = asyncio.create_task(server.serve_forever())
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@contextlib.asynccontextmanager
async def pymodbus_client(port):
    client = AsyncModbusTcpClient("127.0.0.1", port=port)
    await client.connect()
    assert client.connected
    try:
        yield client
    finally:
        client.close()


# --------------------------------------------------------------------------------------
# Eight function codes, each round-tripped through pymodbus
# --------------------------------------------------------------------------------------


async def test_interop_read_holding_registers():
    async with running_server() as port, pymodbus_client(port) as client:
        rr = await client.read_holding_registers(0, count=4, device_id=1)
        assert not rr.isError()
        assert rr.registers == [10, 20, 30, 40]


async def test_interop_read_input_registers():
    async with running_server() as port, pymodbus_client(port) as client:
        rr = await client.read_input_registers(0, count=4, device_id=1)
        assert rr.registers == [111, 222, 333, 444]


async def test_interop_read_coils():
    async with running_server() as port, pymodbus_client(port) as client:
        rr = await client.read_coils(0, count=5, device_id=1)
        assert rr.bits[:5] == [True, False, True, True, False]


async def test_interop_read_discrete_inputs():
    async with running_server() as port, pymodbus_client(port) as client:
        rr = await client.read_discrete_inputs(0, count=5, device_id=1)
        assert rr.bits[:5] == [False, True, True, False, True]


async def test_interop_write_single_register():
    async with running_server() as port, pymodbus_client(port) as client:
        assert not (await client.write_register(1, 4242, device_id=1)).isError()
        rr = await client.read_holding_registers(1, count=1, device_id=1)
        assert rr.registers == [4242]


async def test_interop_write_single_coil():
    async with running_server() as port, pymodbus_client(port) as client:
        assert not (await client.write_coil(6, True, device_id=1)).isError()
        rr = await client.read_coils(6, count=1, device_id=1)
        assert rr.bits[0] is True


async def test_interop_write_multiple_registers():
    async with running_server() as port, pymodbus_client(port) as client:
        assert not (await client.write_registers(10, [7, 8, 9], device_id=1)).isError()
        rr = await client.read_holding_registers(10, count=3, device_id=1)
        assert rr.registers == [7, 8, 9]


async def test_interop_write_multiple_coils():
    async with running_server() as port, pymodbus_client(port) as client:
        assert not (await client.write_coils(8, [True, False, True, True], device_id=1)).isError()
        rr = await client.read_coils(8, count=4, device_id=1)
        assert rr.bits[:4] == [True, False, True, True]


# --------------------------------------------------------------------------------------
# Three exception cases (SPEC §9)
# --------------------------------------------------------------------------------------


async def test_interop_illegal_data_address():
    async with running_server() as port, pymodbus_client(port) as client:
        rr = await client.read_holding_registers(9999, count=1, device_id=1)
        assert rr.isError()
        assert rr.exception_code == f.ILLEGAL_DATA_ADDRESS


async def test_interop_illegal_function():
    # fc 0x2B (Read Device Identification) is not implemented -> exception 01.
    async with running_server() as port, pymodbus_client(port) as client:
        rr = await client.execute(False, ReadDeviceInformationRequest(dev_id=1))
        assert rr.isError()
        assert rr.exception_code == f.ILLEGAL_FUNCTION


async def test_interop_illegal_data_value_qty_zero():
    async with running_server() as port:
        # pymodbus won't encode count=0, so build the request at the wire level...
        pdu = f.encode_request_pdu(f.ReadHoldingRegistersRequest(0, 0))
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f.build_adu(0x55, 1, pdu))
        await writer.drain()
        header = f.decode_mbap(await reader.readexactly(f.MBAP_HEADER_LEN))
        response_pdu = await reader.readexactly(header.length - 1)
        writer.close()
        await writer.wait_closed()

        # ...and decode the reply with pymodbus's own decoder to prove interop.
        decoded = DecodePDU(is_server=False).decode(response_pdu)
        assert decoded.isError()
        assert decoded.exception_code == f.ILLEGAL_DATA_VALUE
