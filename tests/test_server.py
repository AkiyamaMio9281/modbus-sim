"""Tests for stream framing (SPEC §4) and the asyncio server end-to-end."""

import asyncio
import contextlib
import struct

import pytest

from modbus_sim import frame as f
from modbus_sim.datastore import DataStore, build_device
from modbus_sim.server import ModbusServer, ProtocolError, StreamFramer


def make_adu(transaction_id, unit, request):
    return f.build_adu(transaction_id, unit, f.encode_request_pdu(request))


# --------------------------------------------------------------------------------------
# StreamFramer — the pure reassembly unit (half packet / sticky packet / byte-at-a-time)
# --------------------------------------------------------------------------------------


def test_framer_yields_one_complete_frame():
    adu = make_adu(1, 1, f.ReadHoldingRegistersRequest(0, 4))
    framer = StreamFramer()
    frames = list(framer.feed(adu))
    assert len(frames) == 1
    header, got = frames[0]
    assert header.transaction_id == 1
    assert got == adu
    assert framer.pending == 0


def test_framer_byte_at_a_time():
    adu = make_adu(0x2222, 1, f.ReadHoldingRegistersRequest(3, 2))
    framer = StreamFramer()
    produced = []
    for i, byte in enumerate(adu):
        produced.extend(framer.feed(bytes([byte])))
        # Nothing is emitted until the very last byte completes the frame.
        if i < len(adu) - 1:
            assert produced == []
    assert len(produced) == 1
    assert produced[0][1] == adu


def test_framer_three_sticky_frames_in_one_feed():
    a = make_adu(1, 1, f.ReadCoilsRequest(0, 8))
    b = make_adu(2, 1, f.ReadHoldingRegistersRequest(0, 2))
    c = make_adu(3, 1, f.WriteSingleRegisterRequest(1, 9))
    framer = StreamFramer()
    frames = list(framer.feed(a + b + c))
    assert [h.transaction_id for h, _ in frames] == [1, 2, 3]
    assert [adu for _, adu in frames] == [a, b, c]


def test_framer_half_frame_then_rest():
    adu = make_adu(7, 1, f.ReadHoldingRegistersRequest(0, 4))
    framer = StreamFramer()
    assert list(framer.feed(adu[:5])) == []
    assert framer.pending == 5
    frames = list(framer.feed(adu[5:]))
    assert len(frames) == 1
    assert frames[0][1] == adu


def test_framer_frame_then_partial_next():
    a = make_adu(1, 1, f.ReadHoldingRegistersRequest(0, 4))
    b = make_adu(2, 1, f.ReadHoldingRegistersRequest(0, 4))
    framer = StreamFramer()
    frames = list(framer.feed(a + b[:4]))  # one whole frame plus 4 bytes of the next
    assert len(frames) == 1
    assert frames[0][0].transaction_id == 1
    assert framer.pending == 4
    frames = list(framer.feed(b[4:]))
    assert frames[0][0].transaction_id == 2


@pytest.mark.parametrize("length", [0, 1, 261, 0xFFFF])
def test_framer_rejects_out_of_range_length(length):
    raw = struct.pack(">HHHB", 1, 0, length, 1) + b"\x03\x00\x00\x00\x04"
    framer = StreamFramer()
    with pytest.raises(ProtocolError):
        list(framer.feed(raw))


# --------------------------------------------------------------------------------------
# End-to-end server over a real socket
# --------------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def running_server(store, bindings=()):
    server = ModbusServer(store, host="127.0.0.1", port=0, bindings=bindings)
    await server.start()
    task = asyncio.create_task(server.serve_forever())
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def read_adu(reader):
    head = await asyncio.wait_for(reader.readexactly(f.MBAP_HEADER_LEN), 2.0)
    header = f.decode_mbap(head)
    pdu = await asyncio.wait_for(reader.readexactly(header.length - 1), 2.0)
    return header, pdu


def demo_store():
    store = DataStore([build_device(1, coils=16, holding_registers=16, input_registers=16)])
    store.get(1).holding_registers.write(0, [11, 22, 33, 44])
    return store


async def test_read_holding_roundtrip_and_echoes_transaction_id():
    async with running_server(demo_store()) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(make_adu(0x1234, 1, f.ReadHoldingRegistersRequest(0, 4)))
        await writer.drain()
        header, pdu = await read_adu(reader)
        assert header.transaction_id == 0x1234
        assert f.decode_response_pdu(pdu) == f.ReadHoldingRegistersResponse((11, 22, 33, 44))
        writer.close()
        await writer.wait_closed()


async def test_write_single_register_then_read_back():
    async with running_server(demo_store()) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(make_adu(1, 1, f.WriteSingleRegisterRequest(5, 0xABCD)))
        await writer.drain()
        _, pdu = await read_adu(reader)
        assert f.decode_response_pdu(pdu) == f.WriteSingleRegisterResponse(5, 0xABCD)

        writer.write(make_adu(2, 1, f.ReadHoldingRegistersRequest(5, 1)))
        await writer.drain()
        _, pdu = await read_adu(reader)
        assert f.decode_response_pdu(pdu) == f.ReadHoldingRegistersResponse((0xABCD,))
        writer.close()
        await writer.wait_closed()


async def test_out_of_range_read_returns_exception_frame():
    async with running_server(demo_store()) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(make_adu(9, 1, f.ReadHoldingRegistersRequest(100, 4)))
        await writer.drain()
        header, pdu = await read_adu(reader)
        assert header.transaction_id == 9
        assert f.decode_response_pdu(pdu) == f.ExceptionResponse(
            f.READ_HOLDING_REGISTERS, f.ILLEGAL_DATA_ADDRESS
        )
        writer.close()
        await writer.wait_closed()


async def test_sticky_requests_get_two_responses():
    async with running_server(demo_store()) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        both = make_adu(1, 1, f.ReadHoldingRegistersRequest(0, 1)) + make_adu(
            2, 1, f.ReadHoldingRegistersRequest(1, 1)
        )
        writer.write(both)
        await writer.drain()
        h1, p1 = await read_adu(reader)
        h2, p2 = await read_adu(reader)
        assert (h1.transaction_id, h2.transaction_id) == (1, 2)
        assert f.decode_response_pdu(p1) == f.ReadHoldingRegistersResponse((11,))
        assert f.decode_response_pdu(p2) == f.ReadHoldingRegistersResponse((22,))
        writer.close()
        await writer.wait_closed()


async def test_unknown_unit_is_dropped_without_closing_connection():
    async with running_server(demo_store()) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # Frame for a non-existent unit: no reply expected...
        writer.write(make_adu(1, 99, f.ReadHoldingRegistersRequest(0, 1)))
        # ...followed by a valid frame on the same connection.
        writer.write(make_adu(2, 1, f.ReadHoldingRegistersRequest(0, 1)))
        await writer.drain()
        header, pdu = await read_adu(reader)
        assert header.transaction_id == 2  # the dropped frame produced no response
        assert f.decode_response_pdu(pdu) == f.ReadHoldingRegistersResponse((11,))
        writer.close()
        await writer.wait_closed()


async def test_nonzero_protocol_id_is_dropped():
    async with running_server(demo_store()) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        bad = struct.pack(">HHHB", 1, 7, 6, 1) + f.encode_request_pdu(
            f.ReadHoldingRegistersRequest(0, 1)
        )
        writer.write(bad)
        writer.write(make_adu(2, 1, f.ReadHoldingRegistersRequest(0, 1)))
        await writer.drain()
        header, pdu = await read_adu(reader)
        assert header.transaction_id == 2
        writer.close()
        await writer.wait_closed()
