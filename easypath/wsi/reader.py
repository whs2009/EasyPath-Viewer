from __future__ import annotations

import math

import os

from easypath.checkpoint_io import native_file_path

from io import BytesIO

import re

from abc import ABC

from abc import abstractmethod

from pathlib import Path

from typing import Any

from urllib.parse import unquote

from urllib.parse import urlparse

import numpy as np

from pydantic import BaseModel

from pydantic import Field

from easypath.core.coordinates import wsi_40x_to_native_xy

from easypath.core.coordinates import z_to_downsample

from easypath.core.enums import WSIReaderBackend

from easypath.image.serial_media import DICOM_WSI_SOP_CLASS_UID

from easypath.image.serial_media import discover_dicom_series

from easypath.image.serial_media import select_dicom_series

from easypath.image.dicom_source_identity import series_source_equipment

SUPPORTED_OPENSLIDE_SUFFIXES = {
    ".svs": "svs",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".mrxs": "mrxs",
    ".scn": "scn",
    ".bif": "bif",
    ".dcm": "dcm",
    ".vms": "vms",
    ".vmu": "vmu",
    ".svslide": "svslide",
    ".czi": "czi",
}

VENDOR_SUFFIXES = {
    ".ndpi": "ndpi",
    ".isyntax": "isyntax",
}

SUPPORTED_WSI_SUFFIXES = {**SUPPORTED_OPENSLIDE_SUFFIXES, **VENDOR_SUFFIXES, ".ep": "ep"}

class SlideReaderError(RuntimeError):
    """Base exception for WSI reader errors."""

class SlideReaderBackendUnavailable(SlideReaderError):
    """Raised when a requested backend is not available in this build."""

class SlideMetadata(BaseModel):
    """Backend-neutral WSI metadata.

    ``SlideMetadata`` describes what a backend found. It can then be converted
    into the canonical :class:`EasyPathWSIInput` schema object used by the rest
    of EasyPath.
    """

    path: str
    uri: str
    slide_format: str
    reader_backend: WSIReaderBackend
    width_px: int
    height_px: int
    level_count: int = 1
    level_dimensions: list[list[int]] = Field(default_factory=list)
    level_downsamples: list[float] = Field(default_factory=list)
    base_magnification: float | None = 40.0
    mpp_x: float | None = None
    mpp_y: float | None = None
    vendor_name: str | None = None
    scanner_model: str | None = None
    properties: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    metadata_extra: dict[str, Any] = Field(default_factory=dict)

class PreviewRequest(BaseModel):
    """Low-resolution whole-slide preview request.

    A preview is anchored at the WSI origin and covers the whole level-0/40x
    reference frame as closely as possible. By default, EasyPath chooses an
    EasyPath zoom and output size so the longest preview edge is at most
    ``max_size``. Advanced callers may provide explicit ``z``, ``width``, and
    ``height`` when they need deterministic preview geometry.
    """

    max_size: int = 1024
    z: int | None = None
    width: int | None = None
    height: int | None = None
    request_id: str | None = None
    prefer_native_preview: bool = True
    cache_key: str | None = None

    def uses_explicit_geometry(self) -> bool:
        return self.z is not None or self.width is not None or self.height is not None

class PreviewExecutionPlan(BaseModel):
    """Execution summary for preview generation.

    This object is intentionally small but explicit so a future tile service can
    decide whether to read a native SDK overview, OpenSlide associated image,
    cached JPEG, or fallback low-resolution region.
    """

    z: int
    width: int
    height: int
    max_size: int
    prefer_native_preview: bool = True
    return_format: str = "rgb"  # rgb or jpeg
    cache_key: str | None = None
    note: str | None = None

def _coerce_preview_request(request: PreviewRequest | int | None = None, *, max_size: int | None = None) -> PreviewRequest:
    """Normalize preview request input while preserving old ``max_size`` API."""
    if isinstance(request, PreviewRequest):
        if max_size is not None:
            data = request.model_dump()
            data["max_size"] = int(max_size)
            return PreviewRequest(**data)
        return request
    if isinstance(request, int):
        return PreviewRequest(max_size=int(request))
    if request is None:
        return PreviewRequest(max_size=int(max_size or 1024))
    raise TypeError("Preview request must be PreviewRequest, int max_size, or None.")

