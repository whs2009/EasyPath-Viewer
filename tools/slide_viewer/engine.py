"""Read-only local slide session. C++ owns the viewport; the reader uses 40x coordinates."""
import math
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image

from easypath.wsi.reader import DicomWSIReader, EasyPathWSIReader

EXTENSIONS = {".svs",".ndpi",".scn",".mrxs",".bif",".svslide",".tif",".tiff",".ep",".dcm",".dicom",".isyntax"}

def positive(value):
    try:
        number=float(value)
        return number if math.isfinite(number) and number>0 else 0.
    except (ValueError,TypeError):
        return 0.

def combine_glass_images(images,limit):
    if len(images)==1:return np.ascontiguousarray(images[0])
    a,b=images
    gap=4
    horizontal=(a.width+b.width+gap,max(a.height,b.height))
    vertical=(max(a.width,b.width),a.height+b.height+gap)
    across=max(horizontal)<=max(vertical)
    size=horizontal if across else vertical
    sheet=Image.new("RGB",size,(37,37,37))
    sheet.paste(a,(0,0));sheet.paste(b,(a.width+gap,0) if across else (0,a.height+gap))
    sheet.thumbnail((limit,limit),Image.Resampling.LANCZOS)
    return np.ascontiguousarray(sheet)

def describe_slide(path,m,frames,mx,my):
    missing="Not reported"
    e=m.metadata_extra
    def text(v):
        if v is None or v=="":return missing
        if isinstance(v,(tuple,list)):return ", ".join(map(str,v)) if v else missing
        return str(v)
    def pair(x,y):
        if not x and not y:return missing
        return f"X {format(x,'.6g') if x else missing}; Y {format(y,'.6g') if y else missing} micrometers/pixel"
    rows=[]
    def section(s):rows.extend(["",s.upper()])
    def row(k,v):rows.append(f"{k}:  {text(v)}")
    section("Identification")
    for k,v in (("Name",path.name),("Media type","Whole-slide image"),("Format",m.slide_format.upper()),
                ("Reader backend",m.reader_backend.value),("Scanner",m.scanner_model),("Vendor",m.vendor_name),
                ("Dimensions",f"{m.width_px:,} x {m.height_px:,} px"),("Frame/slice count",frames)):row(k,v)
    if e.get("metadata_warnings"):
        section("Metadata warnings")
        for s in e["metadata_warnings"]:row("Check calibration",s)
    section("Physical scale")
    mag=positive(m.base_magnification)
    provenance=e.get("base_magnification_source")
    dicom=e.get("serial_media_kind")=="dicom_wsi"
    if dicom and not (mx or my):mag=0
    objective=("Pixel-spacing-derived magnification (not scanner objective)" if dicom or provenance=="inferred_from_pixel_spacing"
               else "Native image reference magnification" if provenance in {"reader_image_reference","explicit_dicom_image_reference"}
               else "Base magnification (reader)")
    row(objective,f"{mag:g}x" if mag else None);row("Native pixel length",pair(mx,my))
    for k,key,unit in (("Declared lens magnification","reported_scan_magnification","x"),("TIFF description pixel length","reported_description_mpp"," micrometers/pixel")):
        if e.get(key):row(k,str(e[key])+unit)
    row("40x-reference pixel length",pair(mx*mag/40,my*mag/40))
    section("Pyramid")
    row("Pyramid level count",m.level_count)
    for i in range(max(m.level_count,len(m.level_dimensions),len(m.level_downsamples))):
        parts=[]
        if i<len(m.level_dimensions):
            w,h=m.level_dimensions[i];parts.append(f"{w:,} x {h:,} px")
        ds=positive(m.level_downsamples[i]) if i<len(m.level_downsamples) else (1 if i==0 else 0)
        if ds:parts.append(f"{ds:.6g}x downsample")
        if mag and ds:parts.append(f"{mag/ds:.5g}x equivalent")
        row(f"Level {i}","; ".join(parts))
    if dicom:
        section("DICOM whole-slide pyramid")
        for k,key in (("Source files","dicom_source_file_count"),("Original sample precision (bits)","dicom_source_bits_stored"),
                      ("Original color encoding","dicom_source_photometric_interpretations"),("Display conversion","dicom_display_pixel_conversion"),
                      ("Pyramid placement","dicom_pyramid_placement")):row(k,e.get(key))
        row("Focal planes",frames);row("Native pixel spacing",pair(mx,my))
    section("Technical")
    for k,v in (("Coordinate system",e.get("coordinate_system")),("Frame axis",e.get("frame_axis")),("Channels",e.get("channels") or e.get("decoded_channel_count")),
                ("Source color model",e.get("source_color_model") or e.get("photometric_interpretation"))):row(k,v)
    return "\n".join(rows).strip()

