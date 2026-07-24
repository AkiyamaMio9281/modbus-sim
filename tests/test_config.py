"""Unit tests for YAML config loading, validation, and data-store construction."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from modbus_sim.config import Config, build_datastore, load_config, load_datastore

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "office_building.yaml"


# --------------------------------------------------------------------------------------
# The shipped example must load and build cleanly
# --------------------------------------------------------------------------------------


def test_example_config_loads_and_builds():
    store, bindings = load_datastore(EXAMPLE)
    assert sorted(store.units) == [1, 2, 3]
    # temp_sensor input registers: 3 generators; meter: 4; vfd: 3 -> 10 total.
    assert len(bindings) == 10


def test_example_seeds_generator_slots_at_t0():
    store, _ = load_datastore(EXAMPLE)
    temp = store.get(1)
    # sine(mean=250) at t=0 -> 250; constant pressure -> 1013.
    assert temp.input_registers.read(0, 1) == [250]
    assert temp.input_registers.read(2, 1) == [1013]


def test_example_holding_register_init_and_writability():
    store, _ = load_datastore(EXAMPLE)
    assert store.get(1).holding_registers.read(0, 1) == [250]  # setpoint
    assert store.get(3).holding_registers.read(0, 1) == [1500]  # vfd target speed


def test_example_coils_and_discrete_inits_are_bools():
    store, _ = load_datastore(EXAMPLE)
    vfd = store.get(3)
    assert vfd.coils.read(0, 1) == [True]
    assert vfd.discrete_inputs.read(0, 1) == [True]


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def _config(**device):
    return {"devices": [{"unit_id": 1, "name": "d", **device}]}


def test_minimal_config_defaults_regions_to_zero():
    config = Config.model_validate(_config())
    store, bindings = build_datastore(config)
    assert len(store.get(1).holding_registers) == 0
    assert bindings == []


@pytest.mark.parametrize("unit_id", [0, 248, -1])
def test_unit_id_out_of_range_rejected(unit_id):
    with pytest.raises(ValidationError):
        Config.model_validate({"devices": [{"unit_id": unit_id}]})


def test_duplicate_unit_id_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({"devices": [{"unit_id": 1}, {"unit_id": 1}]})


def test_init_longer_than_size_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate(_config(holding_registers={"size": 2, "init": [1, 2, 3]}))


def test_generator_addr_out_of_region_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate(
            _config(
                input_registers={
                    "size": 2,
                    "generators": [{"addr": 5, "fn": "constant", "args": {"value": 1}}],
                }
            )
        )


def test_generators_on_bit_region_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate(
            _config(
                coils={
                    "size": 4,
                    "generators": [{"addr": 0, "fn": "constant", "args": {"value": 1}}],
                }
            )
        )


def test_bit_region_init_must_be_zero_or_one():
    with pytest.raises(ValidationError):
        Config.model_validate(_config(coils={"size": 2, "init": [0, 5]}))


def test_register_init_out_of_uint16_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate(_config(holding_registers={"size": 1, "init": [70000]}))


def test_unknown_generator_function_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate(
            _config(input_registers={"size": 1, "generators": [{"addr": 0, "fn": "triangle"}]})
        )


def test_bad_generator_args_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate(
            _config(
                input_registers={
                    "size": 1,
                    "generators": [{"addr": 0, "fn": "sine", "args": {"mean": 1}}],
                }
            )
        )


def test_unknown_key_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate(_config(holdingregisters={"size": 1}))  # typo'd key


def test_empty_file_is_valid_empty_config(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    config = load_config(path)
    assert config.devices == []
