"""Bounded, thread-safe HTTP connection reuse, isolated by runtime identity."""
from __future__ import annotations

import atexit
from collections import OrderedDict
from contextlib import contextmanager
import os
from threading import Lock

import httpx


class HttpClientPool:
    def __init__(self, maximum=16):
        self.maximum = maximum
        self._lock = Lock()
        self._clients = OrderedDict()

    @staticmethod
    def _close(client):
        client.close()

    @contextmanager
    def acquire(self, identity, *, verify):
        # Factory identity also isolates replaced transports in tests/operators.
        key = (os.getpid(), identity, verify, httpx.Client)
        disposable = False
        with self._lock:
            entry = self._clients.get(key)
            if entry is None:
                if len(self._clients) >= self.maximum:
                    idle = next((k for k, value in self._clients.items() if value[1] == 0), None)
                    if idle is not None:
                        old, _ = self._clients.pop(idle)
                        self._close(old)
                    else:
                        disposable = True
                client = httpx.Client(verify=verify, limits=httpx.Limits(max_connections=16, max_keepalive_connections=8))
                entry = [client, 0]
                if not disposable:
                    self._clients[key] = entry
            entry[1] += 1
            if not disposable:
                self._clients.move_to_end(key)
        try:
            yield entry[0]
        finally:
            with self._lock:
                entry[1] -= 1
                retired = self._clients.get(key) is not entry
                should_close = disposable or (retired and entry[1] == 0)
            if should_close:
                self._close(entry[0])

    def close(self):
        with self._lock:
            entries = list(self._clients.values())
            self._clients.clear()
        for client, active in entries:
            if not active:
                self._close(client)


odoo_http_clients = HttpClientPool()
atexit.register(odoo_http_clients.close)
