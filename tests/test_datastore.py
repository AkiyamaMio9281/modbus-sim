"""Unit tests for the in-memory data model (:mod:`modbus_sim.datastore`)."""

import pytest

from modbus_sim.datastore import DataStore, Device, MemoryBlock, build_device
from modbus_sim.frame import IllegalDataAddress

# --------------------------------------------------------------------------------------
# MemoryBlock
# --------------------------------------------------------------------------------------


def test_block_defaults_to_fill_value():
    assert MemoryBlock(4, fill=0).snapshot() == [0, 0, 0, 0]
    assert MemoryBlock(3, fill=False).snapshot() == [False, False, False]


def test_block_honours_initial_values_and_pads():
    block = MemoryBlock(4, [11, 22])
    assert block.snapshot() == [11, 22, 0, 0]


def test_block_rejects_too_many_initial_values():
    with pytest.raises(ValueError):
        MemoryBlock(2, [1, 2, 3])


def test_block_rejects_negative_size():
    with pytest.raises(ValueError):
        MemoryBlock(-1)


def test_read_write_roundtrip():
    block = MemoryBlock(8, fill=0)
    block.write(2, [100, 200, 300])
    assert block.read(2, 3) == [100, 200, 300]
    assert block.read(0, 8) == [0, 0, 100, 200, 300, 0, 0, 0]


def test_read_returns_a_copy():
    block = MemoryBlock(4, [1, 2, 3, 4])
    out = block.read(0, 4)
    out[0] = 999
    assert block.read(0, 1) == [1]


def test_set_single_slot():
    block = MemoryBlock(4, fill=0)
    block.set(1, 42)
    assert block.snapshot() == [0, 42, 0, 0]


@pytest.mark.parametrize(
    "address, count",
    [(0, 5), (4, 1), (3, 2), (-1, 1), (2, 3)],
)
def test_out_of_range_read_raises_illegal_data_address(address, count):
    block = MemoryBlock(4)
    with pytest.raises(IllegalDataAddress):
        block.read(address, count)


def test_zero_size_block_rejects_any_access():
    block = MemoryBlock(0)
    with pytest.raises(IllegalDataAddress):
        block.read(0, 1)
    with pytest.raises(IllegalDataAddress):
        block.write(0, [1])


def test_out_of_range_write_raises_and_does_not_mutate():
    block = MemoryBlock(4, [1, 2, 3, 4])
    with pytest.raises(IllegalDataAddress):
        block.write(2, [10, 20, 30])  # would touch index 4
    assert block.snapshot() == [1, 2, 3, 4]


# --------------------------------------------------------------------------------------
# Device / DataStore
# --------------------------------------------------------------------------------------


def test_build_device_sizes_all_regions():
    device = build_device(
        1, "meter", coils=8, discrete_inputs=4, holding_registers=16, input_registers=2
    )
    assert len(device.coils) == 8
    assert len(device.discrete_inputs) == 4
    assert len(device.holding_registers) == 16
    assert len(device.input_registers) == 2
    assert device.name == "meter"


def test_unconfigured_region_defaults_to_size_zero():
    device = build_device(1)
    assert len(device.coils) == 0
    with pytest.raises(IllegalDataAddress):
        device.coils.read(0, 1)


def test_datastore_lookup_and_iteration():
    store = DataStore([build_device(1), build_device(7)])
    assert store.get(1).unit_id == 1
    assert store.get(7).unit_id == 7
    assert store.get(99) is None
    assert sorted(store.units) == [1, 7]
    assert len(store) == 2
    assert {d.unit_id for d in store} == {1, 7}


def test_datastore_rejects_duplicate_unit_id():
    store = DataStore([build_device(1)])
    with pytest.raises(ValueError):
        store.add(build_device(1))


def test_device_has_lock():
    import asyncio

    assert isinstance(build_device(1).lock, asyncio.Lock)


def test_device_is_constructible_directly():
    device = Device(
        unit_id=5,
        name="direct",
        coils=MemoryBlock(2, fill=False),
        discrete_inputs=MemoryBlock(0, fill=False),
        holding_registers=MemoryBlock(2, [7, 8]),
        input_registers=MemoryBlock(0),
    )
    assert device.holding_registers.read(0, 2) == [7, 8]
