# modbus-sim

A from-scratch **Modbus TCP** device simulator and client CLI, implemented in pure
Python with `asyncio`. The protocol stack (MBAP framing, function-code dispatch,
byte-order handling, TCP stream re-assembly) is hand-written — no protocol library is
used in the core. `pymodbus` appears only in the interoperability test suite, where a
battle-tested client is used to cross-check our server.

> Modbus was invented in 1979 by Modicon (now Schneider Electric) and is still the de
> facto standard for industrial automation.

Work in progress — see the project plan for the current status.
