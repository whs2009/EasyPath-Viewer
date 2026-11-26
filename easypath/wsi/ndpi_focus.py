from __future__ import annotations

from collections import OrderedDict

from contextlib import ExitStack

import base64

import math

import os

from pathlib import Path

from threading import RLock

import numpy as np

from PIL import Image

import tifffile

from .ep_format import EPError

from .ep_format import EPLevel

from .ep_format import EPMetadata

from .ep_format import MAX_FILE_BYTES

from .ep_format import MAX_REGION_PIXELS

from .ep_format import integer

from .ep_format import pyramid_levels

from .focal_stack import FocusStack

from .transform_source import _Color

def _index(reader):
    grouped = {}
    aux = []
    for i, page in enumerate(reader.pages):
        if i >= 512:
            raise EPError("Too many NDPI image directories.")
        lens = page.tags.valueof(65421)
        if lens in (-1, -2):
            aux.append("macro" if lens == -1 else "scanner-tissue-map")
            continue
        z = page.tags.valueof(65424)
        if lens is None or not math.isfinite(lens) or lens <= 0 or type(z) is not int:
            raise EPError("Unsupported NDPI image or incomplete focal metadata.")
        levels = grouped.setdefault(z,{})
        if lens in levels or len(grouped)>20:
            raise EPError("Duplicate focal level or unsupported number of NDPI planes.")
        levels[lens]=page
    return grouped,aux

def has_ndpi_focus(path):
    # Ordinary NDPI stays on the existing vendor/OpenSlide reader.
    try:
        with tifffile.TiffFile(path) as tf:
            if not tf.is_ndpi: return False
            seen=set()
            for i,page in enumerate(tf.pages):
                if i>=512: raise EPError("Too many NDPI image directories.")
                if (page.tags.valueof(65421) or 0)>0:
                    seen.add(page.tags.valueof(65424))
                    if len(seen)>1:return True
            return False
    except tifffile.TiffFileError:
        return False

def micron(page, tag):
    unit={2:25400.,3:10000.}.get(page.tags.valueof(296))
    value=page.tags.valueof(tag)
    if not unit or not value:return None
    n,d=value
    if not n or not d:raise EPError("Invalid NDPI calibration.")
    return unit*d/n

