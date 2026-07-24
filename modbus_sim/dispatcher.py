"""Function-code dispatch and exception-code generation.

:func:`handle_request` maps a decoded request onto a device's data regions and returns a
response dataclass, raising :class:`modbus_sim.frame.ModbusError` for semantic problems.
:func:`process_pdu` is the byte-level entry point the server calls: it decodes, dispatches,
and turns every failure into the correct exception PDU.

Exception-code priority (SPEC §3) is enforced by *where* each check happens:

1. **01 Illegal Function** — unknown function code, detected while decoding.
2. **03 Illegal Data Value** — quantity out of range / byte-count mismatch / bad coil value,
   checked *before* any region access.
3. **02 Illegal Data Address** — raised by the data region only after the quantity is known
   to be valid, so an illegal quantity always wins over an illegal address.
4. **04 Server Device Failure** — catch-all for an unexpected internal error.

This module never touches the network; it is a pure function of ``(device, bytes)`` (aside
from logging an unexpected internal error), which is what lets the dispatcher tests run
without a server.
"""

from __future__ import annotations

import logging

from modbus_sim import frame
from modbus_sim.datastore import Device

logger = logging.getLogger(__name__)


def _require_quantity(count: int, limit: int) -> None:
    """Validate a read/write quantity, raising IllegalDataValue (03) if out of range."""
    if count < 1 or count > limit:
        raise frame.IllegalDataValue(f"quantity {count} outside [1, {limit}]")


def handle_request(device: Device, request: frame.Request) -> frame.Response:
    """Execute a decoded request against ``device`` and build the response dataclass.

    Raises :class:`modbus_sim.frame.IllegalDataValue` for illegal quantities and
    :class:`modbus_sim.frame.IllegalDataAddress` (from the data region) for bad addresses.
    """
    match request:
        case frame.ReadCoilsRequest(address=addr, count=count):
            _require_quantity(count, frame.MAX_READ_BITS)
            bits = device.coils.read(addr, count)
            return frame.ReadCoilsResponse(tuple(bool(b) for b in bits))

        case frame.ReadDiscreteInputsRequest(address=addr, count=count):
            _require_quantity(count, frame.MAX_READ_BITS)
            bits = device.discrete_inputs.read(addr, count)
            return frame.ReadDiscreteInputsResponse(tuple(bool(b) for b in bits))

        case frame.ReadHoldingRegistersRequest(address=addr, count=count):
            _require_quantity(count, frame.MAX_READ_REGISTERS)
            regs = device.holding_registers.read(addr, count)
            return frame.ReadHoldingRegistersResponse(tuple(int(r) for r in regs))

        case frame.ReadInputRegistersRequest(address=addr, count=count):
            _require_quantity(count, frame.MAX_READ_REGISTERS)
            regs = device.input_registers.read(addr, count)
            return frame.ReadInputRegistersResponse(tuple(int(r) for r in regs))

        case frame.WriteSingleCoilRequest(address=addr, value=value):
            device.coils.write(addr, [value])
            return frame.WriteSingleCoilResponse(addr, value)

        case frame.WriteSingleRegisterRequest(address=addr, value=value):
            device.holding_registers.write(addr, [value])
            return frame.WriteSingleRegisterResponse(addr, value)

        case frame.WriteMultipleCoilsRequest(address=addr, values=values):
            _require_quantity(len(values), frame.MAX_WRITE_COILS)
            device.coils.write(addr, list(values))
            return frame.WriteMultipleCoilsResponse(addr, len(values))

        case frame.WriteMultipleRegistersRequest(address=addr, values=values):
            _require_quantity(len(values), frame.MAX_WRITE_REGISTERS)
            device.holding_registers.write(addr, list(values))
            return frame.WriteMultipleRegistersResponse(addr, len(values))

    # decode_request_pdu only ever produces the dataclasses above, so this is unreachable.
    raise frame.ServerDeviceFailure(f"unhandled request type {type(request).__name__}")


def process_pdu(device: Device, pdu: bytes) -> bytes:
    """Decode, dispatch, and encode a single request PDU into a response PDU.

    Any :class:`modbus_sim.frame.ModbusError` becomes the matching exception response; any
    other exception is logged and mapped to Server Device Failure (04).
    """
    fallback_fc = (pdu[0] & ~frame.EXCEPTION_MASK) if pdu else 0
    try:
        request = frame.decode_request_pdu(pdu)
        response: frame.Response = handle_request(device, request)
    except frame.ModbusError as exc:
        fc = exc.function_code if exc.function_code is not None else fallback_fc
        response = frame.ExceptionResponse(fc, exc.exception_code)
    except Exception:  # noqa: BLE001 - last-resort safety net -> exception code 04
        logger.exception("unhandled error processing PDU %s", pdu.hex())
        response = frame.ExceptionResponse(fallback_fc, frame.SERVER_DEVICE_FAILURE)
    return frame.encode_response_pdu(response)
