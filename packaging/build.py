"""Build and qualify the General package on its target OS, using a clean Python 3.12 env."""
import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from PyInstaller.archive.readers import CArchiveReader

ROOT=Path(__file__).resolve().parents[1]
TOOL=ROOT/"tools/slide_viewer"
def run(*args, **kw):
    subprocess.run([str(a) for a in args],check=True,**kw)

def audit(exe):
    archive=CArchiveReader(str(exe))
    names=[s.lower().replace(chr(92),"/") for s in archive.toc]
    forbidden=("vendor_libs/","vendor_sdk/","pixelengine","ndpread2","libndpread","softwarerenderbackend","research-use.txt")
    if not any("libopenslide" in n for n in names) or any(t in n for n in names for t in forbidden):
        raise ValueError("Unexpected packaged runtime inventory")
    pyz=archive.open_embedded_archive("PYZ.pyz")
    for name in pyz.toc:
        if name.startswith("easypath.") and not (ROOT.joinpath(*name.split(".")).with_suffix(".py").is_file() or ROOT.joinpath(*name.split("."),"__init__.py").is_file()):
            raise ValueError("Unexpected EasyPath module: "+name)

def build():
    os.chdir(ROOT)
    py=sys.executable
    osname={"darwin":"macos","win32":"windows","linux":"ubuntu"}[sys.platform]
    arch={"amd64":"x64","x86_64":"x64","arm64":"arm64","aarch64":"arm64"}[platform.machine().lower()]
    target=osname+"-"+arch
    if os.environ.get("EXPECTED_TARGET",target)!=target:raise ValueError("Wrong native runner")
    name="EasyPath-Slide-Viewer-"+target
    run(py,TOOL/"fetch_fltk.py")
    fltk=ROOT/"build-fltk";ui=ROOT/"build-ui"
    opts=["-DCMAKE_BUILD_TYPE=Release","-DCMAKE_POSITION_INDEPENDENT_CODE=ON","-DFLTK_BACKEND_WAYLAND=OFF","-DFLTK_BUILD_TEST=OFF","-DFLTK_BUILD_GL=OFF","-DFLTK_BUILD_EXAMPLES=OFF","-DFLTK_BUILD_FLUID=OFF"]
    run("cmake","-S","build-deps/fltk-1.4.5","-B",fltk,*opts)
    run("cmake","--build",fltk,"--config","Release","--target","fltk","--parallel","2")
    run("cmake","-S",TOOL,"-B",ui,"-DFLTK_DIR="+str(fltk),"-DCMAKE_BUILD_TYPE=Release")
    run("cmake","--build",ui,"--config","Release","--parallel","2")
    run(py,"-m","PyInstaller","--noconfirm","--log-level","WARN",TOOL/"general.spec")
    folder=ROOT/"dist"
    exe=folder/(name+(".exe" if osname=="windows" else ""))
    audit(exe)
    env=dict(os.environ,PYTHONPATH=str(ROOT),APPIMAGE_EXTRACT_AND_RUN="1")
    def check(program):
        run(py,TOOL/"accept.py",program,env=env,timeout=900)
    if osname=="windows":
        pkg=exe
        check(exe)
    else:
        run(py,TOOL/"package_unix.py",exe,folder)
        if osname=="ubuntu":
            pkg=folder/(name+".AppImage")
            with TemporaryDirectory() as tmp:
                run(pkg,"--appimage-extract","AppRun",cwd=tmp)
                if (Path(tmp)/"squashfs-root/AppRun").read_bytes()!=exe.read_bytes():
                    raise ValueError("AppImage executable mismatch")
            check(pkg)
        else:
            pkg=folder/(name+".dmg")
            with TemporaryDirectory() as tmp:
                run("hdiutil","attach","-readonly","-nobrowse","-mountpoint",tmp,pkg)
                try:
                    mounted=Path(tmp)/"EasyPath Slide Viewer.app/Contents/MacOS"/name
                    if mounted.read_bytes()!=exe.read_bytes():raise ValueError("DMG executable mismatch")
                    check(mounted)
                finally:
                    for attempt in range(11):
                        result=subprocess.run(["hdiutil","detach",tmp])
                        if result.returncode==0:break
                        if result.returncode!=16 or attempt==10:result.check_returncode()
                        sleep(1)
    out=ROOT/"verified";out.mkdir(exist_ok=False)
    shutil.copy2(pkg,out/pkg.name)
    with pkg.open("rb") as f:print(pkg.name, hashlib.file_digest(f,"sha256").hexdigest())

if __name__=="__main__":build()
