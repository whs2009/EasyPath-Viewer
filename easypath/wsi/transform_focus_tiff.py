from __future__ import annotations

from contextlib import ExitStack

import base64

import os

from pathlib import Path

import xml.etree.ElementTree as ET

import numpy as np

from PIL import Image

import tifffile

from .ep_format import EPError

from .ep_format import EPMetadata

from .ep_format import MAX_FILE_BYTES

from .ep_format import MAX_METADATA

from .ep_format import integer

from .ep_format import pyramid_levels

from .focal_stack import FocusStack

from .transform_source import _Color

OME = "{http://www.openmicroscopy.org/Schemas/OME/2016-06}"

UM = "\u00b5m"

def _microns(element, key):
    if key not in element.attrib:
        return None
    if element.get(key+"Unit") != UM:
        raise EPError("This OME adapter requires explicit micrometre units.")
    return float(element.attrib[key])

def _axes(page):
    tag = page.tags.get("ImageDescription")
    if tag is None or tag.count > MAX_METADATA:
        raise EPError("Missing or oversized OME metadata.")
    xml = page.description
    if "<!" in xml:
        raise EPError("Unsupported OME XML declaration.")
    root = ET.fromstring(xml)
    images = root.findall(OME+"Image")
    if root.tag != OME+"OME" or len(images) != 1:
        raise EPError("Expected one OME slide image.")
    px = images[0].find(OME+"Pixels")
    if (px is None or px.get("Type") != "uint8" or px.get("SizeT") != "1" or px.get("SizeC") != "3"
            or len(px.findall(OME+"Channel")) != 1 or px.find(OME+"Channel").get("SamplesPerPixel") != "3"):
        raise EPError("Only one interleaved RGB8 optical path without a time axis is supported.")
    count = integer(int(px.attrib["SizeZ"]), "Z plane count", 2, 20)
    mapping = px.findall(OME+"TiffData")
    if (len(mapping) != 1 or list(mapping[0]) or int(mapping[0].get("PlaneCount","0")) != count
            or any(int(mapping[0].get(key,"0")) != 0 for key in ("FirstC","FirstT","FirstZ","IFD"))):
        raise EPError("A single local, ordered Z-to-IFD map is required.")
    rows = px.findall(OME+"Plane")
    positions = {}
    for row in rows:
        z = integer(int(row.attrib["TheZ"]), "OME Z index", 0, count-1)
        if (z in positions or row.get("TheT") != "0" or row.get("TheC") != "0"
                or any(key in row.attrib for key in ("PositionX","PositionY"))):
            raise EPError("Overlapping or unsupported OME position metadata.")
        positions[z] = _microns(row, "PositionZ")
    if (rows and len(positions) != count) or px.get("PhysicalSizeZ") is not None:
        raise EPError("Partial or spacing-only OME focal metadata is not supported by this adapter.")
    return (int(px.attrib["SizeX"]), int(px.attrib["SizeY"]), _microns(px,"PhysicalSizeX"),
            _microns(px,"PhysicalSizeY"), tuple(positions.get(z) for z in range(count)))

