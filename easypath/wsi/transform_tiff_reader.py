from __future__ import annotations

import json

import os

from pathlib import Path

import stat

import threading

import numpy as np

from easypath.core.enums import WSIReaderBackend

from .ep_format import EPError

from .ep_format import EPMetadata

from .ep_format import MAX_FILE_BYTES

from .ep_format import MAX_METADATA

from .ep_format import MAX_REGION_PIXELS

from .ep_format import TILE_SIZE

from .ep_format import integer

from .transform_pyramid import REFERENCE_POLICY

from .transform_reader import TransformRasterReader

from .transform_tiff import TIFF_SCHEMA

def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise EPError("Duplicate TIFF provenance field.")
        result[key] = value
    return result

def _description(page):
    tag = page.tags.get(270)
    if tag is None or tag.count > 2*MAX_METADATA:
        return None
    return tag.value

def is_transform_tiff(path):
    import tifffile
    try:
        with tifffile.TiffFile(path) as reader:
            if reader.pages[0].software == "EasyPath Scan Focus v1":
                return True
            value = _description(reader.pages[0])
            if not isinstance(value,str) or TIFF_SCHEMA not in value:
                return False
            # Claimed transformed TIFFs go through the strict reader even when
            # malformed; they must not fall back to an uncalibrated backend.
            return True
    except (OSError,ValueError,IndexError):
        return False

