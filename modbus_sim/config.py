"""YAML device configuration, validated with pydantic (SPEC §5).

``load_config`` parses and validates a YAML file into a :class:`Config`; ``build_datastore``
turns a validated config into a runtime :class:`~modbus_sim.datastore.DataStore` plus the
list of :class:`~modbus_sim.generators.GeneratorBinding` the server ticks each second.
Unknown keys are rejected (``extra="forbid"``) so config typos fail loudly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from modbus_sim.datastore import DataStore, Device, MemoryBlock
from modbus_sim.generators import GeneratorBinding, make_generator

GeneratorName = Literal["constant", "sine", "random_walk"]

#: Region attribute names that may carry generators (they produce uint16 values).
REGISTER_REGIONS = ("holding_registers", "input_registers")
BIT_REGIONS = ("coils", "discrete_inputs")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneratorSpec(_Model):
    addr: int = Field(ge=0, le=0xFFFF)
    fn: GeneratorName
    args: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_args(self) -> GeneratorSpec:
        # Building the generator here surfaces bad/missing args at config-load time.
        try:
            make_generator(self.fn, self.args)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid args for generator {self.fn!r}: {exc}") from None
        return self


class RegionSpec(_Model):
    size: int = Field(default=0, ge=0, le=0x10000)
    init: list[int] | None = None
    generators: list[GeneratorSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_bounds(self) -> RegionSpec:
        if self.init is not None and len(self.init) > self.size:
            raise ValueError(f"init has {len(self.init)} values but region size is {self.size}")
        for spec in self.generators:
            if spec.addr >= self.size:
                raise ValueError(f"generator addr {spec.addr} is outside region size {self.size}")
        return self


class DeviceSpec(_Model):
    unit_id: int = Field(ge=1, le=247)
    name: str = ""
    coils: RegionSpec = Field(default_factory=RegionSpec)
    discrete_inputs: RegionSpec = Field(default_factory=RegionSpec)
    holding_registers: RegionSpec = Field(default_factory=RegionSpec)
    input_registers: RegionSpec = Field(default_factory=RegionSpec)

    @model_validator(mode="after")
    def _check_regions(self) -> DeviceSpec:
        for name in BIT_REGIONS:
            region: RegionSpec = getattr(self, name)
            if region.generators:
                raise ValueError(f"generators are not allowed on bit region {name!r}")
            if region.init is not None and any(v not in (0, 1) for v in region.init):
                raise ValueError(f"{name} init values must be 0 or 1")
        for name in REGISTER_REGIONS:
            region = getattr(self, name)
            if region.init is not None and any(not 0 <= v <= 0xFFFF for v in region.init):
                raise ValueError(f"{name} init values must be in 0..65535")
        return self


class Config(_Model):
    devices: list[DeviceSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_unit_ids(self) -> Config:
        seen: set[int] = set()
        for device in self.devices:
            if device.unit_id in seen:
                raise ValueError(f"duplicate unit_id {device.unit_id}")
            seen.add(device.unit_id)
        return self


def load_config(path: str | Path) -> Config:
    """Read and validate a YAML config file."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)


def _build_block(region: RegionSpec, *, bits: bool) -> MemoryBlock:
    fill: int | bool = False if bits else 0
    values: list[int | bool] | None = None
    if region.init is not None:
        values = [bool(v) for v in region.init] if bits else list(region.init)
    return MemoryBlock(region.size, values, fill=fill)


def build_datastore(config: Config) -> tuple[DataStore, list[GeneratorBinding]]:
    """Materialize a validated config into a data store and its generator bindings."""
    devices: list[Device] = []
    bindings: list[GeneratorBinding] = []
    for spec in config.devices:
        blocks = {
            "coils": _build_block(spec.coils, bits=True),
            "discrete_inputs": _build_block(spec.discrete_inputs, bits=True),
            "holding_registers": _build_block(spec.holding_registers, bits=False),
            "input_registers": _build_block(spec.input_registers, bits=False),
        }
        device = Device(
            unit_id=spec.unit_id,
            name=spec.name or f"device-{spec.unit_id}",
            coils=blocks["coils"],
            discrete_inputs=blocks["discrete_inputs"],
            holding_registers=blocks["holding_registers"],
            input_registers=blocks["input_registers"],
        )
        devices.append(device)
        for region_name in REGISTER_REGIONS:
            block = blocks[region_name]
            for gspec in getattr(spec, region_name).generators:
                binding = GeneratorBinding(
                    device=device,
                    block=block,
                    address=gspec.addr,
                    generator=make_generator(gspec.fn, gspec.args),
                    label=f"{device.name}.{region_name}[{gspec.addr}]",
                )
                binding.update(0.0)  # seed the slot with the generator's t=0 value
                bindings.append(binding)
    return DataStore(devices), bindings


def load_datastore(path: str | Path) -> tuple[DataStore, list[GeneratorBinding]]:
    """Convenience: load a config file and build its data store + generator bindings."""
    return build_datastore(load_config(path))
