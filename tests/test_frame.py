"""Unit tests for the pure frame codec (:mod:`modbus_sim.frame`).

Where possible the tests pin exact byte strings taken from the official *Modbus Application
Protocol Specification v1.1b3* examples, so correctness is proven against the standard and
not merely against our own round-trip.
"""

import struct

import pytest

from modbus_sim import frame as f

# --------------------------------------------------------------------------------------
# Bit / register packing
# --------------------------------------------------------------------------------------


def test_pack_bits_is_lsb_first():
    # bits[0] -> bit 0 of byte 0 (value 0x01), bits[7] -> bit 7 (value 0x80).
    assert f.pack_bits([True]) == b"\x01"
    assert f.pack_bits([False, True]) == b"\x02"
    assert f.pack_bits([True] * 8) == b"\xff"
    # 10 bits -> 2 bytes; the canonical Modbus example byte pattern 0xCD 0x01.
    bits = [True, False, True, True, False, False, True, True, True, False]
    assert f.pack_bits(bits) == b"\xcd\x01"


def test_unpack_bits_trims_to_count():
    assert f.unpack_bits(b"\xcd\x01", 10) == [
        True,
        False,
        True,
        True,
        False,
        False,
        True,
        True,
        True,
        False,
    ]
    # Padding bits beyond ``count`` are ignored.
    assert f.unpack_bits(b"\xff", 3) == [True, True, True]


def test_unpack_bits_roundtrip_non_byte_aligned():
    bits = [True, False, False, True, True]  # 5 bits
    assert f.unpack_bits(f.pack_bits(bits), len(bits)) == bits


def test_unpack_bits_rejects_short_buffer():
    with pytest.raises(f.IllegalDataValue):
        f.unpack_bits(b"\x00", 9)


def test_register_pack_unpack_roundtrip():
    regs = [0, 1, 0x1234, 0xFFFF]
    assert f.unpack_registers(f.pack_registers(regs)) == regs


def test_unpack_registers_rejects_odd_length():
    with pytest.raises(f.IllegalDataValue):
        f.unpack_registers(b"\x00\x01\x02")


def test_pack_registers_rejects_overflow():
    with pytest.raises(struct.error):
        f.pack_registers([0x1_0000])


# --------------------------------------------------------------------------------------
# MBAP header
# --------------------------------------------------------------------------------------


def test_mbap_roundtrip():
    header = f.MBAPHeader(transaction_id=0x1234, protocol_id=0, length=6, unit_id=1)
    assert f.decode_mbap(f.encode_mbap(header)) == header


def test_build_adu_length_includes_unit_id():
    # The classic bug: Length must count the unit id byte plus the whole PDU.
    pdu = b"\x03\x00\x00\x00\x04"  # 5-byte PDU
    adu = f.build_adu(transaction_id=7, unit_id=1, pdu=pdu)
    header = f.decode_mbap(adu)
    assert header.length == len(pdu) + 1 == 6
    assert header.transaction_id == 7
    assert header.protocol_id == f.PROTOCOL_ID == 0
    assert header.unit_id == 1
    assert adu[f.MBAP_HEADER_LEN :] == pdu


def test_decode_mbap_preserves_nonzero_protocol_id():
    # The frame layer just reports protocol_id; the server decides to drop it.
    raw = struct.pack(">HHHB", 1, 1, 6, 1) + b"\x03\x00\x00\x00\x01"
    assert f.decode_mbap(raw).protocol_id == 1


def test_decode_mbap_needs_seven_bytes():
    with pytest.raises(ValueError):
        f.decode_mbap(b"\x00\x00\x00")


# --------------------------------------------------------------------------------------
# Golden request vectors (Modbus spec examples)
# --------------------------------------------------------------------------------------

_WMC_BITS = (True, False, True, True, False, False, True, True, True, False)  # 0xCD 0x01

REQUEST_VECTORS = [
    (f.ReadCoilsRequest(0x0013, 0x0013), bytes.fromhex("0100130013")),
    (f.ReadDiscreteInputsRequest(0x00C4, 0x0016), bytes.fromhex("0200C40016")),
    (f.ReadHoldingRegistersRequest(0x006B, 0x0003), bytes.fromhex("03006B0003")),
    (f.ReadInputRegistersRequest(0x0008, 0x0001), bytes.fromhex("0400080001")),
    (f.WriteSingleCoilRequest(0x00AC, True), bytes.fromhex("0500ACFF00")),
    (f.WriteSingleCoilRequest(0x00AC, False), bytes.fromhex("0500AC0000")),
    (f.WriteSingleRegisterRequest(0x0001, 0x0003), bytes.fromhex("0600010003")),
    (f.WriteMultipleCoilsRequest(0x0013, _WMC_BITS), bytes.fromhex("0F0013000A02CD01")),
    (
        f.WriteMultipleRegistersRequest(0x0001, (0x000A, 0x0102)),
        bytes.fromhex("100001000204000A0102"),
    ),
]


@pytest.mark.parametrize("request_obj, expected", REQUEST_VECTORS)
def test_request_encode_matches_spec_bytes(request_obj, expected):
    assert f.encode_request_pdu(request_obj) == expected


@pytest.mark.parametrize("request_obj, encoded", REQUEST_VECTORS)
def test_request_decode_roundtrip(request_obj, encoded):
    assert f.decode_request_pdu(encoded) == request_obj
    assert f.decode_request_pdu(f.encode_request_pdu(request_obj)) == request_obj


# --------------------------------------------------------------------------------------
# Golden response vectors (Modbus spec examples)
# --------------------------------------------------------------------------------------

