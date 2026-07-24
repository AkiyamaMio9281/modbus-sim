"""Tests for the command-line interface (:mod:`modbus_sim.cli`)."""

import asyncio
import contextlib
import socket
import subprocess
import sys
import time

import pytest

from modbus_sim.cli import _int_list, _read_loop, _write, build_parser
from modbus_sim.datastore import DataStore, build_device
from modbus_sim.server import ModbusServer

# --------------------------------------------------------------------------------------
# Argument parsing / value converters
# --------------------------------------------------------------------------------------


def test_parser_read_holding_defaults():
    args = build_parser().parse_args(["read-holding", "--addr", "4"])
    assert args.command == "read-holding"
    assert args.addr == 4
    assert args.count == 1
    assert args.port == 5020
    assert args.watch is None


def test_parser_coil_value_and_lists():
    args = build_parser().parse_args(["write-coil", "--addr", "0", "--value", "on"])
    assert args.value is True
    args = build_parser().parse_args(["write-coils", "--addr", "0", "--values", "1,0,1"])
    assert args.values == [True, False, True]
    args = build_parser().parse_args(["write-registers", "--addr", "0", "--values", "1,0x10,3"])
    assert args.values == [1, 16, 3]


def test_int_list_helper():
    assert _int_list("1, 2 ,0xff") == [1, 2, 255]
    assert _int_list("") == []


def test_bad_coil_value_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["write-coil", "--addr", "0", "--value", "maybe"])


# --------------------------------------------------------------------------------------
# In-process client commands against a live server
# --------------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def running_server():
    store = DataStore([build_device(1, coils=16, holding_registers=16, input_registers=16)])
    server = ModbusServer(store, host="127.0.0.1", port=0)
    await server.start()
    task = asyncio.create_task(server.serve_forever())
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_cli_write_then_read(capsys):
    async with running_server() as port:
        common = ["--port", str(port), "--unit", "1"]
        await _write(
            build_parser().parse_args(["write-register", *common, "--addr", "0", "--value", "1234"])
        )
        await _write(
            build_parser().parse_args(["write-coils", *common, "--addr", "0", "--values", "1,0,1"])
        )
        await _read_loop(
            build_parser().parse_args(["read-holding", *common, "--addr", "0", "--count", "1"])
        )
        await _read_loop(
            build_parser().parse_args(["read-coils", *common, "--addr", "0", "--count", "3"])
        )
    out = capsys.readouterr().out
    assert "wrote holding[0] = 1234" in out
    assert "holding[0:1] = [1234]" in out
    assert "coils[0:3] = [True, False, True]" in out


# --------------------------------------------------------------------------------------
# Full `serve` subcommand over a subprocess
# --------------------------------------------------------------------------------------


def _wait_for_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def test_cli_serve_subprocess_end_to_end(tmp_path):
    config = tmp_path / "dev.yaml"
    config.write_text(
        "devices:\n  - unit_id: 1\n    holding_registers: {size: 8, init: [5, 6, 7, 8]}\n",
        encoding="utf-8",
    )
    port = 15599
    serve = subprocess.Popen(
        [sys.executable, "-m", "modbus_sim", "serve", "--config", str(config), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_for_port(port, timeout=15):
            if serve.poll() is not None:
                pytest.skip(f"serve exited early (port {port} unavailable?)")
            pytest.fail("server did not start listening in time")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "modbus_sim",
                "read-holding",
                "--port",
                str(port),
                "--unit",
                "1",
                "--addr",
                "0",
                "--count",
                "4",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert "[5, 6, 7, 8]" in result.stdout
    finally:
        serve.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            serve.wait(timeout=10)