class OMEFocusInput:
    """Strict tiled subset. Never open an external OME companion or guess a page axis."""
    def __init__(self, path, *, assume_srgb=False, source_sha256=None, check_cancel=lambda:None):
        self.check_cancel = check_cancel
        self._owned = ExitStack()
        try:
            self._file = self._owned.enter_context(Path(path).open("rb"))
            self._identity = self.identity()
            size = self._identity[2]
            integer(size, "source length", 16, MAX_FILE_BYTES)
            self.reader = self._owned.enter_context(tifffile.TiffFile(self._file))
            w,h,mx,my,zs = _axes(self.reader.pages[0])
            integer(w,"source width",1,500_000)
            integer(h,"source height",1,500_000)
            self.pages = []
            metas = []
            self.colors = []
            for page in self.reader.pages:
                if (len(self.pages) >= len(zs) or page.shape != (h,w,3) or page.dtype != np.uint8
                        or not page.is_tiled or page.photometric != 2 or page.planarconfig != 1
                        or page.compression not in (1,8,32946) or page.predictor not in (1,2)
                        or page.tiledepth != 1 or page.tags.get(274) and page.tags[274].value != 1
                        or page.is_reduced or not 16 <= page.tilewidth <= 512 or not 16 <= page.tilelength <= 512):
                    raise EPError("Unsupported OME stack page, geometry, orientation or tiled encoding.")
                count = ((w+page.tilewidth-1)//page.tilewidth)*((h+page.tilelength-1)//page.tilelength)
                if len(page.dataoffsets) != count or len(page.databytecounts) != count:
                    raise EPError("Incomplete OME tile index.")
                for at,length in zip(page.dataoffsets,page.databytecounts):
                    if not (16 <= at and 0 < length <= 2*512*512*3 and at+length <= size):
                        raise EPError("OME tile exceeds the bounded source.")
                icc = page.tags.get(34675)
                if icc and icc.count > 1024*1024:
                    raise EPError("Oversized OME color profile.")
                profile = icc.value if icc else None
                self.colors.append(_Color(profile,assume_srgb=assume_srgb))
                metas.append(EPMetadata(levels=pyramid_levels(w,h),mpp_x=mx,mpp_y=my,
                    source_color_policy="icc-to-srgb" if profile else "declared-srgb",
                    source_icc_base64=base64.b64encode(profile).decode() if profile else None,source_sha256=source_sha256))
                self.pages.append(page)
            self.stack = FocusStack(tuple(metas),zs)
            self.stack.validate("ep")
            self.metadata = self.stack.rasters[0]
            self.check()
        except BaseException:
            self.close()
            raise

    def identity(self):
        st = os.fstat(self._file.fileno())
        return st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns

    def check(self):
        if self.identity() != self._identity:
            raise EPError("The source stack changed.")

    def provenance(self):
        return dict(pixel_selection="all-native-focal-planes",axis="scanner-focus",
                    focal_positions_um=list(self.stack.positions_um),color_intent="relative-colorimetric",
                    source_profiles={"volume":self.metadata.source_icc_base64})

    def read(self,z,plane,x,y,w,h):
        integer(z,"focus index",0,len(self.pages)-1)
        integer(plane,"source base level",0,0)
        page = self.pages[z]
        integer(x,"source x",0,page.imagewidth-1)
        integer(y,"source y",0,page.imagelength-1)
        integer(w,"read width",1,min(512,page.imagewidth-x))
        integer(h,"read height",1,min(512,page.imagelength-y))
        self.check()
        out = np.empty((h,w,3),np.uint8)
        tw,th = page.tilewidth,page.tilelength
        for ty in range(y//th,(y+h-1)//th+1):
            for tx in range(x//tw,(x+w-1)//tw+1):
                idx = ty*((page.imagewidth+tw-1)//tw)+tx
                self._file.seek(page.dataoffsets[idx])
                blob = self._file.read(page.databytecounts[idx])
                if len(blob) != page.databytecounts[idx]:
                    raise EPError("Truncated OME tile.")
                data,pos,_ = page.decode(blob,idx)
                sx,sy = tx*tw,ty*th
                if data is None or data.dtype != np.uint8 or data.shape != (1,th,tw,3) or pos != (0,0,sy,sx,0):
                    raise EPError("Unexpected decoded OME tile.")
                l,t,r,b=max(sx,x),max(sy,y),min(sx+tw,x+w),min(sy+th,y+h)
                out[t-y:b-y,l-x:r-x] = data[0,t-sy:b-sy,l-sx:r-sx]
        self.check()
        return self.colors[z].apply(Image.fromarray(out))

    def close(self): self._owned.close()
    def __enter__(self): return self
    def __exit__(self,*args): self.close()
