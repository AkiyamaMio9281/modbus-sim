"""Command-line entry point: the ``serve`` server plus client read/write subcommands.

modbus-sim serve --config examples/office_building.yaml
modbus-sim read-holding --unit 1 --addr 0 --count 4
modbus-sim read-input   --unit 1 --addr 0 --count 3 --watch 1
modbus-sim write-register --unit 1 --addr 0 --value 275
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from modbus_sim.client import ModbusClient, ModbusExceptionError
from modbus_sim.config import load_datastore
from modbus_sim.server import DEFAULT_PORT, ModbusServer, configure_logging

# command -> (client method name, human label) for the four read subcommands.
READ_COMMANDS = {
    "read-coils": ("read_coils", "coils"),
    "read-discrete": ("read_discrete_inputs", "discrete"),
    "read-holding": ("read_holding_registers", "holding"),
    "read-input": ("read_input_registers", "input"),
}

_TRUE_WORDS = {"1", "on", "true", "yes"}
_FALSE_WORDS = {"0", "off", "false", "no"}


def _coil_value(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in _TRUE_WORDS:
        return True
    if lowered in _FALSE_WORDS:
        return False
    raise argparse.ArgumentTypeError(f"expected on/off (got {text!r})")


def _int_list(text: str) -> list[int]:
    return [int(part, 0) for part in text.split(",") if part.strip() != ""]


def _coil_list(text: str) -> list[bool]:
    return [_coil_value(part) for part in text.split(",") if part.strip() != ""]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modbus-sim", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the device simulator")
    serve.add_argument("--config", required=True, help="path to a device YAML file")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--log-level", default="INFO")

    def add_client_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        p.add_argument("--unit", type=int, default=1)

    for name in READ_COMMANDS:
        p = sub.add_parser(name, help=f"read {READ_COMMANDS[name][1]}")
        add_client_args(p)
        p.add_argument("--addr", type=int, required=True)
        p.add_argument("--count", type=int, default=1)
        p.add_argument(
            "--watch",
            type=float,
            default=None,
            metavar="SECONDS",
            help="poll every SECONDS until interrupted",
        )

    p = sub.add_parser("write-coil", help="write a single coil")
    add_client_args(p)
    p.add_argument("--addr", type=int, required=True)
    p.add_argument("--value", type=_coil_value, required=True, help="on/off/1/0")

    p = sub.add_parser("write-register", help="write a single holding register")
    add_client_args(p)
    p.add_argument("--addr", type=int, required=True)
    p.add_argument("--value", type=lambda s: int(s, 0), required=True)

    p = sub.add_parser("write-coils", help="write multiple coils")
    add_client_args(p)
    p.add_argument("--addr", type=int, required=True)
    p.add_argument("--values", type=_coil_list, required=True, help="comma-separated 0/1")

    p = sub.add_parser("write-registers", help="write multiple holding registers")
    add_client_args(p)
    p.add_argument("--addr", type=int, required=True)
    p.add_argument("--values", type=_int_list, required=True, help="comma-separated ints")

    return parser


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def _run_serve(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    store, bindings = load_datastore(args.config)
    server = ModbusServer(store, host=args.host, port=args.port, bindings=bindings)
    print(f"modbus-sim serving {len(store)} device(s) on {args.host}:{args.port} (Ctrl+C to stop)")
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        print("\nshutting down")
    return 0


async def _read_loop(args: argparse.Namespace) -> None:
    method_name, label = READ_COMMANDS[args.command]
    async with ModbusClient(args.host, args.port, unit=args.unit) as client:
        method = getattr(client, method_name)
        while True:
            values = await method(args.addr, args.count)
            end = args.addr + args.count
            print(f"{_now()} unit={args.unit} {label}[{args.addr}:{end}] = {values}")
            if args.watch is None:
                return
            await asyncio.sleep(args.watch)


async def _write(args: argparse.Namespace) -> None:
    async with ModbusClient(args.host, args.port, unit=args.unit) as client:
        if args.command == "write-coil":
            await client.write_coil(args.addr, args.value)
            print(f"{_now()} unit={args.unit} wrote coil[{args.addr}] = {args.value}")
        elif args.command == "write-register":
            await client.write_register(args.addr, args.value)
            print(f"{_now()} unit={args.unit} wrote holding[{args.addr}] = {args.value}")
        elif args.command == "write-coils":
            await client.write_coils(args.addr, args.values)
            print(f"{_now()} unit={args.unit} wrote coils[{args.addr}:] = {args.values}")
        elif args.command == "write-registers":
            await client.write_registers(args.addr, args.values)
            print(f"{_now()} unit={args.unit} wrote holding[{args.addr}:] = {args.values}")


def _run_client(args: argparse.Namespace) -> int:
    coro = _read_loop(args) if args.command in READ_COMMANDS else _write(args)
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        return 0
    except ModbusExceptionError as exc:
        print(f"modbus error: {exc}", file=sys.stderr)
        return 1
    except (TimeoutError, ConnectionRefusedError, OSError) as exc:
        print(f"connection error: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _run_serve(args)
    return _run_client(args)
