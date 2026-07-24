"""modbus-sim: a from-scratch Modbus TCP device simulator and client CLI.

The protocol stack lives in :mod:`modbus_sim.frame` and :mod:`modbus_sim.dispatcher`,
which are pure functions (bytes / dataclass in, bytes / dataclass out, zero IO) so the
overwhelming majority of the test-suite can exercise the protocol without a socket.
"""

__version__ = "0.1.0"
