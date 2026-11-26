"""Offline native desktop entry point. No HTTP listener, account or database."""
import ctypes as C
import sys
from pathlib import Path

def main(argv, *, annotator=False):
    frozen=bool(getattr(sys,"frozen",False))
    root=Path(getattr(sys,"_MEIPASS",Path(__file__).resolve().parent))
    hospital=False
    annotation_check=annotator and len(argv)==4 and argv[0]=="--check-annotations"
    zoom_check=(len(argv)==4 and argv[0]=="--check-ndpi-zoom" and Path(argv[1]).suffix.lower()==".ndpi"
                and argv[2] in {"20","40"} and argv[3] in {"openslide","vendor_sdk"})
    if argv and argv!=["--gui-smoke"] and not (zoom_check or annotation_check or (len(argv)==4 and argv[0]=="--check-navigation") or (len(argv)==3 and argv[0] in {"--check-overlays","--check-slide-details"})):
        return 2
    from engine import OverviewCache,Session,save_view
    import numpy as np
    cache=OverviewCache()
    session=Session(hospital,cache)
    store=None
    integer,ptr=C.c_int,C.c_void_p
    op=C.CFUNCTYPE(integer,C.c_char_p,C.POINTER(C.c_double),ptr,integer)
    rd=C.CFUNCTYPE(integer,*([integer]*6),ptr,ptr,integer)
    sv=C.CFUNCTYPE(integer,C.c_char_p,integer,integer,ptr,ptr,integer)
    mc=C.CFUNCTYPE(integer,integer,C.POINTER(integer),ptr)
    def text(out,size,s):
        val=str(s).encode("utf8","replace")[:size-1]+b"\0";C.memmove(out,val,len(val))
    @op
    def open_file(name,dims,msg,size):
        nonlocal session,store
        try:
            d,label=session.open(name.decode())
            for i,n in enumerate(d):dims[i]=n
            text(msg,size,label);return 0
        except Exception as e:text(msg,size,e);return 1
    @rd
    def read(x,y,z,w,h,f,out,msg,size):
        try:
            a=session.render(x,y,z,w,h,f)
            C.memmove(out,a.ctypes.data,a.nbytes);return 0
        except Exception as e:text(msg,size,e);return 1
    @sv
    def save(name,w,h,buf,msg,size):
        try:
            if not 0<w*h<=16777216:raise ValueError("Invalid snapshot.")
            rgb=np.ctypeslib.as_array((C.c_ubyte*(w*h*3)).from_address(buf)).reshape(h,w,3)
            save_view(name.decode(),rgb);return 0
        except Exception as e:text(msg,size,e);return 1
    @mc
    def macro(limit,dims,out):
        try:
            rgb=session.macro(limit)
            if rgb.dtype!=np.uint8 or rgb.ndim!=3 or rgb.shape[2]!=3 or min(rgb.shape[:2])<=0 or max(rgb.shape[:2])>limit:return 1
            dims[0],dims[1]=rgb.shape[1],rgb.shape[0]
            C.memmove(out,rgb.ctypes.data,rgb.nbytes);return 0
        except Exception:return 1
    dll={"win32":"viewer_ui.dll","linux":"libviewer_ui.so","darwin":"libviewer_ui.dylib"}[sys.platform]
    ui=C.CDLL(str(root/(dll.replace("viewer","annotator") if annotator else dll)))
    try:
        if zoom_check:
            ui.viewer_ndpi_zoom.argtypes=[op,rd,C.c_char_p,integer]
            code=ui.viewer_ndpi_zoom(open_file,read,argv[1].encode(),11 if argv[2]=="20" else 12)
            if code:return code
            return 0 if session.dims[5]*40==int(argv[2]) and session.meta.reader_backend.value==argv[3] else 9
        if argv[:1]==["--check-slide-details"]:
            ui.viewer_details.argtypes=[op,rd,mc,C.c_char_p,C.c_char_p]
            return ui.viewer_details(open_file,read,macro,*[v.encode() for v in argv[1:]])
        if argv[:1]==["--check-overlays"]:
            ui.viewer_overlays.argtypes=[op,rd,sv,C.c_char_p,C.c_char_p]
            return ui.viewer_overlays(open_file,read,save,*[v.encode() for v in argv[1:]])
        if argv[:1]==["--check-navigation"]:
            ui.viewer_check.argtypes=[op,rd,sv]+[C.c_char_p]*3
            return ui.viewer_check(open_file,read,save,*[v.encode() for v in argv[1:]])
        ui.viewer_show.argtypes=[op,rd,sv,mc,integer]
        return ui.viewer_show(open_file,read,save,macro,bool(argv))
    finally:session.close()

if __name__=="__main__":sys.exit(main(sys.argv[1:]))