class NDPIFocus:
    def __init__(self,path,*,assume_srgb=False,source_sha256=None,check_cancel=lambda:None):
        self.check_cancel=check_cancel
        self._owned=ExitStack()
        self._cache=OrderedDict()
        self._lock=RLock()
        self._cached_bytes=0
        try:
            self.file=self._owned.enter_context(Path(path).open("rb"))
            self._identity=self.identity()
            size=self._identity[2]
            integer(size,"NDPI file size",16,MAX_FILE_BYTES)
            tf=self._owned.enter_context(tifffile.TiffFile(self.file))
            self.tf=tf
            if not tf.is_ndpi:raise EPError("Expected a Hamamatsu NDPI.")
            grouped,self.auxiliary=_index(tf)
            if not 2<=len(grouped)<=20:raise EPError("Expected a multi-focus NDPI.")
            zs=sorted(grouped)
            lenses=sorted(grouped[zs[0]],reverse=True)
            first=grouped[zs[0]][lenses[0]]
            self.pages=[]
            g=first.imagewidth,first.imagelength
            levels=pyramid_levels(*g)
            mx,my=micron(first,282),micron(first,283)
            base_icc=None
            for zi,z in enumerate(zs):
                if sorted(grouped[z],reverse=True)!=lenses:raise EPError("Incomplete NDPI focus pyramid.")
                pages=[grouped[z][lens] for lens in lenses]
                if micron(pages[0],282)!=mx or micron(pages[0],283)!=my:raise EPError("Different NDPI plane calibration.")
                for i,p in enumerate(pages):
                    w,h=p.imagewidth,p.imagelength
                    tw,th=p.tilewidth,p.tilelength
                    if (p.shape!=(h,w,3) or p.dtype!=np.uint8 or p.compression!=7 or p.planarconfig!=1
                        or p.photometric not in (2,6) or p.tags.valueof(274,1)!=1 or not p.is_tiled
                        or not (0<tw<=65535 and 0<th<=512 and tw*th<=512*512)
                        or lenses[i]!=lenses[0]/2**i or w!=max(1,g[0]//2**i) or h!=max(1,g[1]//2**i)
                        or any(p.tags.valueof(k)!=first.tags.valueof(k) for k in (65422,65423))
                        or p.tags.valueof(65441,0)!=0):
                        raise EPError("Unsupported NDPI focal geometry or RGB encoding.")
                    count=math.ceil(w/tw)*math.ceil(h/th)
                    if len(p.dataoffsets)!=count or len(p.databytecounts)!=count or not p.jpegheader:
                        raise EPError("Incomplete NDPI JPEG restart index.")
                    if any(at<8 or not 0<n<=4*1024**2 or at+n>size for at,n in zip(p.dataoffsets,p.databytecounts,strict=True)):
                        raise EPError("Invalid NDPI compressed tile range.")
                    tag=p.tags.get(34675)
                    if tag and tag.count>1024**2:raise EPError("Oversized NDPI color profile.")
                    icc=tag.value if tag else None
                    if zi==i==0:base_icc=icc
                    if icc!=base_icc:raise EPError("Different NDPI focal color profiles.")
                self.pages.append(pages)
            self.color=_Color(base_icc,assume_srgb=assume_srgb,image_role="NDPI focal planes")
            self.metadata=EPMetadata(levels=levels,objective_power=float(lenses[0]),mpp_x=mx,mpp_y=my,
                source_sha256=source_sha256,source_color_policy="icc-to-srgb" if base_icc else "declared-srgb",
                source_icc_base64=base64.b64encode(base_icc).decode() if base_icc else None)
            self.stack=FocusStack((self.metadata,)*len(zs),tuple(z/1000 for z in zs))
            self.stack.validate("ep")
            # Old NDPI viewers and annotations used frame zero for the nominal focal plane.
            center=zs.index(0) if 0 in zs else 0
            self.viewer_order=(center,*(i for i in range(len(zs)) if i!=center))
            self.check()
        except BaseException:
            self.close()
            raise

    def identity(self):
        st=os.fstat(self.file.fileno())
        return st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns

    def check(self):
        self.check_cancel()
        if self.identity()!=self._identity:raise EPError("NDPI source changed.")

    def provenance(self):
        return dict(pixel_selection="all-native-focal-planes",axis="scanner-focus",
                    focal_positions_um=list(self.stack.positions_um),source_profiles={"volume":self.metadata.source_icc_base64},
                    color_intent="relative-colorimetric",source_only_auxiliary=list(self.auxiliary))

    def read(self,z,plane,x,y,w,h):
        integer(plane,"native focal base",0,0)
        integer(w,"tile width",1,512);integer(h,"tile height",1,512)
        g=self.metadata.levels[0]
        integer(x,"tile x",0,g.width-w);integer(y,"tile y",0,g.height-h)
        return self.region(z,0,x,y,w,h)

    def region(self,z,level,x,y,w,h):
        integer(z,"focus index",0,len(self.pages)-1)
        p=self.pages[z][integer(level,"focus level",0,len(self.pages[z])-1)]
        if w<=0 or h<=0 or w*h>MAX_REGION_PIXELS:raise EPError("Unbounded NDPI region.")
        with self._lock:
            self.check()
            out=np.full((h,w,3),255,np.uint8)
            l,t=max(0,x),max(0,y);r,b=min(p.imagewidth,x+w),min(p.imagelength,y+h)
            tw,th=p.tilewidth,p.tilelength
            if r<=l or b<=t:return out
            for ty in range(t//th,(b-1)//th+1):
                for tx in range(l//tw,(r-1)//tw+1):
                    i=ty*math.ceil(p.imagewidth/tw)+tx
                    key=z,level,i
                    tile=self._cache.get(key)
                    if tile is None:
                        self.file.seek(p.dataoffsets[i])
                        blob=self.file.read(p.databytecounts[i])
                        if len(blob)!=p.databytecounts[i]:raise EPError("Truncated NDPI tile.")
                        raw,pos,_=p.decode(blob,i,jpegheader=p.jpegheader)
                        if raw is None or raw.dtype!=np.uint8 or raw.shape!=(1,th,tw,3) or pos!=(0,0,ty*th,tx*tw,0):
                            raise EPError("Invalid NDPI tile geometry.")
                        tile=raw[0]
                        self._cache[key]=tile
                        self._cached_bytes+=tile.nbytes
                        while self._cached_bytes>16*1024**2:self._cached_bytes-=self._cache.popitem(last=False)[1].nbytes
                    else:self._cache.move_to_end(key)
                    sx,sy=tx*tw,ty*th
                    a,c=max(l,sx),max(t,sy);d,e=min(r,sx+tw),min(b,sy+th)
                    out[c-y:e-y,a-x:d-x]=tile[c-sy:e-sy,a-sx:d-sx]
            self.check()
            return self.color.apply(Image.fromarray(out)) if self.color.transform else out

    def close(self):
        self._cache.clear()
        self._owned.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()

class _View:
    def __init__(self,source,z):
        self.source,self.z=source,z
        self.meta=source.metadata.model_copy(update={"levels":tuple(EPLevel(width=p.imagewidth,height=p.imagelength)
                                                                    for p in source.pages[z])})
    def metadata(self):return self.meta
    def read_region(self,level,x,y,w,h):return self.source.region(self.z,level,x,y,w,h)

class _Container:
    def __init__(self,path):
        self.source=NDPIFocus(path,assume_srgb=True)
        order=self.source.viewer_order
        self.stack=FocusStack(self.source.stack.rasters,tuple(self.source.stack.positions_um[i] for i in order))
    def focus_view(self,index):
        order=self.source.viewer_order
        return _View(self.source,order[integer(index,"NDPI viewer plane",0,len(order)-1)])
    def metadata(self):return self.focus_view(0).metadata()
    def close(self):self.source.close()

from .transform_reader import TransformRasterReader

from easypath.core.enums import WSIReaderBackend

class NDPIFocusReader(TransformRasterReader):
    slide_format="ndpi"
    reader_backend=WSIReaderBackend.TIFFFILE
    def _open_container(self,path):return _Container(path)

    def get_metadata(self):
        meta=super().get_metadata()
        extra=meta.metadata_extra
        src=self._opened().source
        return meta.model_copy(update=dict(vendor_name="Hamamatsu",
            properties={"hamamatsu.focus_reader":"NDPI JPEG restart tiles"},
            metadata_extra={k:extra[k] for k in ("frames","frame_count","frame_axis","focal_planes","frame_navigation")}
            | {"ndpi_auxiliary":list(src.auxiliary)}))

    def read_slide_macro_rgb(self,*,max_size=1024):
        from .reader import SlideReaderError
        src=self._opened().source
        integer(max_size,"macro size",1,4096)
        with src._lock:
            src.check()
            for p in src.tf.pages:
                if p.tags.valueof(65421)==-1:
                    if p.imagewidth*p.imagelength>MAX_REGION_PIXELS or p.shape!=(p.imagelength,p.imagewidth,3) or p.dtype!=np.uint8:
                        raise SlideReaderError("Unsupported NDPI macro.")
                    im=Image.fromarray(p.asarray(maxworkers=1))
                    im.thumbnail((max_size,max_size),Image.Resampling.LANCZOS)
                    return np.asarray(im).copy()
        raise SlideReaderError("NDPI macro unavailable.")
