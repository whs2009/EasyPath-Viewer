from __future__ import annotations

from functools import lru_cache

from importlib import import_module

@lru_cache(maxsize=1)
def _native_pixels():
    try:
        return import_module("easypath._ep_pixels")
    except ModuleNotFoundError as exc:
        if exc.name != "easypath._ep_pixels":
            raise
        return None
