"""Property-based tests with Hypothesis (SPEC §10).

Two properties are asserted:

1. **Round-trip identity** — for any *valid* request, ``decode(encode(req)) == req``.
2. **Fuzz safety** — the request decoder and the stream framer never raise anything other
   than their declared protocol errors, no matter what bytes they are fed.
"""

import contextlib

import hypothesis.strategies as st
from hypothesis import given, settings

from modbus_sim import frame as f
from modbus_sim.server import ProtocolError, StreamFramer

_u16 = st.integers(min_value=0, max_value=0xFFFF)

# Valid requests only: quantities within their protocol limits, values within uint16.
# Multi-write lengths are capped below their true maxima to keep example generation fast.
requests = st.one_of(
    st.builds(f.ReadCoilsRequest, _u16, st.integers(1, f.MAX_READ_BITS)),
    st.builds(f.ReadDiscreteInputsRequest, _u16, st.integers(1, f.MAX_READ_BITS)),
    st.builds(f.ReadHoldingRegistersRequest, _u16, st.integers(1, f.MAX_READ_REGISTERS)),
    st.builds(f.ReadInputRegistersRequest, _u16, st.integers(1, f.MAX_READ_REGISTERS)),
    st.builds(f.WriteSingleCoilRequest, _u16, st.booleans()),
    st.builds(f.WriteSingleRegisterRequest, _u16, _u16),
    st.builds(
        lambda a, v: f.WriteMultipleCoilsRequest(a, tuple(v)),
        _u16,
        st.lists(st.booleans(), min_size=1, max_size=256),
    ),
    st.builds(
        lambda a, v: f.WriteMultipleRegistersRequest(a, tuple(v)),
        _u16,
        st.lists(_u16, min_size=1, max_size=f.MAX_WRITE_REGISTERS),
    ),
)


@given(request=requests)
def test_request_encode_decode_roundtrip(request):
    assert f.decode_request_pdu(f.encode_request_pdu(request)) == request


@given(request=requests)
def test_encoded_request_is_self_consistent_after_framing(request):
    # Wrapping in an ADU and re-reading the header must preserve unit/transaction id.
    pdu = f.encode_request_pdu(request)
    adu = f.build_adu(0x1234, 7, pdu)
    header = f.decode_mbap(adu)
    assert header.transaction_id == 0x1234
    assert header.unit_id == 7
    assert header.length == len(pdu) + 1
    assert adu[f.MBAP_HEADER_LEN :] == pdu


@settings(max_examples=500)
@given(data=st.binary(max_size=f.MAX_ADU_LEN))
def test_request_decoder_only_raises_modbus_error(data):
    # The decoder may return a request or raise a ModbusError, but must never crash with
    # struct.error / IndexError / etc. on arbitrary input.
    with contextlib.suppress(f.ModbusError):
        f.decode_request_pdu(data)


@settings(max_examples=500)
@given(chunks=st.lists(st.binary(max_size=64), max_size=10))
def test_stream_framer_only_raises_protocol_error(chunks):
    framer = StreamFramer()
    for chunk in chunks:
        try:
            for _header, _adu in framer.feed(chunk):
                pass
        except ProtocolError:
            return  # a protocol error is allowed and terminates the connection
