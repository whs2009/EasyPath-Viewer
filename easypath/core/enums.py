from __future__ import annotations

from enum import Enum

class WSIReaderBackend(str, Enum):
    EP = "ep"
    TIFFFILE = "tifffile"
    DICOM = "dicom"
    OPENSLIDE = "openslide"
    TIFFSLIDE = "tiffslide"
    PYVIPS = "pyvips"
    VENDOR_SDK = "vendor_sdk"
    CPP_BINDING = "cpp_binding"
    UNKNOWN = "unknown"