def choose_preview_geometry(
    width_px: int,
    height_px: int,
    *,
    base_magnification: float | None = 40.0,
    max_size: int = 1024,
    z: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> tuple[int, int, int]:
    """Choose ``(z, width, height)`` for a whole-slide preview.

    If explicit geometry is supplied, it is validated and returned. Otherwise,
    EasyPath picks the highest zoom whose output longest edge is <= ``max_size``
    under the ``z=12 -> 40x`` convention. Dimensions are native level-0 pixels;
    magnification maps their extent to the 40x region coordinate system.
    """
    if max_size <= 0:
        raise ValueError("max_size must be positive")
    slide_w = max(1, int(width_px))
    slide_h = max(1, int(height_px))

    if z is not None or width is not None or height is not None:
        if z is None or width is None or height is None:
            raise ValueError("Explicit preview geometry requires z, width, and height together.")
        if int(width) <= 0 or int(height) <= 0:
            raise ValueError("Explicit preview width and height must be positive.")
        return int(z), int(width), int(height)

    objective = float(base_magnification or 40.0)
    if not math.isfinite(objective) or objective <= 0:
        objective = 40.0
    slide_w *= 40.0 / objective
    slide_h *= 40.0 / objective
    if not math.isfinite(slide_w) or not math.isfinite(slide_h):
        raise ValueError("Preview reference dimensions must be finite.")
    needed_downsample = max(slide_w / float(max_size), slide_h / float(max_size), 1.0)
    zoom_offset = max(0, int(math.ceil(math.log2(needed_downsample))))
    chosen_z = 12 - zoom_offset
    downsample = z_to_downsample(chosen_z)
    out_w = max(1, int(math.ceil(slide_w / downsample)))
    out_h = max(1, int(math.ceil(slide_h / downsample)))
    return int(chosen_z), int(out_w), int(out_h)

def make_preview_execution_plan(
    metadata: SlideMetadata,
    request: PreviewRequest | int | None = None,
    *,
    max_size: int | None = None,
    return_format: str = "rgb",
) -> PreviewExecutionPlan:
    """Create a preview execution plan from metadata and request settings."""
    req = _coerce_preview_request(request, max_size=max_size)
    z, width, height = choose_preview_geometry(
        int(metadata.width_px),
        int(metadata.height_px),
        base_magnification=metadata.base_magnification,
        max_size=int(req.max_size),
        z=req.z,
        width=req.width,
        height=req.height,
    )
    cache_key = req.cache_key or f"preview:z{z}:w{width}:h{height}:max{req.max_size}:{return_format}"
    return PreviewExecutionPlan(
        z=z,
        width=width,
        height=height,
        max_size=int(req.max_size),
        prefer_native_preview=bool(req.prefer_native_preview),
        return_format=str(return_format),
        cache_key=cache_key,
    )

def validate_rgb_array(array: np.ndarray, *, width: int | None = None, height: int | None = None) -> np.ndarray:
    """Validate and normalize a backend RGB array.

    EasyPath WSI backends must return Python/PyTorch-friendly RGB arrays with
    shape ``(height, width, 3)`` and dtype ``uint8``.
    """
    arr = np.asarray(array, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise SlideReaderError(f"Expected RGB array with shape (H, W, 3), got {arr.shape}.")
    if height is not None and arr.shape[0] != int(height):
        raise SlideReaderError(f"Expected RGB height {height}, got {arr.shape[0]}.")
    if width is not None and arr.shape[1] != int(width):
        raise SlideReaderError(f"Expected RGB width {width}, got {arr.shape[1]}.")
    return np.ascontiguousarray(arr)

def encode_rgb_to_jpeg(array: np.ndarray, *, quality: int = 90) -> bytes:
    """Encode an RGB numpy array as JPEG bytes.

    This is the shared Python fallback for JPEG output. Future C++ backends may
    override JPEG encoding for speed, but the public API stays the same.
    """
    quality = int(quality)
    if quality < 1 or quality > 100:
        raise ValueError("JPEG quality must be between 1 and 100.")
    arr = validate_rgb_array(array)
    from PIL import Image

    image = Image.fromarray(arr, mode="RGB")
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()

_ASSOCIATED_MACRO_NAME_PRIORITY: tuple[tuple[str, int], ...] = (
    ("macro", 0),
    ("slide", 1),
    ("label", 2),
    ("barcode", 3),
)

def _associated_macro_name_rank(name: Any) -> int | None:
    lowered = str(name).lower()
    for token, rank in _ASSOCIATED_MACRO_NAME_PRIORITY:
        if token in lowered:
            return rank
    return None

def _resize_pil_image_to_max_rgb(image: Any, max_size: int) -> Any:
    from PIL import Image

    limit = max(1, int(max_size))
    rgb = image.convert("RGB")
    width, height = map(int, getattr(rgb, "size", (0, 0)))
    if max(width, height) > limit:
        resampling = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
        rgb = rgb.copy()
        rgb.thumbnail((limit, limit), resampling)
    return rgb

_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[a-zA-Z]:(?:[\\/]|$)")

def _looks_like_windows_drive_path(value: str) -> bool:
    """Return True for local Windows drive paths before URI parsing.

    ``urllib.parse.urlparse`` treats ``C:\\path`` and ``C:/path`` as if
    ``C`` were a URI scheme.  EasyPath must accept these as local WSI files on
    Windows, so callers need this guard before checking URI schemes.
    """
    return bool(_WINDOWS_DRIVE_PATH_RE.match(value))

def resolve_local_wsi_path(uri_or_path: str) -> Path:
    """Resolve a local WSI path or ``file://`` URI.

    Phase 1.4 only defines local-file reader behavior. Object storage, PACS,
    DICOMweb, and remote connector loading are represented by schema objects but
    are not read directly yet. Windows drive-letter paths such as
    ``C:\\data\\case.svs`` are local paths, not non-local URI schemes.
    """
    value = str(uri_or_path)
    if _looks_like_windows_drive_path(value):
        return Path(value)

    parsed = urlparse(value)
    if parsed.scheme == "file":
        path_text = unquote(parsed.path)
        if len(path_text) >= 4 and path_text[0] == "/" and path_text[2] == ":":
            path_text = path_text[1:]
        return Path(path_text)
    if parsed.scheme in {"", None}:
        return Path(value)
    raise ValueError(
        f"Unsupported non-local URI scheme '{parsed.scheme}'. Phase 1.4 only reads local WSI files."
    )

def _path_to_file_uri(path: Path) -> str:
    return path.resolve().as_uri()

def slide_format_from_path(path: str | Path) -> str:
    """Return normalized WSI format name from file suffix."""
    suffix = Path(path).suffix.lower()
    if suffix not in SUPPORTED_WSI_SUFFIXES:
        raise ValueError(
            f"Unsupported WSI suffix '{suffix}'. Supported: "
            f"{', '.join(sorted(SUPPORTED_WSI_SUFFIXES))}."
        )
    return SUPPORTED_WSI_SUFFIXES[suffix]

class SlideReader(ABC):

    @abstractmethod
    def open(self, uri_or_path: str) -> 'SlideReader':
        """Open a WSI and return ``self`` for chaining."""

    @abstractmethod
    def close(self) -> None:
        """Close the currently opened slide and release backend resources."""

    @abstractmethod
    def get_metadata(self) -> SlideMetadata:
        """Return backend-neutral slide metadata."""

    @abstractmethod
    def read_region_rgb(self, x_40x: int, y_40x: int, z: int, width: int, height: int) -> np.ndarray:
        """Read an RGB region using EasyPath 40x-reference top-left coordinates."""

    def read_region_rgb_frame(self, x_40x: int, y_40x: int, z: int, width: int, height: int, *, frame_index: int=0) -> np.ndarray:
        """Read one serial-media frame without mutating shared reader state."""
        if int(frame_index) != 0:
            raise SlideReaderError('This whole-slide source has only one focal plane.')
        return self.read_region_rgb(x_40x, y_40x, z, width, height)

    def _preview_zoom_and_size(self, max_size: int=1024) -> tuple[int, int, int]:
        """Choose an EasyPath zoom and output size for a whole-slide preview.

        This compatibility helper delegates to the typed preview planning
        function added in Phase 1.5n. New code should prefer
        :meth:`make_preview_plan`.
        """
        plan = self.make_preview_plan(PreviewRequest(max_size=max_size))
        return (plan.z, plan.width, plan.height)

    def make_preview_plan(self, request: PreviewRequest | int | None=None, *, max_size: int | None=None, return_format: str='rgb') -> PreviewExecutionPlan:
        """Create a backend-neutral preview execution plan for this slide."""
        return make_preview_execution_plan(self.get_metadata(), request, max_size=max_size, return_format=return_format)

    def read_preview_rgb(self, request: PreviewRequest | int | None=None, *, max_size: int | None=None) -> np.ndarray:
        """Generate a low-resolution RGB preview of the whole slide.

        The old API ``read_preview_rgb(max_size=1024)`` still works. The new
        typed API also accepts :class:`PreviewRequest` for deterministic preview
        planning and future cache/native-preview routing.
        """
        plan = self.make_preview_plan(request, max_size=max_size, return_format='rgb')
        return self.read_region_rgb(0, 0, int(plan.z), int(plan.width), int(plan.height))

    def read_preview_rgb_frame(self, request: PreviewRequest | int | None=None, *, max_size: int | None=None, frame_index: int=0) -> np.ndarray:
        if int(frame_index) != 0:
            raise SlideReaderError('This whole-slide source has only one focal plane.')
        return self.read_preview_rgb(request, max_size=max_size)

    def read_slide_macro_rgb(self, *, max_size: int=1024) -> np.ndarray:
        """Read the slide label/macro associated image when available.

        This intentionally does not fall back to the generated whole-slide
        preview. The viewer uses this path for the physical slide label/macro
        image, which may contain QR codes or printed slide text.
        """
        raise SlideReaderError('Slide macro/label associated image is not available.')

def _parse_float_property(value: Any) -> float | None:
    """Parse OpenSlide property values safely."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _first_float_property(properties: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        parsed = _parse_float_property(properties.get(key))
        if parsed is not None:
            return parsed
    return None

class PythonOpenSlideReader(SlideReader):
    """Required fallback backend using ``openslide-python``.

    EasyPathWSIReader first tries :class:`CppOpenSlideReader`. If the C++
    binding is not installed, this Python implementation is the portable
    fallback. The package dependencies include ``openslide-python`` and
    ``openslide-bin`` so WSI support can work across Windows, WSL, Ubuntu, and
    Docker.
    """

    def __init__(self, openslide_module: Any | None = None) -> None:
        self._openslide_module = openslide_module
        self._slide: Any | None = None
        self._path: Path | None = None
        self._metadata: SlideMetadata | None = None

    def _load_openslide_module(self) -> Any:
        if self._openslide_module is not None:
            return self._openslide_module
        try:
            import openslide  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - environment dependent
            raise SlideReaderBackendUnavailable(
                "Python OpenSlide backend is not available. Install EasyPath with WSI support: "
                'pip install -e ".[wsi]". The package dependencies include '
                "openslide-python and openslide-bin."
            ) from exc
        self._openslide_module = openslide
        return openslide

    def open(self, uri_or_path: str, *, source_path: str | Path | None = None) -> "PythonOpenSlideReader":
        path = resolve_local_wsi_path(uri_or_path)
        operand = resolve_local_wsi_path(str(source_path)) if source_path is not None else path
        if not operand.exists():
            raise FileNotFoundError(f"WSI file does not exist: {path}")
        openslide = self._load_openslide_module()
        try:
            self._slide = openslide.OpenSlide(str(operand))
        except Exception as exc:
            raise SlideReaderError(f"Failed to open WSI with OpenSlide: {path}") from exc
        self._path = path
        self._metadata = self._read_metadata()
        return self

    def close(self) -> None:
        if self._slide is not None:
            close = getattr(self._slide, "close", None)
            if callable(close):
                close()
        self._slide = None
        self._path = None
        self._metadata = None

    def _require_open(self) -> None:
        if self._slide is None or self._path is None:
            raise SlideReaderError("Slide is not opened.")

    def _read_metadata(self) -> SlideMetadata:
        self._require_open()
        assert self._slide is not None
        assert self._path is not None

        properties = dict(getattr(self._slide, "properties", {}) or {})
        width_px, height_px = getattr(self._slide, "dimensions")
        level_dimensions = [list(map(int, dim)) for dim in getattr(self._slide, "level_dimensions", [])]
        level_downsamples = [float(v) for v in getattr(self._slide, "level_downsamples", [])]
        level_count = int(getattr(self._slide, "level_count", len(level_dimensions) or 1))

        objective_power = _first_float_property(
            properties,
            [
                "openslide.objective-power",
                "aperio.AppMag",
                "hamamatsu.SourceLens",
                "tiff.ImageDescription.AppMag",
            ],
        )
        if objective_power is None:
            objective_power = 40.0

        mpp_x = _first_float_property(properties, ["openslide.mpp-x", "aperio.MPP", "hamamatsu.XResolution"])
        mpp_y = _first_float_property(properties, ["openslide.mpp-y", "aperio.MPP", "hamamatsu.YResolution"])
        vendor = properties.get("openslide.vendor")

        return SlideMetadata(
            path=str(self._path),
            uri=_path_to_file_uri(self._path),
            slide_format=slide_format_from_path(self._path),
            reader_backend=WSIReaderBackend.OPENSLIDE,
            width_px=int(width_px),
            height_px=int(height_px),
            level_count=level_count,
            level_dimensions=level_dimensions,
            level_downsamples=level_downsamples,
            base_magnification=objective_power,
            mpp_x=mpp_x,
            mpp_y=mpp_y,
            vendor_name=str(vendor) if vendor is not None else None,
            properties=properties,
        )

    def get_metadata(self) -> SlideMetadata:
        self._require_open()
        if self._metadata is None:
            self._metadata = self._read_metadata()
        return self._metadata

    def _best_level_for_native_downsample(self, desired_downsample: float) -> int:
        """Select a source level for a physical target downsample.

        EasyPath keeps z fixed to physical magnification: z=12 is 40x, z=11 is
        20x, and so on.  If the exact level is not scanned, prefer the nearest
        higher-resolution level (smaller/equal native downsample) and downsample
        from it.  Only when no higher-resolution level exists do we use the
        nearest lower-resolution level and upsample.
        """
        self._require_open()
        assert self._slide is not None
        desired_downsample = max(float(desired_downsample), 1.0)
        metadata = self.get_metadata()
        if not metadata.level_downsamples:
            return 0
        best_higher_index: int | None = None
        best_higher_downsample = -math.inf
        best_lower_index: int | None = None
        best_lower_downsample = math.inf
        epsilon = max(desired_downsample, 1.0) * 1e-9
        for index, raw_downsample in enumerate(metadata.level_downsamples):
            downsample = max(float(raw_downsample), 1e-9)
            if downsample <= desired_downsample + epsilon:
                if downsample > best_higher_downsample:
                    best_higher_downsample = downsample
                    best_higher_index = index
            elif downsample < best_lower_downsample:
                best_lower_downsample = downsample
                best_lower_index = index
        if best_higher_index is not None:
            return int(best_higher_index)
        if best_lower_index is not None:
            return int(best_lower_index)
        return 0

    def read_region_rgb(
        self,
        x_40x: int,
        y_40x: int,
        z: int,
        width: int,
        height: int,
    ) -> np.ndarray:
        """Read an RGB region using EasyPath 40x-reference top-left coordinates."""
        self._require_open()
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        assert self._slide is not None

        metadata = self.get_metadata()
        objective_power = metadata.base_magnification or 40.0
        x_native, y_native = wsi_40x_to_native_xy(x_40x, y_40x, objective_power)

        # The requested output region covers width/height pixels at zoom z. In
        # 40x reference coordinates, that corresponds to z_to_downsample(z)
        # times wider/taller physical coverage. Convert that coverage to native
        # level-0 pixels, then choose an exact or higher-resolution source level
        # for downsampling. If no higher-resolution layer exists, upsample from
        # the nearest lower-resolution source while preserving the requested
        # physical zoom.
        downsample_40x = z_to_downsample(z)
        scale_40_to_native = float(objective_power) / 40.0
        native_coverage_w = float(width) * downsample_40x * scale_40_to_native
        native_coverage_h = float(height) * downsample_40x * scale_40_to_native
        desired_native_downsample = max(native_coverage_w / float(width), 1.0)

        level = self._best_level_for_native_downsample(desired_native_downsample)
        level_downsamples = metadata.level_downsamples or [1.0]
        level_downsample = float(level_downsamples[level]) if level < len(level_downsamples) else 1.0
        read_w = max(1, int(round(native_coverage_w / level_downsample)))
        read_h = max(1, int(round(native_coverage_h / level_downsample)))

        try:
            pil_img = self._slide.read_region(
                (int(round(x_native)), int(round(y_native))),
                int(level),
                (read_w, read_h),
            )
        except Exception as exc:
            raise SlideReaderError(
                f"OpenSlide read_region failed at x={x_40x}, y={y_40x}, z={z}, "
                f"size=({width}, {height})."
            ) from exc

        pil_img = pil_img.convert("RGB")
        if pil_img.size != (int(width), int(height)):
            # Pillow >= 9 exposes Resampling; keep fallback for compatibility.
            resampling = getattr(getattr(pil_img, "Resampling", None), "BILINEAR", None)
            if resampling is None:
                from PIL import Image

                resampling = Image.Resampling.BILINEAR if hasattr(Image, "Resampling") else Image.BILINEAR
            pil_img = pil_img.resize((int(width), int(height)), resample=resampling)
        arr = np.asarray(pil_img, dtype=np.uint8)
        return validate_rgb_array(arr, width=width, height=height)

    def read_preview_rgb(
        self,
        request: PreviewRequest | int | None = None,
        *,
        max_size: int | None = None,
    ) -> np.ndarray:
        """Generate a preview using OpenSlide thumbnail support when possible."""
        self._require_open()
        assert self._slide is not None
        req = _coerce_preview_request(request, max_size=max_size)
        if req.uses_explicit_geometry() or not req.prefer_native_preview:
            return super().read_preview_rgb(req)
        thumbnail = getattr(self._slide, "get_thumbnail", None)
        if callable(thumbnail):
            try:
                pil_img = thumbnail((int(req.max_size), int(req.max_size))).convert("RGB")
                arr = np.asarray(pil_img, dtype=np.uint8)
                return validate_rgb_array(arr)
            except Exception:
                pass
        return super().read_preview_rgb(req)

    def read_preview_jpeg(
        self,
        request: PreviewRequest | int | None = None,
        *,
        max_size: int | None = None,
        quality: int = 85,
    ) -> bytes:
        """Generate a JPEG preview, using OpenSlide thumbnail when available."""
        rgb = self.read_preview_rgb(request, max_size=max_size)
        return encode_rgb_to_jpeg(rgb, quality=quality)

    def _read_associated_macro_rgb(self, max_size: int) -> np.ndarray | None:
        self._require_open()
        assert self._slide is not None
        associated_images = getattr(self._slide, "associated_images", None)
        keys = list(associated_images.keys()) if hasattr(associated_images, "keys") else []
        if not keys:
            return None
        candidates: list[tuple[int, int, str, Any]] = []
        for key in keys:
            rank = _associated_macro_name_rank(key)
            if rank is not None:
                candidates.append((rank, 0, str(key).lower(), key))
        if not candidates:
            return None
        _rank, _area, _name, selected_key = min(candidates, key=lambda item: (item[0], item[2]))
        try:
            image = associated_images[selected_key]
            rgb = _resize_pil_image_to_max_rgb(image, max_size)
            return validate_rgb_array(np.asarray(rgb, dtype=np.uint8))
        except Exception:
            return None

    def read_slide_macro_rgb(self, *, max_size: int = 1024) -> np.ndarray:
        """Read the scanner macro/glass-slide image, then an associated fallback."""
        macro = self._read_associated_macro_rgb(int(max_size))
        if macro is not None:
            return macro
        return super().read_slide_macro_rgb(max_size=max_size)

    def cache_info(self) -> dict[str, Any]:
        self._require_open()
        return {"enabled": False, "backend": "PythonOpenSlideReader"}

    def clear_cache(self) -> None:
        # openslide-python exposes no common cache-clear API across versions.
        return None

class DicomWSIReader(SlideReader):
    """Standards-aware DICOM whole-slide reader backed by ``wsidicom``."""

    def __init__(self, wsidicom_module: Any | None = None) -> None:
        self._wsidicom_module = wsidicom_module
        self._slide: Any | None = None
        self._path: Path | None = None
        self._metadata: SlideMetadata | None = None
        self._series: Any | None = None
        self._levels: list[Any] = []
        self._focal_planes: list[float] = [0.0]
        self._physical_levels: list[Any] = []

    def _load_wsidicom_module(self) -> Any:
        if self._wsidicom_module is not None:
            return self._wsidicom_module
        try:
            import wsidicom  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - installation contract covers this branch.
            raise SlideReaderBackendUnavailable(
                "DICOM WSI requires the pinned wsidicom runtime. Repair or update the EasyPath installation."
            ) from exc
        self._wsidicom_module = wsidicom
        return wsidicom

    @staticmethod
    def _series_for_source(path: Path) -> Any:
        if native_file_path(path).is_file():
            candidates = discover_dicom_series(path.parent)
            selected = next(
                (series for series in candidates if any(item.path == path for item in series.instances)),
                None,
            )
            if selected is None:
                raise SlideReaderError("The selected DICOM file is not an admissible series member.")
        else:
            selected = select_dicom_series(path)
        if selected.sop_class_uid != DICOM_WSI_SOP_CLASS_UID:
            raise SlideReaderError("The selected DICOM series is not a VL Whole Slide Microscopy Image series.")
        return selected

    @staticmethod
    def _size_pair(value: Any) -> tuple[int, int]:
        width = int(getattr(value, "width", 0) or 0)
        height = int(getattr(value, "height", 0) or 0)
        if width <= 0 or height <= 0:
            try:
                width, height = map(int, value)
            except (TypeError, ValueError):
                width, height = 0, 0
        if width <= 0 or height <= 0:
            raise SlideReaderError("DICOM WSI reported invalid image dimensions.")
        return width, height

    @staticmethod
    def _mpp_pair(value: Any) -> tuple[float | None, float | None]:
        if value is None:
            return None, None
        width = getattr(value, "width", None)
        height = getattr(value, "height", None)
        if width is None or height is None:
            try:
                width, height = value
            except (TypeError, ValueError):
                return None, None
        try:
            return float(width), float(height)
        except (TypeError, ValueError):
            return None, None

    def open(self, uri_or_path: str) -> "DicomWSIReader":
        path = resolve_local_wsi_path(uri_or_path)
        if not native_file_path(path).exists():
            raise FileNotFoundError(f"DICOM WSI source does not exist: {path}")
        series = self._series_for_source(path)
        return self._open_series(path, series)

    def _open_series(self, path: Path, series: Any, *, stream_opener=None) -> "DicomWSIReader":
        from easypath.wsi.dicom_metadata import dicom_pyramid_options
        from easypath.wsi.dicom_codecs import JPEG_XL_SYNTAXES, open_jpeg_xl_slide

        module = self._load_wsidicom_module()
        if stream_opener is None and os.name == "nt" and any(
            native_file_path(item.path) != item.path or str(item.path).startswith("\\\\?\\") for item in series.instances
        ):
            # UPath rewrites extended Windows paths. Hand off streams for the exact discovered members instead.
            def stream_opener(source):
                return native_file_path(source).open("rb")
        header_reader = None
        if stream_opener is not None:
            from contextlib import closing
            import pydicom
            def header_reader(source, **kwargs):
                with closing(stream_opener(source)) as stream:
                    return pydicom.dcmread(stream, **kwargs)
        options, evidence = dicom_pyramid_options(series, **({"header_reader": header_reader} if header_reader else {}))
        try:
            if any(item.transfer_syntax_uid in JPEG_XL_SYNTAXES for item in series.instances):
                slide = open_jpeg_xl_slide(module, series,
                    **({"stream_opener": stream_opener} if stream_opener is not None else {}), **options)
                evidence["dicom_display_pixel_conversion"] = (
                    "libjxl RGB decode; fixed declared sample range mapped to RGB8 for display/training. "
                    "Original encoded pixels and precision are preserved."
                )
                evidence["dicom_source_bits_stored"] = sorted({item.bits_stored for item in series.instances})
                evidence["dicom_source_photometric_interpretations"] = sorted({item.photometric_interpretation for item in series.instances})
            elif stream_opener is not None:
                from easypath.wsi.dicom_codecs import open_dicom_stream_slide
                slide = open_dicom_stream_slide(module, series, stream_opener, **options)
            else:
                slide = module.WsiDicom.open([str(item.path) for item in series.instances], **options)
        except Exception as exc:
            raise SlideReaderError(f"Failed to open DICOM WSI series: {path}: {exc}") from exc
        levels_container = getattr(slide, "levels", None)
        levels = list(getattr(levels_container, "levels", []) or [])
        if not levels:
            close = getattr(slide, "close", None)
            if callable(close):
                close()
            raise SlideReaderError("DICOM WSI contains no readable pyramid levels.")
        levels.sort(key=lambda item: int(getattr(item, "level", 0)))
        focal_planes: list[float] = []
        for level in levels:
            for raw_value in list(getattr(level, "focal_planes", []) or []):
                try:
                    focal_planes.append(float(raw_value))
                except (TypeError, ValueError):
                    continue
        self._slide = slide
        self._path = path
        self._series = series
        self._levels = levels
        self._focal_planes = sorted(set(focal_planes)) or [0.0]
        from easypath.wsi.dicom_pyramid import compatible_physical_levels
        self._physical_levels = compatible_physical_levels(slide)
        if self._physical_levels:
            self._levels = [entry.level for entry in self._physical_levels]
            evidence["dicom_pyramid_placement"] = "Explicit physical origins and spacing; compatible cropped levels across pyramid groups"
        self._metadata = self._read_metadata()
        self._metadata.metadata_extra.update(evidence)
        if evidence.get("reported_scan_magnification"):
            self._metadata.base_magnification = evidence["reported_scan_magnification"]
        if evidence.get("render_reference_magnification"):
            self._metadata.base_magnification = evidence["render_reference_magnification"]
        return self

    def close(self) -> None:
        if self._slide is not None:
            close = getattr(self._slide, "close", None)
            if callable(close):
                close()
        self._slide = None
        self._path = None
        self._metadata = None
        self._series = None
        self._levels = []
        self._focal_planes = [0.0]
        self._physical_levels = []

    def _require_open(self) -> None:
        if self._slide is None or self._path is None or self._series is None:
            raise SlideReaderError("DICOM WSI is not opened.")

    def _read_metadata(self) -> SlideMetadata:
        self._require_open()
        assert self._slide is not None
        assert self._path is not None
        assert self._series is not None
        width_px, height_px = self._size_pair(self._slide.size)
        level_dimensions = [list(self._size_pair(level.size)) for level in self._levels]
        level_downsamples = [max(width_px / float(size[0]), height_px / float(size[1])) for size in level_dimensions]
        if self._physical_levels:
            level_downsamples = [entry.downsample for entry in self._physical_levels]
        mpp_x, mpp_y = self._mpp_pair(getattr(self._slide, "mpp", None))
        valid_mpp = [value for value in (mpp_x, mpp_y) if value is not None and value > 0]
        base_magnification = 10.0 / (sum(valid_mpp) / len(valid_mpp)) if valid_mpp else 40.0
        frame_navigation = {
            "axis": "z",
            "count": len(self._focal_planes),
            "initial_index": 0,
        }
        return SlideMetadata(
            path=str(self._path),
            uri=_path_to_file_uri(self._path),
            slide_format="dicom",
            reader_backend=WSIReaderBackend.DICOM,
            width_px=width_px,
            height_px=height_px,
            level_count=len(level_dimensions),
            level_dimensions=level_dimensions,
            level_downsamples=level_downsamples,
            base_magnification=base_magnification,
            mpp_x=mpp_x,
            mpp_y=mpp_y,
            vendor_name="DICOM",
            properties={
                "dicom_sop_class_uid": self._series.sop_class_uid,
                "dicom_modality": self._series.modality,
            },
            metadata_extra={
                "serial_media_kind": "dicom_wsi",
                "dicom_source_equipment": series_source_equipment(self._series),
                "dicom_series_key": self._series.series_key,
                "dicom_source_file_count": self._series.source_file_count,
                "frame_axis": "z",
                "frames": len(self._focal_planes),
                "frame_navigation": frame_navigation,
                "focal_planes": list(self._focal_planes),
            },
        )

    def get_metadata(self) -> SlideMetadata:
        self._require_open()
        assert self._metadata is not None
        return self._metadata

    def _best_level(self, desired_downsample: float) -> tuple[Any, float]:
        metadata = self.get_metadata()
        candidates = list(zip(self._levels, metadata.level_downsamples))
        higher = [item for item in candidates if float(item[1]) <= desired_downsample + 1e-9]
        if higher:
            return max(higher, key=lambda item: float(item[1]))
        return min(candidates, key=lambda item: float(item[1]))

    def _focal_plane(self, frame_index: int) -> float:
        index = int(frame_index)
        if index < 0 or index >= len(self._focal_planes):
            raise SlideReaderError(
                f"Focal-plane index {index} is outside this DICOM WSI's {len(self._focal_planes)} planes."
            )
        return self._focal_planes[index]

    def read_region_rgb(
        self,
        x_40x: int,
        y_40x: int,
        z: int,
        width: int,
        height: int,
    ) -> np.ndarray:
        return self.read_region_rgb_frame(x_40x, y_40x, z, width, height, frame_index=0)

    def read_region_rgb_frame(
        self,
        x_40x: int,
        y_40x: int,
        z: int,
        width: int,
        height: int,
        *,
        frame_index: int = 0,
    ) -> np.ndarray:
        self._require_open()
        if int(width) <= 0 or int(height) <= 0:
            raise ValueError("width and height must be positive")
        assert self._slide is not None
        metadata = self.get_metadata()
        objective_power = float(metadata.base_magnification or 40.0)
        x_native, y_native = wsi_40x_to_native_xy(x_40x, y_40x, objective_power)
        native_downsample = z_to_downsample(z) * objective_power / 40.0
        if self._physical_levels:
            from easypath.wsi.dicom_pyramid import read_physical_region
            try:
                return read_physical_region(self._slide, self._physical_levels, x_native, y_native,
                                            native_downsample, int(width), int(height), self._focal_plane(frame_index))
            except Exception as exc:
                raise SlideReaderError("DICOM WSI physical pyramid read failed.") from exc
        level, level_downsample = self._best_level(native_downsample)
        read_width = max(1, int(round(float(width) * native_downsample / float(level_downsample))))
        read_height = max(1, int(round(float(height) * native_downsample / float(level_downsample))))
        requested_x = int(round(float(x_native) / float(level_downsample)))
        requested_y = int(round(float(y_native) / float(level_downsample)))
        level_width, level_height = self._size_pair(level.size)
        clipped_x = max(0, requested_x)
        clipped_y = max(0, requested_y)
        clipped_right = min(level_width, requested_x + read_width)
        clipped_bottom = min(level_height, requested_y + read_height)
        if clipped_right <= clipped_x or clipped_bottom <= clipped_y:
            return np.full((int(height), int(width), 3), 255, dtype=np.uint8)
        clipped_size = (
            clipped_right - clipped_x,
            clipped_bottom - clipped_y,
        )
        location = (
            clipped_x,
            clipped_y,
        )
        try:
            image = self._slide.read_region(
                location,
                int(getattr(level, "level", 0)),
                clipped_size,
                z=self._focal_plane(frame_index),
            ).convert("RGB")
        except Exception as exc:
            raise SlideReaderError(
                f"DICOM WSI read failed at x={x_40x}, y={y_40x}, z={z}, size=({width}, {height})."
            ) from exc
        from PIL import Image

        if clipped_size != (read_width, read_height):
            padded = Image.new("RGB", (read_width, read_height), (255, 255, 255))
            padded.paste(image, (clipped_x - requested_x, clipped_y - requested_y))
            image = padded
        if image.size != (int(width), int(height)):
            resampling = Image.Resampling.BILINEAR if hasattr(Image, "Resampling") else Image.BILINEAR
            image = image.resize((int(width), int(height)), resample=resampling)
        return validate_rgb_array(np.asarray(image, dtype=np.uint8), width=width, height=height)

    def read_preview_rgb(
        self,
        request: PreviewRequest | int | None = None,
        *,
        max_size: int | None = None,
    ) -> np.ndarray:
        return self.read_preview_rgb_frame(request, max_size=max_size, frame_index=0)

    def read_preview_rgb_frame(
        self,
        request: PreviewRequest | int | None = None,
        *,
        max_size: int | None = None,
        frame_index: int = 0,
    ) -> np.ndarray:
        self._require_open()
        assert self._slide is not None
        req = _coerce_preview_request(request, max_size=max_size)
        if req.uses_explicit_geometry():
            plan = self.make_preview_plan(req, return_format="rgb")
            return self.read_region_rgb_frame(
                0,
                0,
                int(plan.z),
                int(plan.width),
                int(plan.height),
                frame_index=int(frame_index),
            )
        try:
            if self._physical_levels:
                from easypath.wsi.dicom_pyramid import read_physical_region
                metadata = self.get_metadata()
                downsample = max(1.0, metadata.width_px / req.max_size, metadata.height_px / req.max_size)
                return read_physical_region(self._slide, self._physical_levels, 0, 0, downsample,
                                            max(1, round(metadata.width_px/downsample)),
                                            max(1, round(metadata.height_px/downsample)), self._focal_plane(frame_index))
            image = self._slide.read_thumbnail(
                min(int(req.max_size), max(self.get_metadata().width_px, self.get_metadata().height_px)),
                z=self._focal_plane(frame_index),
            ).convert("RGB")
        except Exception as exc:
            raise SlideReaderError("DICOM WSI thumbnail generation failed.") from exc
        return validate_rgb_array(np.asarray(image, dtype=np.uint8))

    def read_preview_jpeg(
        self,
        request: PreviewRequest | int | None = None,
        *,
        max_size: int | None = None,
        quality: int = 85,
    ) -> bytes:
        return encode_rgb_to_jpeg(
            self.read_preview_rgb(request, max_size=max_size),
            quality=quality,
        )

    def read_slide_macro_rgb(self, *, max_size: int = 1024, require_associated: bool = False) -> np.ndarray:
        self._require_open()
        assert self._slide is not None
        for method_name in ("read_overview", "read_label"):
            method = getattr(self._slide, method_name, None)
            if not callable(method):
                continue
            try:
                image = method().convert("RGB")
                return validate_rgb_array(np.asarray(_resize_pil_image_to_max_rgb(image, max_size), dtype=np.uint8))
            except Exception:
                continue
        if require_associated:
            return super().read_slide_macro_rgb(max_size=max_size)
        return self.read_preview_rgb(max_size=max_size)

    def cache_info(self) -> dict[str, Any]:
        self._require_open()
        return {"enabled": False, "backend": "DicomWSIReader"}

class EasyPathWSIReader(SlideReader):
    """Offline dispatch; the General edition never probes a proprietary runtime."""

    def __init__(self, *, prefer_cpp=False, prefer_vendor_sdk=False):
        if prefer_cpp or prefer_vendor_sdk:
            raise SlideReaderError('Proprietary readers are not part of the General edition.')
        self._backend = None
        self._backend_name = None
        self._backend_resolution_reports = []

    @property
    def backend(self):
        return self._backend

    def open(self, uri_or_path):
        path = resolve_local_wsi_path(uri_or_path)
        suffix = path.suffix.lower()
        if suffix == '.isyntax':
            raise SlideReaderError('iSyntax requires a separately authorized edition.')
        if suffix == '.ep':
            from .ep_reader import EPReader
            reader = EPReader()
        elif suffix in {'.dcm', '.dicom'}:
            reader = DicomWSIReader()
        else:
            reader = PythonOpenSlideReader()
            if suffix == '.ndpi':
                from .ndpi_focus import has_ndpi_focus, NDPIFocusReader
                if has_ndpi_focus(path):
                    reader = NDPIFocusReader()
            elif suffix in {'.tif', '.tiff'}:
                from .transform_tiff_reader import is_transform_tiff, TransformTIFFReader
                if is_transform_tiff(path):
                    reader = TransformTIFFReader()
        try:
            reader.open(str(path))
        except BaseException:
            reader.close()
            raise
        self.close()
        self._backend = reader
        self._add_tiff_evidence(path)
        return self

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()
        self._backend = None
        self._backend_name = None

    def _add_tiff_evidence(self, path: Path) -> None:
        if path.suffix.lower() not in {'.tif', '.tiff'} or self._backend is None:
            return
        from easypath.wsi.tiff_associated import tiff_calibration_evidence
        try:
            metadata = self._backend.get_metadata()
            metadata.metadata_extra.update(tiff_calibration_evidence(path, metadata))
        except (OSError, ValueError):
            return

    def get_metadata(self) -> SlideMetadata:
        if self._backend is None:
            raise SlideReaderError('No backend is opened.')
        metadata = self._backend.get_metadata()
        if not self._backend_resolution_reports:
            return metadata
        metadata_extra = dict(metadata.metadata_extra)
        metadata_extra['backend_resolution_reports'] = [dict(item) for item in self._backend_resolution_reports]
        return metadata.model_copy(update={'metadata_extra': metadata_extra})

    def read_region_rgb(self, x_40x: int, y_40x: int, z: int, width: int, height: int) -> np.ndarray:
        if self._backend is None:
            raise SlideReaderError('No backend is opened.')
        return self._backend.read_region_rgb(x_40x, y_40x, z, width, height)

    def read_region_rgb_frame(self, x_40x: int, y_40x: int, z: int, width: int, height: int, *, frame_index: int=0) -> np.ndarray:
        if self._backend is None:
            raise SlideReaderError('No backend is opened.')
        return self._backend.read_region_rgb_frame(x_40x, y_40x, z, width, height, frame_index=int(frame_index))

    def read_slide_macro_rgb(self, *, max_size: int=1024) -> np.ndarray:
        if self._backend is None:
            raise SlideReaderError('No backend is opened.')
        try:
            return self._backend.read_slide_macro_rgb(max_size=max_size)
        except SlideReaderError:
            path = Path(self._backend.get_metadata().path)
            if path.suffix.lower() in {'.tif', '.tiff'}:
                from easypath.wsi.tiff_associated import read_tiff_macro
                macro = read_tiff_macro(path, max_size)
                if macro is not None:
                    return macro
            raise
