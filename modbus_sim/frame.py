"""Modbus TCP frame codec — MBAP header + PDU encode/decode.

This module is the protocol foundation. Every function here is **pure**: bytes and
dataclasses in, bytes and dataclasses out, with zero IO. That property is what lets the
overwhelming majority of the test-suite exercise the protocol without ever opening a
socket, and it keeps the wire format (byte order, field widths, bit packing) in one
auditable place.

Layering
--------
* An **ADU** (Application Data Unit) on the wire is ``MBAP header (7 bytes) + PDU``.
* A **PDU** (Protocol Data Unit) is ``function code (1 byte) + function payload``.

`decode_request_pdu` / `decode_response_pdu` raise :class:`ModbusError` subclasses when a
PDU is structurally invalid in a way that maps onto a Modbus exception code (unknown
function code, byte-count/quantity mismatch, illegal coil value). Semantic validation
that needs protocol limits or a device map (quantity range, address range) is deliberately
left to :mod:`modbus_sim.dispatcher`, so the exception-priority ordering lives in one place.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# --------------------------------------------------------------------------------------
# Protocol constants (single source of truth for the wire format)
# --------------------------------------------------------------------------------------

#: Length of the MBAP header in bytes: transaction id (2) + protocol id (2) + length (2)
#: + unit id (1).
MBAP_HEADER_LEN = 7

#: Protocol identifier for Modbus. Any other value means "not Modbus" and the frame is
#: dropped by the server (see SPEC §1).
PROTOCOL_ID = 0

#: Maximum size of a complete ADU on the wire (Modbus TCP caps the PDU at 253 bytes).
MAX_ADU_LEN = 260
#: Maximum size of a PDU (function code + payload).
MAX_PDU_LEN = 253
#: Valid range for the MBAP ``length`` field: at least a unit id + a 1-byte function code,
#: at most a full ADU minus the 6 bytes preceding the length field (see SPEC §4).
MIN_LENGTH_FIELD = 2
MAX_LENGTH_FIELD = 260

# Function codes ------------------------------------------------------------------------
READ_COILS = 0x01
READ_DISCRETE_INPUTS = 0x02
READ_HOLDING_REGISTERS = 0x03
READ_INPUT_REGISTERS = 0x04
WRITE_SINGLE_COIL = 0x05
WRITE_SINGLE_REGISTER = 0x06
WRITE_MULTIPLE_COILS = 0x0F
WRITE_MULTIPLE_REGISTERS = 0x10

#: Every function code this implementation understands.
SUPPORTED_FUNCTION_CODES = frozenset(
    {
        READ_COILS,
        READ_DISCRETE_INPUTS,
        READ_HOLDING_REGISTERS,
        READ_INPUT_REGISTERS,
        WRITE_SINGLE_COIL,
        WRITE_SINGLE_REGISTER,
        WRITE_MULTIPLE_COILS,
        WRITE_MULTIPLE_REGISTERS,
    }
)

#: Exception responses set the high bit of the echoed function code.
EXCEPTION_MASK = 0x80

# Exception codes -----------------------------------------------------------------------
ILLEGAL_FUNCTION = 0x01
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03
SERVER_DEVICE_FAILURE = 0x04

# On/off encoding for Write Single Coil.
COIL_ON = 0xFF00
COIL_OFF = 0x0000

# Per-request quantity limits (SPEC §2).
MAX_READ_BITS = 2000
MAX_READ_REGISTERS = 125
MAX_WRITE_COILS = 1968
MAX_WRITE_REGISTERS = 123

#: Largest value a single 16-bit register can hold.
REGISTER_MAX = 0xFFFF


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class ModbusError(Exception):
    """Base class for protocol errors that map onto a Modbus exception code.

    ``exception_code`` is set by each concrete subclass; ``function_code`` carries the
    offending request's function code when it is known, so the caller can echo it back in
    the ``fc | 0x80`` exception response.
    """

    exception_code: int

    def __init__(self, message: str = "", *, function_code: int | None = None) -> None:
        super().__init__(message)
        self.function_code = function_code


class IllegalFunction(ModbusError):
    exception_code = ILLEGAL_FUNCTION


class IllegalDataAddress(ModbusError):
    exception_code = ILLEGAL_DATA_ADDRESS


class IllegalDataValue(ModbusError):
    exception_code = ILLEGAL_DATA_VALUE


class ServerDeviceFailure(ModbusError):
    exception_code = SERVER_DEVICE_FAILURE


# --------------------------------------------------------------------------------------
# Bit / register packing helpers
# --------------------------------------------------------------------------------------


def pack_bits(bits: tuple[bool, ...] | list[bool]) -> bytes:
    """Pack booleans LSB-first: ``bits[0]`` becomes bit 0 of the first byte.

    ``n`` bits produce ``ceil(n / 8)`` bytes; unused high bits of the final byte are 0.
    """
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def unpack_bits(data: bytes, count: int) -> list[bool]:
    """Unpack exactly ``count`` LSB-first bits from ``data``."""
    if count < 0:
        raise ValueError("count must be non-negative")
    if count > len(data) * 8:
        raise IllegalDataValue("not enough bytes to unpack the requested bits")
    return [bool((data[i >> 3] >> (i & 7)) & 1) for i in range(count)]


def pack_registers(registers: tuple[int, ...] | list[int]) -> bytes:
    """Pack 16-bit registers big-endian. Raises ``struct.error`` if any value overflows."""
    return struct.pack(f">{len(registers)}H", *registers)


def unpack_registers(data: bytes) -> list[int]:
    """Unpack big-endian 16-bit registers from an even-length byte string."""
    if len(data) % 2:
        raise IllegalDataValue("register byte string must have even length")
    return list(struct.unpack(f">{len(data) // 2}H", data))


# --------------------------------------------------------------------------------------
# MBAP header
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MBAPHeader:
    """The 7-byte Modbus Application Protocol header."""

    transaction_id: int
    protocol_id: int
    length: int
    unit_id: int


def decode_mbap(data: bytes) -> MBAPHeader:
    """Parse the first 7 bytes of ``data`` into an :class:`MBAPHeader`."""
    if len(data) < MBAP_HEADER_LEN:
        raise ValueError(f"need at least {MBAP_HEADER_LEN} bytes for an MBAP header")
    transaction_id, protocol_id, length, unit_id = struct.unpack(">HHHB", data[:MBAP_HEADER_LEN])
    return MBAPHeader(transaction_id, protocol_id, length, unit_id)


def encode_mbap(header: MBAPHeader) -> bytes:
    """Serialize an :class:`MBAPHeader` to 7 bytes."""
    return struct.pack(
        ">HHHB", header.transaction_id, header.protocol_id, header.length, header.unit_id
    )


def build_adu(transaction_id: int, unit_id: int, pdu: bytes) -> bytes:
    """Wrap a PDU in a fresh MBAP header, computing ``length`` = unit id + PDU bytes."""
    length = 1 + len(pdu)
    return struct.pack(">HHHB", transaction_id, PROTOCOL_ID, length, unit_id) + pdu


# --------------------------------------------------------------------------------------
# Request PDUs
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadCoilsRequest:
    address: int
    count: int


@dataclass(frozen=True, slots=True)
class ReadDiscreteInputsRequest:
    address: int
    count: int


@dataclass(frozen=True, slots=True)
class ReadHoldingRegistersRequest:
    address: int
    count: int


@dataclass(frozen=True, slots=True)
class ReadInputRegistersRequest:
    address: int
    count: int


@dataclass(frozen=True, slots=True)
class WriteSingleCoilRequest:
    address: int
    value: bool


@dataclass(frozen=True, slots=True)
class WriteSingleRegisterRequest:
    address: int
    value: int


@dataclass(frozen=True, slots=True)
class WriteMultipleCoilsRequest:
    address: int
    values: tuple[bool, ...]


@dataclass(frozen=True, slots=True)
class WriteMultipleRegistersRequest:
    address: int
    values: tuple[int, ...]


Request = (
    ReadCoilsRequest
    | ReadDiscreteInputsRequest
    | ReadHoldingRegistersRequest
    | ReadInputRegistersRequest
    | WriteSingleCoilRequest
    | WriteSingleRegisterRequest
    | WriteMultipleCoilsRequest
    | WriteMultipleRegistersRequest
)


# --------------------------------------------------------------------------------------
# Response PDUs
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadCoilsResponse:
    bits: tuple[bool, ...]


@dataclass(frozen=True, slots=True)
class ReadDiscreteInputsResponse:
    bits: tuple[bool, ...]


@dataclass(frozen=True, slots=True)
class ReadHoldingRegistersResponse:
    registers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ReadInputRegistersResponse:
    registers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WriteSingleCoilResponse:
    address: int
    value: bool


@dataclass(frozen=True, slots=True)
class WriteSingleRegisterResponse:
    address: int
    value: int


@dataclass(frozen=True, slots=True)
class WriteMultipleCoilsResponse:
    address: int
    quantity: int


@dataclass(frozen=True, slots=True)
class WriteMultipleRegistersResponse:
    address: int
    quantity: int


@dataclass(frozen=True, slots=True)
class ExceptionResponse:
    """An exception response. ``function_code`` is the *original* code (no high bit)."""

    function_code: int
    exception_code: int


Response = (
    ReadCoilsResponse
    | ReadDiscreteInputsResponse
    | ReadHoldingRegistersResponse
    | ReadInputRegistersResponse
    | WriteSingleCoilResponse
    | WriteSingleRegisterResponse
    | WriteMultipleCoilsResponse
    | WriteMultipleRegistersResponse
    | ExceptionResponse
)


# --------------------------------------------------------------------------------------
# Request decoding
# --------------------------------------------------------------------------------------


def _read_request(pdu: bytes) -> tuple[int, int, int]:
    """Decode the common ``fc + addr(2) + qty(2)`` layout used by reads and single writes."""
    if len(pdu) != 5:
        raise IllegalDataValue("expected a 5-byte PDU", function_code=pdu[0])
    fc, addr, qty = struct.unpack(">BHH", pdu)
    return fc, addr, qty


def decode_request_pdu(pdu: bytes) -> Request:
    """Decode a request PDU into a typed dataclass.

    Raises :class:`IllegalFunction` for unknown function codes and :class:`IllegalDataValue`
    for structurally invalid payloads (wrong length, byte-count/quantity mismatch, illegal
    coil value). Quantity-range and address-range validation are left to the dispatcher.
    """
    if not pdu:
        raise IllegalDataValue("empty PDU")
    fc = pdu[0]

    if fc in (READ_COILS, READ_DISCRETE_INPUTS, READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS):
        _, addr, qty = _read_request(pdu)
        cls = {
            READ_COILS: ReadCoilsRequest,
            READ_DISCRETE_INPUTS: ReadDiscreteInputsRequest,
            READ_HOLDING_REGISTERS: ReadHoldingRegistersRequest,
            READ_INPUT_REGISTERS: ReadInputRegistersRequest,
        }[fc]
        return cls(address=addr, count=qty)

    if fc == WRITE_SINGLE_COIL:
        _, addr, raw = _read_request(pdu)
        if raw == COIL_ON:
            value = True
        elif raw == COIL_OFF:
            value = False
        else:
            raise IllegalDataValue(
                "coil value must be 0xFF00 or 0x0000", function_code=WRITE_SINGLE_COIL
            )
        return WriteSingleCoilRequest(address=addr, value=value)

    if fc == WRITE_SINGLE_REGISTER:
        _, addr, value = _read_request(pdu)
        return WriteSingleRegisterRequest(address=addr, value=value)

    if fc == WRITE_MULTIPLE_COILS:
        addr, qty, values = _decode_multi_write(pdu, WRITE_MULTIPLE_COILS)
        return WriteMultipleCoilsRequest(address=addr, values=tuple(unpack_bits(values, qty)))

    if fc == WRITE_MULTIPLE_REGISTERS:
        addr, qty, values = _decode_multi_write(pdu, WRITE_MULTIPLE_REGISTERS)
        return WriteMultipleRegistersRequest(address=addr, values=tuple(unpack_registers(values)))

    raise IllegalFunction(f"unsupported function code 0x{fc:02x}", function_code=fc)


def _decode_multi_write(pdu: bytes, fc: int) -> tuple[int, int, bytes]:
    """Decode ``fc + addr(2) + qty(2) + byte_count(1) + payload`` and validate consistency."""
    if len(pdu) < 6:
        raise IllegalDataValue("multi-write PDU too short", function_code=fc)
    _, addr, qty, byte_count = struct.unpack(">BHHB", pdu[:6])
    payload = pdu[6:]
    if len(payload) != byte_count:
        raise IllegalDataValue("byte count does not match payload length", function_code=fc)
    expected = 2 * qty if fc == WRITE_MULTIPLE_REGISTERS else (qty + 7) // 8
    if byte_count != expected:
        raise IllegalDataValue("byte count inconsistent with quantity", function_code=fc)
    return addr, qty, payload


# --------------------------------------------------------------------------------------
# Request encoding
# --------------------------------------------------------------------------------------


def encode_request_pdu(request: Request) -> bytes:
    """Serialize a request dataclass to a PDU. Inverse of :func:`decode_request_pdu`."""
    match request:
        case ReadCoilsRequest(address=a, count=c):
            return struct.pack(">BHH", READ_COILS, a, c)
        case ReadDiscreteInputsRequest(address=a, count=c):
            return struct.pack(">BHH", READ_DISCRETE_INPUTS, a, c)
        case ReadHoldingRegistersRequest(address=a, count=c):
            return struct.pack(">BHH", READ_HOLDING_REGISTERS, a, c)
        case ReadInputRegistersRequest(address=a, count=c):
            return struct.pack(">BHH", READ_INPUT_REGISTERS, a, c)
        case WriteSingleCoilRequest(address=a, value=v):
            return struct.pack(">BHH", WRITE_SINGLE_COIL, a, COIL_ON if v else COIL_OFF)
        case WriteSingleRegisterRequest(address=a, value=v):
            return struct.pack(">BHH", WRITE_SINGLE_REGISTER, a, v)
        case WriteMultipleCoilsRequest(address=a, values=vals):
            body = pack_bits(vals)
            return struct.pack(">BHHB", WRITE_MULTIPLE_COILS, a, len(vals), len(body)) + body
        case WriteMultipleRegistersRequest(address=a, values=vals):
            body = pack_registers(vals)
            return struct.pack(">BHHB", WRITE_MULTIPLE_REGISTERS, a, len(vals), len(body)) + body
    raise TypeError(f"not a request dataclass: {request!r}")


# --------------------------------------------------------------------------------------
# Response encoding
# --------------------------------------------------------------------------------------


def encode_response_pdu(response: Response) -> bytes:
    """Serialize a response dataclass to a PDU."""
    match response:
        case ReadCoilsResponse(bits=b):
            body = pack_bits(b)
            return struct.pack(">BB", READ_COILS, len(body)) + body
        case ReadDiscreteInputsResponse(bits=b):
            body = pack_bits(b)
            return struct.pack(">BB", READ_DISCRETE_INPUTS, len(body)) + body
        case ReadHoldingRegistersResponse(registers=r):
            body = pack_registers(r)
            return struct.pack(">BB", READ_HOLDING_REGISTERS, len(body)) + body
        case ReadInputRegistersResponse(registers=r):
            body = pack_registers(r)
            return struct.pack(">BB", READ_INPUT_REGISTERS, len(body)) + body
        case WriteSingleCoilResponse(address=a, value=v):
            return struct.pack(">BHH", WRITE_SINGLE_COIL, a, COIL_ON if v else COIL_OFF)
        case WriteSingleRegisterResponse(address=a, value=v):
            return struct.pack(">BHH", WRITE_SINGLE_REGISTER, a, v)
        case WriteMultipleCoilsResponse(address=a, quantity=q):
            return struct.pack(">BHH", WRITE_MULTIPLE_COILS, a, q)
        case WriteMultipleRegistersResponse(address=a, quantity=q):
            return struct.pack(">BHH", WRITE_MULTIPLE_REGISTERS, a, q)
        case ExceptionResponse(function_code=fc, exception_code=ec):
            return struct.pack(">BB", fc | EXCEPTION_MASK, ec)
    raise TypeError(f"not a response dataclass: {response!r}")


# --------------------------------------------------------------------------------------
# Response decoding (used by the client CLI and interop tests)
# --------------------------------------------------------------------------------------


def decode_response_pdu(pdu: bytes, *, bit_count: int | None = None) -> Response:
    """Decode a response PDU. For coil/discrete reads, pass ``bit_count`` (the quantity
    originally requested) to recover the exact number of bits, since the wire format only
    carries a whole-byte count."""
    if not pdu:
        raise IllegalDataValue("empty PDU")
    fc = pdu[0]

    if fc & EXCEPTION_MASK:
        if len(pdu) != 2:
            raise IllegalDataValue("exception response must be 2 bytes")
        return ExceptionResponse(function_code=fc & ~EXCEPTION_MASK, exception_code=pdu[1])

    if fc in (READ_COILS, READ_DISCRETE_INPUTS):
        byte_count = pdu[1]
        if len(pdu) != 2 + byte_count:
            raise IllegalDataValue("byte count does not match response length")
        count = bit_count if bit_count is not None else byte_count * 8
        bits = tuple(unpack_bits(pdu[2:], count))
        return ReadCoilsResponse(bits) if fc == READ_COILS else ReadDiscreteInputsResponse(bits)

    if fc in (READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS):
        byte_count = pdu[1]
        if len(pdu) != 2 + byte_count:
            raise IllegalDataValue("byte count does not match response length")
        regs = tuple(unpack_registers(pdu[2:]))
        if fc == READ_HOLDING_REGISTERS:
            return ReadHoldingRegistersResponse(regs)
        return ReadInputRegistersResponse(regs)

    if fc in (WRITE_SINGLE_COIL, WRITE_SINGLE_REGISTER):
        _, addr, raw = _read_request(pdu)
        if fc == WRITE_SINGLE_COIL:
            return WriteSingleCoilResponse(address=addr, value=raw == COIL_ON)
        return WriteSingleRegisterResponse(address=addr, value=raw)

    if fc in (WRITE_MULTIPLE_COILS, WRITE_MULTIPLE_REGISTERS):
        if len(pdu) != 5:
            raise IllegalDataValue("expected a 5-byte PDU")
        _, addr, qty = struct.unpack(">BHH", pdu)
        if fc == WRITE_MULTIPLE_COILS:
            return WriteMultipleCoilsResponse(address=addr, quantity=qty)
        return WriteMultipleRegistersResponse(address=addr, quantity=qty)

    raise IllegalFunction(f"unsupported function code 0x{fc:02x}", function_code=fc)
