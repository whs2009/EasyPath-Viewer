from __future__ import annotations

import numpy as np

from PIL import Image

from .ep_format import EPError

from .ep_format import integer

_EXPANSION_TABLES = {
    bits: (tuple((code * 255 + ((1 << bits) - 1) // 2) // ((1 << bits) - 1)
                 for code in range(1 << bits)) + (0,) * (256 - (1 << bits))) * 3
    for bits in (5, 6, 7)
}

def quantize_rgb(pixels, bits):
    integer(bits, "channel bits", 5, 8)
    if (not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8 or
            pixels.ndim != 3 or pixels.shape[2] != 3 or not pixels.size):
        raise EPError("EP precision requires nonempty RGB8 pixels.")
    if bits == 8:
        return np.ascontiguousarray(pixels)
    maximum = (1 << bits) - 1
    return ((pixels.astype(np.uint16) * maximum + 127) // 255).astype(np.uint8)

def expand_rgb_codes(codes, bits):
    integer(bits, "channel bits", 5, 8)
    if (not isinstance(codes, np.ndarray) or codes.dtype != np.uint8 or
            codes.ndim != 3 or codes.shape[2] != 3 or not codes.size):
        raise EPError("EP precision requires nonempty RGB codes.")
    maximum = (1 << bits) - 1
    if int(codes.max()) > maximum:
        raise EPError("EP decoded samples exceed their declared channel precision.")
    if bits == 8:
        return codes
    # Pillow applies the same integer LUT in native code. Preserve the writable,
    # independently owned RGB array returned by the previous NumPy lookup.
    return np.array(Image.fromarray(codes).point(_EXPANSION_TABLES[bits]), dtype=np.uint8, copy=True)

def precision_reference(pixels, bits):
    return expand_rgb_codes(quantize_rgb(pixels, bits), bits)
