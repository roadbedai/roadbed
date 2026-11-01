"""pytest plugin for CI: any outbound socket connection fails the run. Load with -p no_network.

Proves the watch tests replay cassettes only. Loopback stays allowed.
"""

from __future__ import annotations

import socket

_connect = socket.socket.connect


def _guarded(self: socket.socket, address, *a, **kw):  # type: ignore[no-untyped-def]
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "::1", "localhost") and self.family != socket.AF_UNIX:
        raise RuntimeError(f"network blocked in replay-only tests: {address!r}")
    return _connect(self, address, *a, **kw)


def pytest_configure(config) -> None:  # type: ignore[no-untyped-def]
    socket.socket.connect = _guarded  # type: ignore[method-assign]
