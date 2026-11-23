"""Loaded automatically in every Python process of the test suite (PYTHONPATH).

Blocks network connections to anything but this computer, so the suite proves
that see_also works offline: no model download, no Hugging Face request, nothing.
Connections to 127.0.0.1 / ::1 (the test server) and local sockets stay allowed.
"""
import socket

_LOKAL = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"}
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _pruefen(addr):
    if isinstance(addr, tuple) and addr and str(addr[0]) not in _LOKAL:
        raise OSError(f"see_also tests: network access blocked ({addr[0]})")


def connect(self, addr):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _pruefen(addr)
    return _connect(self, addr)


def connect_ex(self, addr):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _pruefen(addr)
    return _connect_ex(self, addr)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
_getaddrinfo = socket.getaddrinfo


def getaddrinfo(host, *a, **k):
    if host is not None and str(host) not in _LOKAL:
        raise socket.gaierror(f"see_also tests: name lookup blocked ({host})")
    return _getaddrinfo(host, *a, **k)


socket.getaddrinfo = getaddrinfo