RESPONSE_VECTORS = [
    # (dataclass, exact PDU bytes, bit_count for decoding coil reads)
    # Spec example: 19 coils, status bytes 0xCD 0x6B 0x05 (LSB first).
    (
        f.ReadCoilsResponse(
            (
                True,
                False,
                True,
                True,
                False,
                False,
                True,
                True,  # 0xCD
                True,
                True,
                False,
                True,
                False,
                True,
                True,
                False,  # 0x6B
                True,
                False,
                True,  # 0x05 (3 bits)
            )
        ),
        bytes.fromhex("0103CD6B05"),
        19,
    ),
    (
        f.ReadHoldingRegistersResponse((0x022B, 0x0000, 0x0064)),
        bytes.fromhex("0306022B00000064"),
        None,
    ),
    (f.ReadInputRegistersResponse((0x000A,)), bytes.fromhex("0402000A"), None),
    (f.WriteSingleCoilResponse(0x00AC, True), bytes.fromhex("0500ACFF00"), None),
    (f.WriteSingleRegisterResponse(0x0001, 0x0003), bytes.fromhex("0600010003"), None),
    (f.WriteMultipleCoilsResponse(0x0013, 0x000A), bytes.fromhex("0F0013000A"), None),
    (f.WriteMultipleRegistersResponse(0x0001, 0x0002), bytes.fromhex("1000010002"), None),
]


@pytest.mark.parametrize("response_obj, expected, bit_count", RESPONSE_VECTORS)
def test_response_encode_matches_spec_bytes(response_obj, expected, bit_count):
    assert f.encode_response_pdu(response_obj) == expected


@pytest.mark.parametrize("response_obj, expected, bit_count", RESPONSE_VECTORS)
def test_response_decode_roundtrip(response_obj, expected, bit_count):
    assert f.decode_response_pdu(expected, bit_count=bit_count) == response_obj


# --------------------------------------------------------------------------------------
# Exception responses
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("fc", sorted(f.SUPPORTED_FUNCTION_CODES))
@pytest.mark.parametrize("code", [1, 2, 3, 4])
def test_exception_response_sets_high_bit(fc, code):
    pdu = f.encode_response_pdu(f.ExceptionResponse(fc, code))
    assert pdu == bytes([fc | 0x80, code])
    assert f.decode_response_pdu(pdu) == f.ExceptionResponse(fc, code)


# --------------------------------------------------------------------------------------
# Decode error paths
# --------------------------------------------------------------------------------------


def test_unknown_function_code_raises_illegal_function():
    with pytest.raises(f.IllegalFunction) as exc:
        f.decode_request_pdu(b"\x2b\x00\x00")
    assert exc.value.exception_code == f.ILLEGAL_FUNCTION
    assert exc.value.function_code == 0x2B


def test_write_single_coil_bad_value_raises_illegal_data_value():
    with pytest.raises(f.IllegalDataValue) as exc:
        f.decode_request_pdu(bytes.fromhex("0500AC1234"))
    assert exc.value.exception_code == f.ILLEGAL_DATA_VALUE
    assert exc.value.function_code == f.WRITE_SINGLE_COIL


def test_read_request_wrong_length_raises():
    with pytest.raises(f.IllegalDataValue):
        f.decode_request_pdu(b"\x03\x00\x00")  # missing quantity bytes


def test_multi_write_registers_byte_count_mismatch_raises():
    # qty=2 registers implies byte_count 4, but header claims 2.
    bad = bytes.fromhex("1000010002020000")
    with pytest.raises(f.IllegalDataValue):
        f.decode_request_pdu(bad)


def test_multi_write_payload_length_mismatch_raises():
    # byte_count says 4 but only 2 payload bytes are present.
    bad = bytes.fromhex("100001000204 0000".replace(" ", ""))
    with pytest.raises(f.IllegalDataValue):
        f.decode_request_pdu(bad)


def test_multi_write_coils_byte_count_mismatch_raises():
    # qty=10 coils implies byte_count 2, but header claims 1 (with 1 payload byte).
    bad = bytes.fromhex("0F0013000A01CD")
    with pytest.raises(f.IllegalDataValue):
        f.decode_request_pdu(bad)


def test_empty_pdu_raises():
    with pytest.raises(f.IllegalDataValue):
        f.decode_request_pdu(b"")


def test_unpack_bits_rejects_negative_count():
    with pytest.raises(ValueError):
        f.unpack_bits(b"\x00", -1)


# --------------------------------------------------------------------------------------
# Response decode error paths (used by the client)
# --------------------------------------------------------------------------------------


def test_decode_response_empty_raises():
    with pytest.raises(f.IllegalDataValue):
        f.decode_response_pdu(b"")


def test_decode_response_exception_wrong_length_raises():
    with pytest.raises(f.IllegalDataValue):
        f.decode_response_pdu(b"\x83\x02\x00")  # exception PDUs are exactly 2 bytes


def test_decode_response_read_registers_byte_count_mismatch_raises():
    with pytest.raises(f.IllegalDataValue):
        f.decode_response_pdu(
            bytes.fromhex("0306022B0000")
        )  # byte_count 6 but only 4 payload bytes


def test_decode_response_read_coils_byte_count_mismatch_raises():
    with pytest.raises(f.IllegalDataValue):
        f.decode_response_pdu(bytes.fromhex("0103CD6B"))  # byte_count 3 but only 2 payload bytes


def test_decode_response_unknown_function_raises():
    with pytest.raises(f.IllegalFunction):
        f.decode_response_pdu(b"\x2b\x00\x00")
