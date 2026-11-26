"""Generate two nonclinical slides and check the real packaged viewer's exported frame."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image
import tifffile

def fixture(folder):
    y,x=np.indices((223,347))
    ref=np.stack([x%256,y%256,(x+y)%256],axis=-1).astype("u1")
    scan=folder/"synthetic.svs"
    with tifffile.TiffWriter(scan) as tw:
        options=dict(photometric="rgb",metadata=None,compression="deflate")
        tw.write(ref,description="Aperio Image Library v12.4.3\n347x223 (128x128) |AppMag = 40|MPP = 0.25",tile=(128,128),**options)
        tw.write(ref[::4,::4],description="thumbnail",**options)
        label=np.zeros((40,32,3),"u1");label[:,2::4]=255;label[:,3::4]=255
        tw.write(label,description="Aperio Image Library\nlabel 32x40",subfiletype=1,**options)
        glass=np.full((40,100,3),(18,180,55),"u1")
        tw.write(glass,description="Aperio Image Library\nmacro 100x40",extratags=[(254,"I",1,9,False)],**options)
    import imagecodecs, json, struct
    digest=lambda b:hashlib.sha256(b).digest()
    meta=json.dumps(dict(source_color_policy="declared-srgb",mpp_x=.25,mpp_y=.5,objective_power=40.,levels=[dict(width=347,height=223)]),separators=(",",":")).encode()
    members=[]
    for rgb in (ref,255-ref,np.roll(ref,17,axis=0)):
        tile=np.full((512,512,3),255,"u1")
        tile[:223,:347]=rgb
        blob=imagecodecs.jpegxl_encode(tile,lossless=True,numthreads=1)
        start=128+len(meta)
        entry=struct.pack("<QQ32s",start+48,len(blob),digest(blob))
        header=struct.pack("<8sHHIQQQQ32s32s16s",b"EPWSI\0\r\n",1,0,1,len(meta),1,start,start+48+len(blob),digest(meta),digest(entry),bytes(16))
        members.append(header+meta+entry+blob)
    rows=b""
    at=56+56*len(members)
    for z,member in enumerate(members,-1):
        rows+=struct.pack("<dQQ32s",float(z),at,len(member),digest(member[:128]))
        at+=len(member)
    (folder/"stack.ep").write_bytes(struct.pack("<8sHHIQ32s",b"EPZWSI\r\n",3,1,3,at,digest(rows))+rows+b"".join(members))

    return scan,folder/"stack.ep",255-ref

def accept(exe):
    with tempfile.TemporaryDirectory() as tmp:
        work=Path(tmp);scan,stack,want=fixture(work);out=work/"view.png"
        env={k:v for k,v in os.environ.items() if not k.startswith(("PYTHON","EASYPATH_","EP_READER"))}
        env["APPIMAGE_EXTRACT_AND_RUN"]="1"
        def run(args,expected=0):
            p=subprocess.run([str(exe),*map(str,args)],env=env,cwd=work,capture_output=True,timeout=150)
            if p.returncode!=expected:raise RuntimeError((args[0],p.returncode,expected,p.stderr[-2048:]))
        before=[hashlib.sha256(f.read_bytes()).digest() for f in (scan,stack)]
        run(["--gui-smoke"])
        run(["--check-slide-details",scan,stack])
        args=["--check-navigation",scan,stack,out]
        run(args)
        got=np.asarray(Image.open(out))
        assert np.array_equal(got[:223,:347],want) and np.all(got[223:]==37)
        prior=out.read_bytes()
        run(["--check-overlays",stack,work])
        def exported(tag,ext):
            with Image.open(work/f"overlay-{tag}.{ext}") as im:
                assert im.format==("PNG" if ext=="png" else "JPEG")
                return np.asarray(im).copy()
        for ext in ("png","jpg"):
            clean,bar,marks,both=(exported(k,ext) for k in ("00","10","01","11"))
            assert clean.shape==got.shape==(461,800,3), (ext,clean.shape,got.shape)
            assert bar.shape==marks.shape==both.shape==clean.shape, (ext,bar.shape,marks.shape,both.shape)
            assert np.any(clean!=bar), f"{ext}: scale bar missing"
            assert np.any(clean!=marks), f"{ext}: measurement missing"
            mid=len(clean)//2
            assert np.array_equal(clean[:mid],bar[:mid]), f"{ext}: scale bar changed upper pixels"
            assert np.array_equal(clean[mid:],marks[mid:]), f"{ext}: measurement changed lower pixels"
            assert np.array_equal(both[:mid],marks[:mid]), f"{ext}: combined measurement changed"
            assert np.array_equal(both[mid:],bar[mid:]), f"{ext}: combined scale bar changed"
        clean=exported("00","png");assert np.array_equal(clean[:223,:347],255-want)
        rot=np.asarray(Image.open(work/"rotated.png"));h,w=rot.shape[:2]
        yy,xx=np.indices((181,161))
        assert np.array_equal(rot[h//2-90:h//2+91,w//2-80:w//2+81],clean[110-(xx-80),160+(yy-90)])
        run(args,5)
        assert out.read_bytes()==prior and before==[hashlib.sha256(f.read_bytes()).digest() for f in (scan,stack)]
        bad=work/"fake.svs";Image.fromarray(want).save(bad,format="PNG")
        run(["--check-navigation",bad,stack,work/"bad.png"],1)
        assert not (work/"bad.png").exists()
        print(exe.name,": actual GUI, two slides, three planes, frame pixel equality, navigation bounds, overwrite and non-WSI refusal passed.",flush=True)

if __name__=="__main__":accept(Path(sys.argv[1]).resolve(strict=True))
