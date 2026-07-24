"""In-memory device data model: the four Modbus register regions plus a per-device lock.

A :class:`MemoryBlock` is one addressable region (coils, discrete inputs, holding registers
or input registers). Every read/write is bounds-checked against the block size and raises
:class:`modbus_sim.frame.IllegalDataAddress` on overflow — that is the *only* place the
address-range (exception code 02) decision is made.

The store carries no protocol knowledge (no function codes, no quantity limits); those live
in :mod:`modbus_sim.dispatcher`. Each :class:`Device` owns an ``asyncio.Lock`` so the server
can serialize client writes against the background generator task (SPEC §6).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from modbus_sim.frame import IllegalDataAddress


class MemoryBlock:
    """A fixed-size, zero-based addressable region of bits or 16-bit registers."""

    __slots__ = ("size", "_values")

    def __init__(
        self, size: int, values: Sequence[int | bool] | None = None, *, fill: int | bool = 0
    ):
        if size < 0:
            raise ValueError("size must be non-negative")
        vals: list[int | bool] = list(values) if values is not None else []
        if len(vals) > size:
            raise ValueError(f"{len(vals)} initial values exceed block size {size}")
        vals.extend([fill] * (size - len(vals)))
        self.size = size
        self._values = vals

    def _check(self, address: int, count: int) -> None:
        if address < 0 or count < 0 or address + count > self.size:
            raise IllegalDataAddress(
                f"address range [{address}, {address + count}) outside [0, {self.size})"
            )

    def read(self, address: int, count: int) -> list[int | bool]:
        """Return ``count`` values starting at ``address`` (a fresh copy)."""
        self._check(address, count)
        return self._values[address : address + count]

    def write(self, address: int, values: Sequence[int | bool]) -> None:
        """Overwrite the block starting at ``address`` with ``values``."""
        self._check(address, len(values))
        self._values[address : address + len(values)] = values

    def set(self, address: int, value: int | bool) -> None:
        """Set a single slot (used by the background generators)."""
        self._check(address, 1)
        self._values[address] = value

    def snapshot(self) -> list[int | bool]:
        """A copy of the whole block, for logging / inspection."""
        return list(self._values)

    def __len__(self) -> int:
        return self.size


@dataclass(slots=True)
class Device:
    """A single simulated Modbus device: an addressable unit with four regions."""

    unit_id: int
    name: str
    coils: MemoryBlock
    discrete_inputs: MemoryBlock
    holding_registers: MemoryBlock
    input_registers: MemoryBlock
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def build_device(
    unit_id: int,
    name: str = "",
    *,
    coils: int = 0,
    discrete_inputs: int = 0,
    holding_registers: int = 0,
    input_registers: int = 0,
) -> Device:
    """Convenience factory building a device from region *sizes* (all slots zeroed)."""
    return Device(
        unit_id=unit_id,
        name=name or f"device-{unit_id}",
        coils=MemoryBlock(coils, fill=False),
        discrete_inputs=MemoryBlock(discrete_inputs, fill=False),
        holding_registers=MemoryBlock(holding_registers, fill=0),
        input_registers=MemoryBlock(input_registers, fill=0),
    )


class DataStore:
    """A collection of devices keyed by unit id."""

    def __init__(self, devices: Iterable[Device] = ()):
        self._by_unit: dict[int, Device] = {}
        for device in devices:
            self.add(device)

    def add(self, device: Device) -> None:
        if device.unit_id in self._by_unit:
            raise ValueError(f"duplicate unit id {device.unit_id}")
        self._by_unit[device.unit_id] = device

    def get(self, unit_id: int) -> Device | None:
        """Return the device for ``unit_id`` or ``None`` (unknown units are dropped)."""
        return self._by_unit.get(unit_id)

    @property
    def units(self) -> list[int]:
        return list(self._by_unit)

    def __iter__(self):
        return iter(self._by_unit.values())

    def __len__(self) -> int:
        return len(self._by_unit)
