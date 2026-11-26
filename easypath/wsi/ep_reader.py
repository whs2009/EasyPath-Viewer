from __future__ import annotations

from pathlib import Path

import numpy as np

from .ep_container import EPFile

from .ep_format import EPError

from .ep_quality import _native_pixels

from .reader import SlideReaderError

from .transform_reader import TransformRasterReader

class EPReader(TransformRasterReader):
    @staticmethod
    def _resample_block(source, size, extent):
        resample = getattr(_native_pixels(), "bilinear_extent_rgb8", None)
        if resample is not None and max(source.shape[:2]) <= 2048:
            return resample(np.ascontiguousarray(source), *size, *extent)
        return TransformRasterReader._resample_block(source, size, extent)

    def _open_container(self, path):
        from .ep_focus import open_ep_path
        return open_ep_path(path)

    def cache_info(self):
        info = self._opened().cache_info()
        return dict(enabled=info["limit_bytes"] > 0, backend=self.__class__.__name__, **info)

    def clear_cache(self):
        if self._file is not None:
            self._file.clear_cache()

    def open_stream(self, stream, *, source_path):
        """Use an admitted descriptor; source_path is metadata, not an operand."""
        path = Path(source_path)
        if not path.is_absolute():
            raise SlideReaderError("An absolute admitted source identity is required.")
        try:
            from .ep_focus import STACK_MAGIC,EPFocusFile
            stream.seek(0)
            opened = EPFocusFile(stream=stream) if stream.read(len(STACK_MAGIC)) == STACK_MAGIC else EPFile.from_stream(stream)
        except (OSError, EPError) as exc:
            raise SlideReaderError("Invalid or unavailable transformed slide.") from exc
        self.close()
        self._file, self._path = opened, path
        return self
