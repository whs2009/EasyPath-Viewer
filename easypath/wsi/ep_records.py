from __future__ import annotations

import numpy as np

from .ep_codec import decode_tile

from .ep_codec import decode_precision_tile

from .ep_format import EPError

from .ep_format import MAX_TILE_BYTES

from .ep_format import TILE_SIZE

from .ep_format import integer

from .ep_precision import precision_reference

from .ep_quality import _native_pixels

def split_record(payload):
    if type(payload) is not bytes or not 3 <= len(payload) <= MAX_TILE_BYTES:
        raise EPError('Invalid EP v2 record length.')
    if payload[0] == 1:
        if len(payload) != 4:
            raise EPError('EP solid record must contain exactly RGB8.')
        return 'solid', payload[1:]
    if payload[0] == 0 and payload[1:3] == b'\xff\x0a':
        return 'jxl', payload[1:]
    raise EPError('Unknown or malformed EP v2 tile record.')

def decode_record(payload, valid_width, valid_height, *, channel_bits=None, native=None):
    integer(valid_width, 'valid tile width', 1, TILE_SIZE)
    integer(valid_height, 'valid tile height', 1, TILE_SIZE)
    if channel_bits is not None:
        integer(channel_bits, 'reduced channel bits', 5, 7)
    kind, value = split_record(payload)
    if kind == 'jxl':
        if native is not None and channel_bits is None:
            return decode_tile(value, native=native)
        return decode_precision_tile(value, channel_bits) if channel_bits is not None else decode_tile(value)
    color = np.frombuffer(value, dtype=np.uint8).reshape(1, 1, 3)
    if channel_bits is not None and not np.array_equal(precision_reference(color, channel_bits), color):
        raise EPError('EP solid color disagrees with its declared channel precision.')
    native = _native_pixels()
    if native is not None and hasattr(native, 'solid_rgb8'):
        return native.solid_rgb8(color, valid_width, valid_height)
    pixels = np.full((TILE_SIZE, TILE_SIZE, 3), 255, dtype=np.uint8)
    pixels[:valid_height, :valid_width] = color
    return pixels
