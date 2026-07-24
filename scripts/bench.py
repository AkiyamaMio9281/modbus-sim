"""Concurrency benchmark: N connections each polling at a fixed rate for a duration.

By default it reproduces the SPEC §6 target of 50 connections x 10 req/s x 60 s and reports
the latency percentiles plus the error count. The server runs in-process on an ephemeral
port so the benchmark is fully self-contained and reproducible.

    conda run -n p2 python scripts/bench.py                 # 50 x 10 x 60s
    conda run -n p2 python scripts/bench.py --duration 5    # quick smoke run
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time

from modbus_sim.client import ModbusClient
from modbus_sim.datastore import DataStore, build_device
from modbus_sim.server import ModbusServer


async def _worker(
    port: int, rate: float, duration: float, latencies_ms: list[float], errors: list[int]
) -> int:
    interval = 1.0 / rate
    count = 0
    async with ModbusClient("127.0.0.1", port, unit=1) as client:
        end = time.monotonic() + duration
        next_deadline = time.monotonic()
        while time.monotonic() < end:
            started = time.perf_counter()
            try:
                await client.read_holding_registers(0, 4)
                latencies_ms.append((time.perf_counter() - started) * 1000)
            except Exception:  # noqa: BLE001 - benchmark counts, does not crash
                errors[0] += 1
            count += 1
            next_deadline += interval
            sleep_for = next_deadline - time.monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    return count


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(pct / 100 * len(ordered)))
    return ordered[index]


async def run(args: argparse.Namespace) -> int:
    store = DataStore([build_device(1, holding_registers=16, input_registers=16)])
    store.get(1).holding_registers.write(0, [10, 20, 30, 40])
    server = ModbusServer(store, host="127.0.0.1", port=0)
    await server.start()
    port = server.sockets[0].getsockname()[1]
    server_task = asyncio.create_task(server.serve_forever())

    latencies: list[float] = []
    errors = [0]
    print(
        f"benchmark: {args.connections} connections x {args.rate} req/s x {args.duration}s "
        f"on 127.0.0.1:{port}"
    )
    wall_start = time.monotonic()
    workers = [
        asyncio.create_task(_worker(port, args.rate, args.duration, latencies, errors))
        for _ in range(args.connections)
    ]
    counts = await asyncio.gather(*workers)
    wall = time.monotonic() - wall_start

    server_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await server_task

    total = sum(counts)
    ok = len(latencies)
    print("-" * 60)
    print(f"requests sent    : {total}")
    print(f"successful       : {ok}")
    print(f"errors           : {errors[0]}")
    print(f"throughput       : {total / wall:.0f} req/s over {wall:.1f}s")
    if latencies:
        print(f"latency p50 (ms) : {_percentile(latencies, 50):.2f}")
        print(f"latency p90 (ms) : {_percentile(latencies, 90):.2f}")
        print(f"latency p99 (ms) : {_percentile(latencies, 99):.2f}")
        print(f"latency max (ms) : {max(latencies):.2f}")
    return 1 if errors[0] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--connections", type=int, default=50)
    parser.add_argument("--rate", type=float, default=10.0, help="requests/second per connection")
    parser.add_argument("--duration", type=float, default=60.0, help="seconds")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
