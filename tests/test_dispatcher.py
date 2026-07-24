"""Unit tests for function-code dispatch and exception-code generation.

These tests never open a socket: they drive :func:`process_pdu` (bytes in, bytes out) and
:func:`handle_request` (dataclass in, dataclass out) directly against an in-memory device.
"""

import pytest

from modbus_sim import frame as f
from modbus_sim.datastore import MemoryBlock, build_device
from modbus_sim.dispatcher import handle_request, process_pdu


def make_device():
    """A device with a little pre-loaded data in every region."""
    device = build_device(
        1, "dut", coils=16, discrete_inputs=16, holding_registers=16, input_registers=16
    )
    device.coils.write(0, [True, False, True, True, False])
    device.discrete_inputs.write(0, [False, True, True, False, True])
    device.holding_registers.write(0, [10, 20, 30, 40])
    device.input_registers.write(0, [111, 222, 333, 444])
    return device


def roundtrip(device, request, *, bit_count=None):
    """Encode ``request``, run it through the dispatcher, and decode the response."""
    pdu = f.encode_request_pdu(request)
    return f.decode_response_pdu(process_pdu(device, pdu), bit_count=bit_count)


def exception_code(device, request_or_pdu) -> int:
    pdu = (
        request_or_pdu
        if isinstance(request_or_pdu, bytes)
        else f.encode_request_pdu(request_or_pdu)
    )
    response = f.decode_response_pdu(process_pdu(device, pdu))
    assert isinstance(response, f.ExceptionResponse), response
    return response.exception_code


# --------------------------------------------------------------------------------------
# Happy path — every function code
# --------------------------------------------------------------------------------------


def test_read_coils():
    resp = roundtrip(make_device(), f.ReadCoilsRequest(0, 5), bit_count=5)
    assert resp == f.ReadCoilsResponse((True, False, True, True, False))


def test_read_discrete_inputs():
    resp = roundtrip(make_device(), f.ReadDiscreteInputsRequest(0, 5), bit_count=5)
    assert resp == f.ReadDiscreteInputsResponse((False, True, True, False, True))


def test_read_holding_registers():
    resp = roundtrip(make_device(), f.ReadHoldingRegistersRequest(0, 4))
    assert resp == f.ReadHoldingRegistersResponse((10, 20, 30, 40))


def test_read_input_registers():
    resp = roundtrip(make_device(), f.ReadInputRegistersRequest(0, 4))
    assert resp == f.ReadInputRegistersResponse((111, 222, 333, 444))


def test_write_single_coil_mutates_and_echoes():
    device = make_device()
    resp = roundtrip(device, f.WriteSingleCoilRequest(1, True))
    assert resp == f.WriteSingleCoilResponse(1, True)
    assert device.coils.read(1, 1) == [True]


def test_write_single_register_mutates_and_echoes():
    device = make_device()
    resp = roundtrip(device, f.WriteSingleRegisterRequest(2, 0xBEEF))
    assert resp == f.WriteSingleRegisterResponse(2, 0xBEEF)
    assert device.holding_registers.read(2, 1) == [0xBEEF]


def test_write_multiple_coils_mutates_and_reports_quantity():
    device = make_device()
    resp = roundtrip(device, f.WriteMultipleCoilsRequest(4, (True, True, False, True)))
    assert resp == f.WriteMultipleCoilsResponse(4, 4)
    assert device.coils.read(4, 4) == [True, True, False, True]


def test_write_multiple_registers_mutates_and_reports_quantity():
    device = make_device()
    resp = roundtrip(device, f.WriteMultipleRegistersRequest(5, (1, 2, 3)))
    assert resp == f.WriteMultipleRegistersResponse(5, 3)
    assert device.holding_registers.read(5, 3) == [1, 2, 3]


def test_write_then_read_back_through_dispatcher():
    device = make_device()
    roundtrip(device, f.WriteMultipleRegistersRequest(8, (0xAAAA, 0xBBBB)))
    resp = roundtrip(device, f.ReadHoldingRegistersRequest(8, 2))
    assert resp == f.ReadHoldingRegistersResponse((0xAAAA, 0xBBBB))


# --------------------------------------------------------------------------------------
# Exception code 01 — illegal function
# --------------------------------------------------------------------------------------


def test_unsupported_function_code_returns_01():
    # 0x2B (Encapsulated Interface Transport) is not implemented.
    assert exception_code(make_device(), b"\x2b\x00\x0e\x01") == f.ILLEGAL_FUNCTION


