"""Dynamic register value generators — so a simulated device looks *alive*.

Three generators are supported (SPEC §5):

* ``constant``     — a fixed value.
* ``sine``         — ``mean + amp * sin(2*pi * t / period_s)`` over elapsed time ``t``.
* ``random_walk``  — a bounded ±``step`` walk seeded from ``start``.

Every generator's :meth:`sample` output is clamped to the uint16 range ``[0, 65535]``. The
server ticks all generators once per second and writes each result into its register slot
under the owning device's lock (:class:`GeneratorBinding`).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from modbus_sim.datastore import Device, MemoryBlock

UINT16_MIN = 0
UINT16_MAX = 0xFFFF


def clamp_u16(value: float) -> int:
    """Round to the nearest integer and clamp into the uint16 range."""
    ivalue = int(round(value))
    if ivalue < UINT16_MIN:
        return UINT16_MIN
    if ivalue > UINT16_MAX:
        return UINT16_MAX
    return ivalue


@runtime_checkable
class Generator(Protocol):
    """A source of register values as a function of elapsed time."""

    def sample(self, elapsed: float) -> int:
        """Return the register value at ``elapsed`` seconds since start."""
        ...


class Constant:
    def __init__(self, value: float):
        self.value = clamp_u16(value)

    def sample(self, elapsed: float) -> int:
        return self.value


class Sine:
    def __init__(self, mean: float, amp: float, period_s: float):
        if period_s <= 0:
            raise ValueError("period_s must be positive")
        self.mean = mean
        self.amp = amp
        self.period_s = period_s

    def sample(self, elapsed: float) -> int:
        return clamp_u16(self.mean + self.amp * math.sin(2 * math.pi * elapsed / self.period_s))


class RandomWalk:
    def __init__(self, start: float, step: float, min: float, max: float, seed: int | None = None):
        if min > max:
            raise ValueError("min must not exceed max")
        self.step = step
        self._min = min
        self._max = max
        self._value = self._bound(start)
        self._rng = random.Random(seed)

    def _bound(self, value: float) -> float:
        if value < self._min:
            return self._min
        if value > self._max:
            return self._max
        return value

    def sample(self, elapsed: float) -> int:
        delta = self._rng.choice((-self.step, self.step))
        self._value = self._bound(self._value + delta)
        return clamp_u16(self._value)


_FACTORIES = {
    "constant": Constant,
    "sine": Sine,
    "random_walk": RandomWalk,
}


def make_generator(fn: str, args: dict | None = None) -> Generator:
    """Build a generator by name. ``args`` are passed as keyword arguments."""
    try:
        factory = _FACTORIES[fn]
    except KeyError:
        raise ValueError(f"unknown generator function {fn!r}") from None
    return factory(**(args or {}))


@dataclass(slots=True)
class GeneratorBinding:
    """A generator wired to one register slot of one device."""

    device: Device
    block: MemoryBlock
    address: int
    generator: Generator
    label: str = field(default="")

    def update(self, elapsed: float) -> int:
        """Sample the generator and write the value into the register slot."""
        value = self.generator.sample(elapsed)
        self.block.set(self.address, value)
        return value