class OverviewCache:
    """Process-local scanner RGB only. No reader, annotation or vendor session is retained."""
    def __init__(self, budget=24*1024**2, entries=64):
        self.budget, self.limit = budget, entries
        self.data, self.size = OrderedDict(), 0

    def get(self, key):
        if key in self.data:
            self.data.move_to_end(key)
            return self.data[key]

    def put(self, key, rgb):
        if rgb.nbytes > self.budget or self.limit < 1:
            return
        old = self.data.pop(key, None)
        if old is not None:
            self.size -= old.nbytes
        while self.data and (self.size + rgb.nbytes > self.budget or len(self.data) >= self.limit):
            self.size -= self.data.popitem(last=False)[1].nbytes
        rgb.flags.writeable = False
        self.data[key] = rgb
        self.size += rgb.nbytes


def file_identity(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


class Session:
    def __init__(self, hospital=False, cache=None):
        self.hospital=hospital
        self.reader=None
        self.cache = OverviewCache() if cache is None else cache
        self.cache_id = None

    def open(self, filename):
        path=Path(filename)
        ext=path.suffix.lower()
        if ext not in EXTENSIONS or not path.is_file():
            raise ValueError("Select a local whole-slide file.")
        if ext==".isyntax" and not self.hospital:
            raise ValueError("Philips iSyntax requires the hospital edition.")
        if ext in {".dcm",".dicom"}:
            from pydicom import dcmread
            d=dcmread(path,stop_before_pixels=True,specific_tags=["SOPClassUID"])
            if str(d.get("SOPClassUID",""))!="1.2.840.10008.5.1.4.1.1.77.1.6":
                raise ValueError("Only DICOM whole-slide microscopy is supported.")
        # Other formats may depend on mutable companions or separately verified tiles.
        single_file = ext in {".svs", ".ndpi", ".bif"}
        identity = file_identity(path) if single_file else None
        r=EasyPathWSIReader(prefer_cpp=self.hospital,prefer_vendor_sdk=self.hospital)
        try:
            r.open(str(path))
            m=r.get_metadata()
            backend=m.reader_backend.value
            vendor=str(m.properties.get("openslide.vendor") or m.vendor_name or "")
            if backend in {"openslide","cpp_binding"} and vendor.lower() in {"","generic-tiff"}:
                raise ValueError("A generic tiled image is not a recognized scanner WSI.")
            scale=positive(m.base_magnification or 40)/40
            if not scale or not 0<m.width_px<=1000000 or not 0<m.height_px<=1000000:
                raise ValueError("Unsupported slide dimensions or scale.")
            frames=int(m.metadata_extra.get("frame_count") or m.metadata_extra.get("frames") or 1)
            if not 1<=frames<=4096: raise ValueError("Unsupported frame count.")
            mx,my=positive(m.mpp_x),positive(m.mpp_y)
            if backend in {"openslide","cpp_binding"}:
                mx=positive(m.properties.get("openslide.mpp-x") or m.properties.get("aperio.MPP"))
                my=positive(m.properties.get("openslide.mpp-y") or m.properties.get("aperio.MPP"))
            info=describe_slide(path,m,frames,mx,my)
            dims=(m.width_px/scale,m.height_px/scale,frames,mx*scale,my*scale,scale)
            cache_id = None
            if single_file and frames == 1 and backend in {"openslide", "cpp_binding", "vendor_sdk"}:
                if file_identity(path) != identity:
                    raise ValueError("Slide changed while opening. Reopen it.")
                cache_id = (path.absolute(), identity, backend, dims)
        except BaseException:
            r.close()
            raise
        self.close()
        self.reader,self.meta,self.dims=r,m,dims
        self.cache_id = cache_id
        return dims,info

    def macro(self,limit):
        if not self.reader or not 1<=limit<=1024:raise ValueError("Invalid macro request.")
        path=Path(self.meta.path)
        if path.suffix.lower() in {".svs",".ndpi",".mrxs",".scn",".bif",".svslide",".tif",".tiff"}:
            import openslide
            from openslide import lowlevel
            try:
                with openslide.OpenSlide(str(path)) as slide:
                    keys={k.lower():k for k in slide.associated_images.keys()}
                    images=[]
                    for key in (keys.get("macro"),keys.get("label") or keys.get("barcode")):
                        if key is None:continue
                        w,h=lowlevel.get_associated_image_dimensions(slide._osr,key)
                        if w<1 or h<1 or w*h>16_777_216:raise ValueError("Associated image exceeds preview limit.")
                        im=slide.associated_images[key]
                        im.thumbnail((limit,limit),Image.Resampling.LANCZOS)
                        images.append(im.convert("RGB"))
                    if images:return combine_glass_images(images,limit)
            except openslide.OpenSlideError:
                pass  # The active vendor reader may support a file that OpenSlide cannot.
        backend=self.reader.backend
        if isinstance(backend,DicomWSIReader):
            rgb=backend.read_slide_macro_rgb(max_size=limit,require_associated=True)
        else:
            rgb=self.reader.read_slide_macro_rgb(max_size=limit)
        if rgb.ndim!=3 or rgb.shape[2]!=3 or rgb.dtype!=np.uint8 or not 0<max(rgb.shape[:2])<=limit:
            raise ValueError("Invalid scanner macro dimensions.")
        return np.ascontiguousarray(rgb)

    def render(self,x,y,z,w,h,frame):
        if not self.reader or not 0<=frame<self.dims[2] or not -12<=z<=16 or not (0<w<=4096 and 0<h<=4096 and w*h<=4194304):
            raise ValueError("Invalid viewport or frame.")
        # Bound the SDK input as well as the output, including on single-level slides.
        ratio=2.**(12-z)*self.dims[5]
        levels=[v for v in self.meta.level_downsamples if 0 < v <= max(1.,ratio)]
        pixels_per_output=max(1.,ratio/max(levels or [1.]))
        if w*h*pixels_per_output**2>16_777_216:
            raise ValueError("No suitable overview level. Zoom in to read a smaller region.")
        def decode(x0, y0, width, height):
            rgb=self.reader.read_region_rgb_frame(x0,y0,z,width,height,frame_index=frame)
            if rgb.shape!=(height,width,3) or rgb.dtype!=np.uint8: raise ValueError("Unexpected RGB viewport.")
            return np.ascontiguousarray(rgb)

        step = 2.**(12-z)
        if not self.cache_id or z>12 or math.ceil(self.dims[0]/step)*math.ceil(self.dims[1]/step)>1_048_576:
            return decode(x,y,w,h)

        path, identity, backend, _ = self.cache_id
        if file_identity(path) != identity:
            raise ValueError("Slide changed. Reopen it.")
        box = (x,y,w,h)
        # At an exact OpenSlide pyramid level, aligned regions are crops of the
        # same pixels. Do not substitute a resized overview or a vendor SDK ROI.
        if backend == "openslide" and ratio in self.meta.level_downsamples and x>=0 and y>=0 and not x%step and not y%step:
            level=self.meta.level_downsamples.index(ratio)
            lw,lh=self.meta.level_dimensions[level]
            ix,iy=int(x/step),int(y/step)
            if (lw*lh<=1_048_576 and ix+w<=lw and iy+h<=lh and
                    lw*ratio==self.meta.width_px and lh*ratio==self.meta.height_px):
                box=(0,0,lw,lh)
        key = (self.cache_id, frame, z, box)
        rgb = self.cache.get(key)
        if rgb is None:
            rgb = decode(*box)
            if file_identity(path) != identity:
                raise ValueError("Slide changed while reading. Reopen it.")
            self.cache.put(key, rgb)
        if box!=(x,y,w,h):
            rgb=rgb[iy:iy+h,ix:ix+w]
        return np.ascontiguousarray(rgb)

    def close(self):
        if self.reader:
            self.reader.close()
            self.reader=None

def save_view(filename, rgb):
    path=Path(filename)
    fmt={".png":"PNG",".jpg":"JPEG",".jpeg":"JPEG"}.get(path.suffix.lower())
    if not fmt: raise ValueError("Choose a new .png, .jpg or .jpeg file.")
    # Exclusive creation protects original slides and existing snapshots alike.
    with path.open("xb") as out:
        try: Image.fromarray(rgb).save(out,format=fmt,**({"quality":95,"subsampling":0} if fmt=="JPEG" else {}))
        except BaseException:
            out.close()
            path.unlink(missing_ok=True)
            raise