def test_exception_response_echoes_function_code_with_high_bit():
    device = make_device()
    pdu = f.encode_request_pdu(f.ReadHoldingRegistersRequest(999, 1))  # out of range
    response_pdu = process_pdu(device, pdu)
    assert response_pdu[0] == f.READ_HOLDING_REGISTERS | 0x80


# --------------------------------------------------------------------------------------
# Exception code 03 — illegal data value (quantity, coil value, byte count)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_obj",
    [
        f.ReadCoilsRequest(0, 0),
        f.ReadCoilsRequest(0, f.MAX_READ_BITS + 1),
        f.ReadDiscreteInputsRequest(0, 0),
        f.ReadHoldingRegistersRequest(0, 0),
        f.ReadHoldingRegistersRequest(0, f.MAX_READ_REGISTERS + 1),
        f.ReadInputRegistersRequest(0, f.MAX_READ_REGISTERS + 1),
    ],
)
def test_illegal_read_quantity_returns_03(request_obj):
    assert exception_code(make_device(), request_obj) == f.ILLEGAL_DATA_VALUE


def test_write_too_many_registers_returns_03():
    request = f.WriteMultipleRegistersRequest(0, tuple(range(f.MAX_WRITE_REGISTERS + 1)))
    assert exception_code(make_device(), request) == f.ILLEGAL_DATA_VALUE


def test_write_too_many_coils_returns_03():
    request = f.WriteMultipleCoilsRequest(0, tuple([True] * (f.MAX_WRITE_COILS + 1)))
    assert exception_code(make_device(), request) == f.ILLEGAL_DATA_VALUE


def test_bad_coil_value_returns_03():
    assert exception_code(make_device(), bytes.fromhex("0500011234")) == f.ILLEGAL_DATA_VALUE


def test_byte_count_mismatch_returns_03():
    # Write Multiple Registers, qty=2 (needs byte_count 4) but header claims 2.
    assert exception_code(make_device(), bytes.fromhex("1000000002020000")) == f.ILLEGAL_DATA_VALUE


# --------------------------------------------------------------------------------------
# Exception code 02 — illegal data address
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_obj",
    [
        f.ReadCoilsRequest(14, 4),  # 14..17, size 16
        f.ReadHoldingRegistersRequest(16, 1),
        f.ReadInputRegistersRequest(100, 1),
        f.WriteSingleCoilRequest(16, True),
        f.WriteSingleRegisterRequest(20, 1),
        f.WriteMultipleRegistersRequest(15, (1, 2)),
        f.WriteMultipleCoilsRequest(15, (True, True)),
    ],
)
def test_out_of_range_address_returns_02(request_obj):
    assert exception_code(make_device(), request_obj) == f.ILLEGAL_DATA_ADDRESS


# --------------------------------------------------------------------------------------
# Exception priority: illegal quantity (03) beats illegal address (02)
# --------------------------------------------------------------------------------------


def test_illegal_quantity_wins_over_illegal_address():
    # Address 9999 is out of range AND quantity exceeds the read limit; 03 must win.
    request = f.ReadHoldingRegistersRequest(9999, f.MAX_READ_REGISTERS + 1)
    assert exception_code(make_device(), request) == f.ILLEGAL_DATA_VALUE

    # Quantity zero with an out-of-range address also yields 03, not 02.
    request = f.ReadCoilsRequest(9999, 0)
    assert exception_code(make_device(), request) == f.ILLEGAL_DATA_VALUE


# --------------------------------------------------------------------------------------
# Exception code 04 — server device failure (unexpected internal error)
# --------------------------------------------------------------------------------------


def test_internal_error_maps_to_04(monkeypatch):
    device = make_device()

    def boom(self, address, count):
        raise RuntimeError("simulated internal failure")

    monkeypatch.setattr(MemoryBlock, "read", boom)
    pdu = f.encode_request_pdu(f.ReadHoldingRegistersRequest(0, 2))
    response = f.decode_response_pdu(process_pdu(device, pdu))
    assert isinstance(response, f.ExceptionResponse)
    assert response.exception_code == f.SERVER_DEVICE_FAILURE


# --------------------------------------------------------------------------------------
# handle_request is usable directly (raises, does not wrap)
# --------------------------------------------------------------------------------------


def test_handle_request_raises_on_illegal_quantity():
    with pytest.raises(f.IllegalDataValue):
        handle_request(make_device(), f.ReadHoldingRegistersRequest(0, 0))


def test_handle_request_raises_on_illegal_address():
    with pytest.raises(f.IllegalDataAddress):
        handle_request(make_device(), f.ReadHoldingRegistersRequest(16, 1))
