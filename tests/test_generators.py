"""Unit tests for the dynamic value generators (:mod:`modbus_sim.generators`)."""

import math

import pytest

from modbus_sim.datastore import build_device
from modbus_sim.generators import (
    UINT16_MAX,
    Constant,
    GeneratorBinding,
    RandomWalk,
    Sine,
    clamp_u16,
    make_generator,
)


@pytest.mark.parametrize(
    "value, expected",
    [(-5, 0), (0, 0), (12.4, 12), (12.6, 13), (70000, UINT16_MAX), (65535, 65535)],
)
def test_clamp_u16(value, expected):
    assert clamp_u16(value) == expected


def test_constant_is_fixed_and_clamped():
    assert Constant(42).sample(0) == 42
    assert Constant(42).sample(1000) == 42
    assert Constant(-3).sample(0) == 0


def test_sine_hits_mean_and_peaks():
    gen = Sine(mean=250, amp=30, period_s=60)
    assert gen.sample(0) == 250  # sin(0) = 0
    assert gen.sample(15) == 280  # quarter period -> +amp
    assert gen.sample(45) == 220  # three-quarter period -> -amp


def test_sine_clamps_to_uint16():
    assert Sine(mean=0, amp=100, period_s=4).sample(3) == 0  # sin(3/4 * 2pi) = -1
    assert Sine(mean=65535, amp=100, period_s=4).sample(1) == UINT16_MAX


def test_sine_rejects_non_positive_period():
    with pytest.raises(ValueError):
        Sine(mean=0, amp=1, period_s=0)


def test_random_walk_is_deterministic_with_seed():
    a = RandomWalk(start=500, step=5, min=0, max=1000, seed=7)
    b = RandomWalk(start=500, step=5, min=0, max=1000, seed=7)
    seq_a = [a.sample(i) for i in range(20)]
    seq_b = [b.sample(i) for i in range(20)]
    assert seq_a == seq_b


def test_random_walk_stays_within_bounds():
    gen = RandomWalk(start=5, step=3, min=0, max=10, seed=1)
    values = [gen.sample(i) for i in range(500)]
    assert all(0 <= v <= 10 for v in values)


def test_random_walk_moves_by_step():
    gen = RandomWalk(start=100, step=5, min=0, max=200, seed=1)
    prev = 100
    for i in range(50):
        value = gen.sample(i)
        assert abs(value - prev) in (0, 5)  # 0 only when clamped at a bound
        prev = value


def test_random_walk_rejects_inverted_bounds():
    with pytest.raises(ValueError):
        RandomWalk(start=0, step=1, min=10, max=5)


def test_make_generator_dispatch():
    assert isinstance(make_generator("constant", {"value": 1}), Constant)
    assert isinstance(make_generator("sine", {"mean": 0, "amp": 1, "period_s": 1}), Sine)
    assert isinstance(
        make_generator("random_walk", {"start": 0, "step": 1, "min": 0, "max": 2}), RandomWalk
    )


def test_make_generator_unknown_raises():
    with pytest.raises(ValueError):
        make_generator("triangle", {})


def test_make_generator_bad_args_raises():
    with pytest.raises(TypeError):
        make_generator("sine", {"mean": 0})  # missing amp / period_s


def test_generator_binding_writes_into_register_slot():
    device = build_device(1, input_registers=4)
    binding = GeneratorBinding(device, device.input_registers, 2, Constant(777))
    assert binding.update(0.0) == 777
    assert device.input_registers.read(2, 1) == [777]


def test_binding_sine_updates_over_time():
    device = build_device(1, input_registers=2)
    binding = GeneratorBinding(device, device.input_registers, 0, Sine(250, 30, 60))
    binding.update(0.0)
    assert device.input_registers.read(0, 1) == [250]
    binding.update(15.0)
    assert device.input_registers.read(0, 1) == [round(250 + 30 * math.sin(math.pi / 2))]
