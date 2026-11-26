from __future__ import annotations

import math

import numpy as np

from PIL import Image

from easypath.core.enums import WSIReaderBackend

from easypath.wsi.ep_format import EPError

from easypath.wsi.ep_format import MAX_REGION_PIXELS

from easypath.wsi.ep_format import integer

from easypath.wsi.reader import PreviewRequest

from easypath.wsi.reader import SlideMetadata

from easypath.wsi.reader import SlideReader

from easypath.wsi.reader import SlideReaderError

from easypath.wsi.reader import _coerce_preview_request

from easypath.wsi.reader import resolve_local_wsi_path

class TransformRasterReader(SlideReader):
    slide_format = "ep"
    reader_backend = WSIReaderBackend.EP

    def __init__(self):
        self._file = None
        self._path = None

    def _open_container(self, path):
        raise NotImplementedError()

    def open(self, uri_or_path: str):
        path = resolve_local_wsi_path(uri_or_path)
        try:
            opened = self._open_container(path)
        except (OSError, EPError) as exc:
            raise SlideReaderError("Invalid or unavailable transformed slide.") from exc
        self.close()
        self._file = opened
        self._path = path.resolve()
        return self

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._path = None

    def _opened(self):
        if self._file is None:
            raise SlideReaderError("No transformed slide is opened.")
        return self._file

    def get_metadata(self) -> SlideMetadata:
        slide = self._opened()
        meta = slide.metadata()
        path = self._path
        return SlideMetadata(
            path=str(path), uri=path.as_uri(), slide_format=self.slide_format, reader_backend=self.reader_backend,
            width_px=meta.levels[0].width, height_px=meta.levels[0].height,
            level_count=len(meta.levels),
            level_dimensions=[[p.width, p.height] for p in meta.levels],
            level_downsamples=[float(2**i) for i in range(len(meta.levels))],
            base_magnification=meta.objective_power, mpp_x=meta.mpp_x, mpp_y=meta.mpp_y,
            properties={"transform.color_space": meta.color_space},
            metadata_extra={"transform_schema_version": meta.schema_version,
                            "transform_encoding_profile": meta.profile,
                            "transform_maximum_nrmse_percent": meta.maximum_nrmse_percent or 0,
                            "transform_channel_bits": meta.channel_bits or 8,
                            "associated_image_roles": [p.name for p in meta.associated],
                            "source_sha256": meta.source_sha256, "frame_count": 1,
                            **({"frames":len(slide.stack.rasters),"frame_count":len(slide.stack.rasters),
                                "frame_axis":"z","focal_planes":list(slide.stack.positions_um),
                                "frame_navigation":{"axis":"z","count":len(slide.stack.rasters),"initial_index":0}}
                               if hasattr(slide,"focus_view") else {})},
        )

    def read_native_region_rgb(self, x: int, y: int, width: int, height: int, *, level: int = 0) -> np.ndarray:
        try:
            slide=self._opened()
            if hasattr(slide,"focus_view"): slide=slide.focus_view(0)
            return slide.read_region(level, x, y, width, height)
        except EPError as exc:
            raise SlideReaderError("Transformed native region could not be verified.") from exc

    def read_region_rgb(self, x_40x: int, y_40x: int, z: int, width: int, height: int) -> np.ndarray:
        return self.read_region_rgb_frame(x_40x,y_40x,z,width,height,frame_index=0)

    def read_region_rgb_frame(self,x_40x,y_40x,z,width,height,*,frame_index=0):
        try:
            integer(x_40x, "x", -(1 << 53), 1 << 53)
            integer(y_40x, "y", -(1 << 53), 1 << 53)
            integer(z, "zoom", -32, 32)
            integer(width, "width", 1, MAX_REGION_PIXELS)
            integer(height, "height", 1, MAX_REGION_PIXELS)
            if width * height > MAX_REGION_PIXELS:
                raise EPError("Transformed output region exceeds its pixel limit.")
            slide = self._opened()
            if hasattr(slide,"focus_view"):
                slide = slide.focus_view(frame_index)
            else:
                integer(frame_index,"focal index",0,0)
            meta = slide.metadata()
            scale = meta.objective_power / 40.0 if meta.objective_power is not None else 1.0
            step = 2.0 ** (12 - z) * scale
            level = min(max(0, math.floor(math.log2(step))), len(meta.levels) - 1)
            factor = step / (2**level)
            x, y = x_40x * scale / (2**level), y_40x * scale / (2**level)
            if factor == 1.0 and x.is_integer() and y.is_integer():
                return slide.read_region(level, int(x), int(y), width, height)
            return self._sample(slide, level, x, y, factor, width, height)
        except EPError as exc:
            raise SlideReaderError("Transformed region could not be verified.") from exc

    @staticmethod
    def _resample_block(source, size, extent):
        image = Image.fromarray(source)
        return np.asarray(image.transform(size, Image.Transform.EXTENT, extent,
            Image.Resampling.BILINEAR, fillcolor=(255, 255, 255)))

    @classmethod
    def _sample(cls, slide, level, x, y, factor, width, height):
        plane = slide.metadata().levels[level]
        output = np.full((height, width, 3), 255, dtype=np.uint8)
        # Output blocks plus a source-pixel halo keep fractional zooms bounded
        # and prevent interpolation seams at the public 1024px tile boundary.
        for oy in range(0, height, 512):
            for ox in range(0, width, 512):
                w, h = min(512, width - ox), min(512, height - oy)
                sx, sy = x + ox * factor, y + oy * factor
                ex, ey = sx + w * factor, sy + h * factor
                left, top = max(0, math.floor(sx) - 1), max(0, math.floor(sy) - 1)
                right, bottom = min(plane.width, math.ceil(ex) + 1), min(plane.height, math.ceil(ey) + 1)
                if left >= right or top >= bottom:
                    continue
                source = slide.read_region(level, left, top, right - left, bottom - top)
                output[oy:oy+h, ox:ox+w] = cls._resample_block(source, (w, h),
                    (sx - left, sy - top, ex - left, ey - top))
        slide.metadata()
        return output

    def read_associated_rgb(self, role: str, *, max_size: int = 1024) -> np.ndarray:
        integer(max_size, "associated size", 1, 4096)
        try:
            slide = self._opened()
            if not hasattr(slide,"read_associated_image"):
                raise EPError("No associated image in this stack.")
            pixels = slide.read_associated_image(role)
        except (KeyError, EPError) as exc:
            raise SlideReaderError("Transformed associated image is unavailable or invalid.") from exc
        image = Image.fromarray(pixels)
        image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        return np.ascontiguousarray(image)

    def read_slide_macro_rgb(self, *, max_size: int = 1024) -> np.ndarray:
        roles = {p.name for p in self._opened().metadata().associated}
        for role in ("macro", "label"):
            if role in roles:
                return self.read_associated_rgb(role, max_size=max_size)
        raise SlideReaderError("Transformed macro/label image is not available.")

    def read_preview_rgb(self, request: PreviewRequest | int | None = None, *, max_size: int | None = None) -> np.ndarray:
        req = _coerce_preview_request(request, max_size=max_size)
        roles = {p.name for p in self._opened().metadata().associated}
        if req.prefer_native_preview and not req.uses_explicit_geometry() and "overview" in roles:
            return self.read_associated_rgb("overview", max_size=req.max_size)
        return super().read_preview_rgb(req)

    def read_preview_rgb_frame(self,request=None,*,max_size=None,frame_index=0):
        if not hasattr(self._opened(),"focus_view"):
            return super().read_preview_rgb_frame(request,max_size=max_size,frame_index=frame_index)
        req = _coerce_preview_request(request,max_size=max_size)
        plan = self.make_preview_plan(req,return_format="rgb")
        return self.read_region_rgb_frame(0,0,int(plan.z),int(plan.width),int(plan.height),frame_index=frame_index)