class TransformTIFFFile:
    def __init__(self,path):
        import tifffile
        self._lock = threading.RLock()
        self._reader = None
        self._file = Path(path).open("rb")
        try:
            info = os.fstat(self._file.fileno())
            if not stat.S_ISREG(info.st_mode) or not 16 <= info.st_size <= MAX_FILE_BYTES:
                raise EPError("Transformed TIFF must be a bounded regular file.")
            self._identity = self._fingerprint()
            self._reader = tifffile.TiffFile(self._file)
            value = _description(self._reader.pages[0])
            if not isinstance(value,str):
                raise EPError("Missing bounded TIFF provenance.")
            document = json.loads(value,object_pairs_hook=_pairs)
            if (type(document) is not dict or set(document) != {"schema","raster","reference_policy"}
                    or document["schema"] != TIFF_SCHEMA or document["reference_policy"] != REFERENCE_POLICY):
                raise EPError("Unsupported TIFF provenance profile.")
            self._metadata = EPMetadata.from_bytes(json.dumps(document["raster"],allow_nan=False).encode("utf-8"))
            self._pages = []
            pages = iter(self._reader.pages)
            for plane_id, plane in enumerate(self._metadata.planes):
                page = next(pages,None)
                if (page is None or page.shape != (plane.height,plane.width,3) or page.dtype != np.uint8
                        or not page.is_tiled or (page.tilewidth,page.tilelength) != (TILE_SIZE,TILE_SIZE)
                        or page.samplesperpixel != 3 or page.photometric != 2 or page.planarconfig != 1
                        or page.compression not in (8,32946) or page.predictor not in (1,2)
                        or len(page.dataoffsets) != plane.tiles or len(page.databytecounts) != plane.tiles
                        or page.tags.get(274) is not None and page.tags[274].value != 1):
                    raise EPError("Invalid transformed TIFF tile geometry or encoding.")
                for offset,length in zip(page.dataoffsets,page.databytecounts):
                    if not 16 <= offset <= info.st_size or not 1 <= length <= TILE_SIZE*TILE_SIZE*6+65536 or length > info.st_size-offset:
                        raise EPError("Transformed TIFF tile range exceeds its bounds.")
                if plane_id >= len(self._metadata.levels) and page.description != plane.name:
                    raise EPError("TIFF associated image role disagrees.")
                self._pages.append(page)
            if next(pages,None) is not None:
                raise EPError("Unexpected transformed TIFF plane.")
            self._check()
        except BaseException:
            self.close()
            raise

    def _fingerprint(self):
        value = os.fstat(self._file.fileno())
        return value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns

    def _check(self):
        if self._file.closed or self._fingerprint() != self._identity:
            raise EPError("Transformed TIFF changed or was closed.")

    def metadata(self):
        with self._lock:
            self._check()
            return self._metadata

    def _tile(self,plane_id,tx,ty):
        plane = self._metadata.planes[plane_id]
        page = self._pages[plane_id]
        index = ty*plane.columns+tx
        with self._lock:
            self._check()
            self._file.seek(page.dataoffsets[index])
            payload = self._file.read(page.databytecounts[index])
            self._check()
            if len(payload) != page.databytecounts[index]:
                raise EPError("Truncated transformed TIFF tile.")
            decoded,position,shape = page.decode(payload,index)
        if (decoded is None or decoded.dtype != np.uint8 or decoded.shape != (1,TILE_SIZE,TILE_SIZE,3)
                or position != (0,0,ty*TILE_SIZE,tx*TILE_SIZE,0)):
            raise EPError("Invalid decoded transformed TIFF tile.")
        return decoded[0]

    def _region(self,plane_id,x,y,width,height):
        self.metadata()
        integer(plane_id,"plane",0,len(self._metadata.planes)-1)
        integer(x,"x",-(1<<53),1<<53)
        integer(y,"y",-(1<<53),1<<53)
        integer(width,"width",1,MAX_REGION_PIXELS)
        integer(height,"height",1,MAX_REGION_PIXELS)
        if width*height > MAX_REGION_PIXELS:
            raise EPError("TIFF region exceeds its pixel limit.")
        plane = self._metadata.planes[plane_id]
        output = np.full((height,width,3),255,dtype=np.uint8)
        l,t,r,b = max(x,0),max(y,0),min(x+width,plane.width),min(y+height,plane.height)
        if l < r and t < b:
            for ty in range(t//TILE_SIZE,(b-1)//TILE_SIZE+1):
                for tx in range(l//TILE_SIZE,(r-1)//TILE_SIZE+1):
                    tile = self._tile(plane_id,tx,ty)
                    sx,sy = tx*TILE_SIZE,ty*TILE_SIZE
                    left,top,right,bottom = max(l,sx),max(t,sy),min(r,sx+TILE_SIZE),min(b,sy+TILE_SIZE)
                    output[top-y:bottom-y,left-x:right-x] = tile[top-sy:bottom-sy,left-sx:right-sx]
        self.metadata()
        return output

    def read_region(self,level,x,y,width,height):
        integer(level,"level",0,len(self._metadata.levels)-1)
        return self._region(level,x,y,width,height)

    def read_associated_image(self,role):
        for index,plane in enumerate(self._metadata.associated,len(self._metadata.levels)):
            if plane.name == role:
                return self._region(index,0,0,plane.width,plane.height)
        raise KeyError(role)

    def close(self):
        with self._lock:
            if self._reader is not None:
                self._reader.close()
            self._file.close()

class TransformTIFFReader(TransformRasterReader):
    slide_format = "tiff"
    reader_backend = WSIReaderBackend.TIFFFILE

    def _open_container(self,path):
        try:
            import tifffile
            with tifffile.TiffFile(path) as reader:
                if reader.pages[0].software == "EasyPath Scan Focus v1":
                    return _FocusTIFF(path)
            return TransformTIFFFile(path)
        except (ValueError,TypeError,IndexError,KeyError,OverflowError) as exc:
            raise EPError("Invalid transformed TIFF container.") from exc

class _FocusTIFF:
    def __init__(self,path):
        from .transform_focus_tiff import OMEFocusInput
        self.source = OMEFocusInput(path)
        try:
            self.stack = self.source.stack
            self.views = []
            lock = threading.RLock()
            for z,root in enumerate(self.source.pages):
                view = TransformTIFFFile.__new__(TransformTIFFFile)
                view._file,view._identity,view._lock = self.source._file,self.source._identity,lock
                view._metadata = self.stack.rasters[z]
                view._pages = [root,*(root.pages or ())]
                if len(view._pages) != len(view._metadata.levels):
                    raise EPError("Invalid focal TIFF pyramid length.")
                for p,g in zip(view._pages,view._metadata.levels):
                    if (p.shape!=(g.height,g.width,3) or p.dtype!=np.uint8 or p.photometric!=2 or p.planarconfig!=1
                            or p.compression not in (8,32946) or p.predictor not in (1,2) or
                            (p.tilewidth,p.tilelength)!=(TILE_SIZE,TILE_SIZE) or len(p.dataoffsets)!=g.tiles or
                            len(p.databytecounts)!=g.tiles or any(not (16<=a and 0<b<=TILE_SIZE*TILE_SIZE*6+65536
                            and a+b<=self.source._identity[2]) for a,b in zip(p.dataoffsets,p.databytecounts))):
                        raise EPError("Invalid focal TIFF SDK directory.")
                self.views.append(view)
        except BaseException:
            self.close()
            raise

    def metadata(self):
        self.source.check()
        return self.stack.rasters[0]
    def focus_view(self,z):
        return self.views[integer(z,"focal index",0,len(self.views)-1)]
    def close(self):
        self.source.close()
