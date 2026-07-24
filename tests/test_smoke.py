"""Smoke test: the package imports and exposes a version."""

import modbus_sim


def test_package_imports() -> None:
    assert isinstance(modbus_sim.__version__, str)
    assert modbus_sim.__version__
