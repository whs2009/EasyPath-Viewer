from __future__ import annotations

from contextlib import contextmanager

import threading

import numpy as np

from .ep_format import EPError

from .ep_format import MAX_TILE_BYTES

from .ep_format import TILE_SIZE

from .ep_decode_lanes import decode_lanes

_decode_context = threading.local()

@contextmanager
def single_threaded_decode():
    """One codec thread per tile while the region reader decodes a pair."""
    previous = getattr(_decode_context, "single_threaded", False)
    _decode_context.single_threaded = True
    try:
        yield
    finally:
        _decode_context.single_threaded = previous

def require_codec():
    try:
        import imagecodecs
        if not imagecodecs.JPEGXL.available:
            raise ImportError("JPEG XL unavailable")
        return imagecodecs
    except (ImportError, AttributeError) as exc:
        raise EPError("EP slides require the EasyPath JPEG XL codec.") from exc

def decode_tile(payload: bytes, *, native=None) -> np.ndarray:
    if type(payload) is not bytes or not 2 <= len(payload) <= MAX_TILE_BYTES:
        raise EPError("Invalid EP tile payload length.")
    # Supplying the exact output array rejects mismatched dimensions, channels,
    # precision and animation shapes before allocating the decoded image.
    requested = 1 if getattr(_decode_context, "single_threaded", False) else 2
    try:
        with decode_lanes(requested) as threads:
            if native is not None:
                pixels = native.decode(payload, 8, threads)
                if (not isinstance(pixels, np.ndarray) or pixels.shape != (TILE_SIZE, TILE_SIZE, 3)
                        or pixels.dtype != np.uint8 or not pixels.flags.c_contiguous):
                    raise EPError("Invalid native EP decode result.")
                return pixels
            output = np.empty((TILE_SIZE, TILE_SIZE, 3), dtype=np.uint8)
            pixels = require_codec().jpegxl_decode(payload, numthreads=threads, keeporientation=True, out=output)
    except (ValueError, RuntimeError) as exc:
        raise EPError("EP tile cannot be decoded into its declared RGB8 storage grid.") from exc
    if pixels.shape != output.shape or pixels.dtype != output.dtype or not np.shares_memory(pixels, output):
        raise EPError("EP decoder output violates the storage grid.")
    return output

def decode_precision_tile(payload: bytes, bits: int) -> np.ndarray:
    from .ep_precision import expand_rgb_codes
    return expand_rgb_codes(decode_tile(payload), bits)
