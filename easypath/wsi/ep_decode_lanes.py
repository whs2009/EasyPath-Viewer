from __future__ import annotations

from collections import deque

from contextlib import contextmanager

from threading import Condition

class _LaneBudget:
    def __init__(self):
        self.changed = Condition()
        self.available = 2
        self.waiters = deque()

    @contextmanager
    def take(self, requested):
        if type(requested) is not int or requested not in (1, 2):
            raise ValueError("Invalid EP decode lane request.")
        with self.changed:
            if self.waiters or not self.available:
                token = object()
                self.waiters.append(token)
                try:
                    self.changed.wait_for(lambda: self.waiters[0] is token and self.available > 0)
                finally:
                    self.waiters.remove(token)
                    self.changed.notify_all()
            width = min(requested, self.available)
            self.available -= width
        try:
            yield width
        finally:
            with self.changed:
                self.available += width
                self.changed.notify_all()

decode_lanes = _LaneBudget().take
