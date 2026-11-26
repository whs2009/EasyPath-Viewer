# Build natively, from an isolated interpreter containing only codec/build deps.
from pathlib import Path
from importlib.machinery import EXTENSION_SUFFIXES
from importlib.metadata import distributions
import os
import platform
import sys
from PyInstaller.utils.hooks import collect_dynamic_libs, copy_metadata, collect_data_files

src = Path(SPECPATH)
root = src.parents[1]

system = platform.system()
arch = {"amd64":"x64","x86_64":"x64","aarch64":"arm64","arm64":"arm64"}[platform.machine().lower()]
target = {"Windows":"windows","Darwin":"macos","Linux":"ubuntu"}[system] + "-" + arch
if target not in {"windows-x64","macos-x64","macos-arm64","ubuntu-x64","ubuntu-arm64"}:
    raise ValueError("Unsupported desktop target")
name = "EasyPath-Slide-Viewer-" + target
annotator=False
guide="ANNOTATOR-GUIDE.md" if annotator else "USER-GUIDE.md"
uidll = {"Windows":"viewer_ui.dll","Darwin":"libviewer_ui.dylib","Linux":"libviewer_ui.so"}[system]
uiroot = Path(os.environ.get("EASYPATH_UI_BUILD", root/"build-ui"))
if annotator:uidll=uidll.replace("viewer","annotator")
ui = next(p/uidll for p in (uiroot/"Release", uiroot) if (p/uidll).is_file())
binaries = [(str(ui), ".")] + collect_dynamic_libs("openslide_bin", search_patterns=[
    "*.dll", "*.dylib", "*.so", "*.so.*"])
data = [(str(src/guide), "docs"), (str(root/"build-deps/fltk-1.4.5/COPYING"), "licenses/FLTK"),
        (str(Path(sys.base_prefix)/"LICENSE.txt"), "licenses/Python")] if system=="Windows" else [
        (str(src/guide), "docs"), (str(root/"build-deps/fltk-1.4.5/COPYING"), "licenses/FLTK")]
data.extend((str(root/file), "licenses/EasyPath") for file in ("LICENSE", "NOTICE"))
for dist in distributions():
    for item in dist.files or []:
        if "license" in str(item).lower() or item.name.lower() in {"copying", "notice", "copyright"}:
            full = Path(dist.locate_file(item))
            if full.is_file() and not full.suffix in {".py", ".pyc", ".pyd"}:
                data.append((str(full),"licenses/"+dist.metadata["Name"]+"/"+str(item.parent)))
for pkg in ("wsidicom", "imagecodecs", "openslide-bin", "universal-pathlib", "dicomweb-client", "pydicom"):
    data += copy_metadata(pkg)
data += collect_data_files("upath", includes=["**/*.json"])
hidden = ["openslide_bin", "imagecodecs._jpegxl", "imagecodecs._jpeg2k", "imagecodecs._jpeg8", "imagecodecs._imcd",
          "imagecodecs._shared", "imagecodecs._shared_cython", "imagecodecs._deflate", "imagecodecs._zlib",
          "encodings.utf_8", "pydicom.pixels.encoders.native"]
a = Analysis([str(src/"main.py")], pathex=[str(src),str(root)], binaries=binaries, datas=data,
             hiddenimports=hidden, excludes=["easypath.api","easypath.storage","torch","cv2","matplotlib","tkinter","IPython","pytest","scipy","pandas"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=name, console=True,
          hide_console="hide-early" if system=="Windows" else None, strip=False, upx=False)
