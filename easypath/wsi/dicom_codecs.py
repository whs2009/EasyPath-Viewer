from __future__ import annotations

from typing import Any

import numpy as np

JPEG_XL_SYNTAXES = frozenset({
    "1.2.840.10008.1.2.4.110", "1.2.840.10008.1.2.4.111", "1.2.840.10008.1.2.4.112",
})

def require_jpeg_xl_codec() -> Any:
    try:
        import imagecodecs
        if not imagecodecs.JPEGXL.available:
            raise ImportError("libjxl is unavailable")
        return imagecodecs
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "JPEG XL DICOM requires imagecodecs==2025.11.11 with libjxl. "
            "Repair or update the EasyPath installation; source files were not changed."
        ) from exc

class JpegXLDisplayDecoder:
    def __init__(self, bits: int, rows: int, columns: int) -> None:
        if not 1 <= bits <= 16 or min(rows, columns) <= 0 or rows * columns > 16_777_216:
            raise ValueError("Unsupported JPEG XL color precision or tile dimensions.")
        self.bits = bits
        self.shape = (rows, columns, 3)
        self.codec = require_jpeg_xl_codec()

    def decode(self, frame: bytes) -> np.ndarray:
        output = np.empty(self.shape, dtype=np.uint8 if self.bits <= 8 else np.uint16)
        pixels = self.codec.jpegxl_decode(frame, numthreads=1, out=output)
        maximum = (1 << self.bits) - 1
        if pixels.shape != self.shape or pixels.dtype != output.dtype or int(pixels.max()) > maximum:
            raise ValueError("JPEG XL decoded pixels disagree with the declared DICOM sample contract.")
        if self.bits == 8:
            return pixels
        return np.rint(pixels.astype(np.float32) * (255.0 / maximum)).astype(np.uint8)

def open_dicom_stream_slide(module: Any, series: Any, stream_opener: Any, **options: Any) -> Any:
    """Open an exact stream inventory, rejecting individually discarded members."""
    from upath import UPath
    from wsidicom.config import use_settings
    from wsidicom.file.io.wsidicom_io import WsiDicomIO
    from wsidicom.file.wsidicom_file_source import WsiDicomFileSource

    streams, slide = [], None
    try:
        with use_settings(options.get("settings")):
            for instance in series.instances:
                handle = stream_opener(instance.path)
                try:
                    streams.append(WsiDicomIO(handle, UPath(instance.path)))
                except BaseException:
                    handle.close()
                    raise
            source = WsiDicomFileSource(streams)
            if len(source.readers) != len(streams):
                raise ValueError("A declared DICOM instance was rejected.")
            slide = module.WsiDicom(source, **options)
            if any(stream.closed for stream in streams):
                raise ValueError("A declared DICOM instance did not match the slide.")
            return slide
    except BaseException:
        if slide is not None:
            slide.close()
        for stream in streams:
            stream.close()
        raise

def open_jpeg_xl_slide(module: Any, series: Any, *, stream_opener=None, **options: Any) -> Any:
    """Use isolated stream/image-data subclasses; never patch wsidicom globals."""
    require_jpeg_xl_codec()
    from upath import UPath
    from wsidicom.config import use_settings
    from wsidicom.file.io.wsidicom_io import WsiDicomIO
    from wsidicom.file.wsidicom_file_image_data import WsiDicomFileImageData
    from wsidicom.file.wsidicom_file_source import WsiDicomFileSource
    from wsidicom.instance import WsiInstance

    class DisplayStream(WsiDicomIO):
        display_precision: int | None = None
        rejection: str | None = None

        def read_dataset(self, *args: Any, **kwargs: Any) -> Any:
            dataset = super().read_dataset(*args, **kwargs)
            syntax = str(self.file_meta_info.TransferSyntaxUID)
            if syntax in JPEG_XL_SYNTAXES:
                if int(dataset.SamplesPerPixel) != 3:
                    self.rejection = "This JPEG XL WSI display adapter requires three color samples per pixel."
                    raise ValueError(self.rejection)
                bits, allocated = int(dataset.BitsStored), int(dataset.BitsAllocated)
                if (str(dataset.PhotometricInterpretation) not in {"XYB", "RGB", "YBR_RCT"}
                        or allocated not in {8, 16} or not 1 <= bits <= allocated
                        or int(dataset.HighBit) != bits - 1
                        or int(dataset.PixelRepresentation) != 0 or int(dataset.PlanarConfiguration) != 0):
                    self.rejection = "Unsupported JPEG XL color sample contract."
                    raise ValueError(self.rejection)
                self.display_precision = bits
                # This is the decoded display view only; the original header is
                # retained by discovery/metadata and original pixels remain on disk.
                dataset.PhotometricInterpretation = "RGB"
                dataset.BitsAllocated = dataset.BitsStored = 8
                dataset.HighBit = 7
            return dataset

    class DisplayImageData(WsiDicomFileImageData):
        def __init__(self, readers: Any, *args: Any) -> None:
            super().__init__(readers, *args)
            bits = {stream.display_precision for stream in (reader._stream for reader in readers)}
            if bits != {None}:
                if None in bits or len(bits) != 1:
                    raise ValueError("Inconsistent JPEG XL color precision within a concatenation.")
                dataset = readers[0].dataset
                self._decoder = JpegXLDisplayDecoder(bits.pop(), int(dataset.Rows), int(dataset.Columns))

    class DisplaySource(WsiDicomFileSource):
        def _create_instances(self, files: Any, series_uids: Any, series_tile_size: Any = None) -> Any:
            groups = self._group_files(self._filter_files(files, series_uids, series_tile_size))
            for readers in groups.values():
                yield WsiInstance(
                    [reader.dataset for reader in readers],
                    DisplayImageData(readers, self._decoded_frame_cache, self._encoded_frame_cache),
                )

    streams = []
    try:
        with use_settings(options.get("settings")):
            for instance in series.instances:
                handle = stream_opener(instance.path) if stream_opener is not None else instance.path.open("rb")
                try:
                    streams.append(DisplayStream(handle, UPath(instance.path)))
                except BaseException:
                    handle.close()
                    raise
            source = DisplaySource(streams)
            if len(source.readers) != len(streams):
                raise ValueError("A DICOM instance was rejected; refusing an incomplete JPEG XL slide.")
            slide = module.WsiDicom(source, **options)
            if any(stream.closed for stream in streams):
                raise ValueError("A DICOM instance did not match the selected slide.")
            return slide
    except BaseException as exc:
        rejection = next((stream.rejection for stream in streams if stream.rejection), None)
        for stream in streams:
            stream.close()
        if rejection and isinstance(exc, Exception):
            # wsidicom may suppress individual reader errors before rejecting
            # an empty source; retain the actionable sample-contract diagnosis.
            raise ValueError(rejection) from exc
        raise
