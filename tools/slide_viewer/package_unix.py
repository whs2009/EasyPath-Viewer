"""Wrap a native, tested PyInstaller binary. Does not sign/publish or modify the OS."""
import hashlib
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen

PINS = {
    "x86_64": (657374641, "617aa77314d57c8b8e4fae9c3f0b35af7b30d5529cb69e98f64f92510ed98a46"),
    "aarch64": (657373827, "9a72a744d221576c9347ec074243acc5c3c7568896b12714e0e82e9f0bf47900")
}
def package(binary, dest):
    machine = platform.machine().lower()
    arch = {"arm64":"arm64","aarch64":"arm64","x86_64":"x64"}[machine]
    from runpy import run_path
    system = run_path(str(Path(__file__).resolve().parents[2]/"packaging/native_target.py"))["system_name"]()
    tool="Annotator" if binary.name.startswith("EasyPath-Slide-Annotator-") else "Viewer"
    title="EasyPath Slide "+tool
    guide=Path(__file__).with_name("ANNOTATOR-GUIDE.md" if tool=="Annotator" else "USER-GUIDE.md")
    name = f"EasyPath-Slide-{tool}-{system}-{arch}"
    if binary.name != name or not os.access(binary, os.X_OK):
        raise ValueError("Expected the matching native executable.")
    ext = ".dmg" if system == "macos" else ".AppImage"
    output = dest/(name + ext)
    with tempfile.TemporaryDirectory(dir=dest,prefix="native-package-") as owned:
        tmp = Path(owned)
        app = tmp/"bundle"
        app.mkdir()
        if system == "macos":
            contents = app/(title+".app")/"Contents"
            (contents/"MacOS").mkdir(parents=True)
            shutil.copy2(binary,contents/"MacOS"/name)
            (contents/"Info.plist").write_bytes(plistlib.dumps(dict(CFBundleExecutable=name,
                CFBundleIdentifier="org.easypath.slide"+tool.lower(), CFBundlePackageType="APPL",
                CFBundleName=title, CFBundleVersion="1", CFBundleShortVersionString={"Viewer":"0.2.4","Annotator":"0.2.3"}[tool])))
            shutil.copy2(guide,app)
            subprocess.run(["hdiutil","create","-volname",title,"-srcfolder",str(app),
                "-format","UDZO",str(tmp/output.name)],check=True)
        else:
            shutil.copy2(binary,app/"AppRun")
            (app/(tool.lower()+".desktop")).write_text(f"[Desktop Entry]\nType=Application\nName={title}\nExec=AppRun\nIcon=converter\nCategories=Science;Graphics;\n")
            (app/"converter.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="8" fill="#175a56"/><path fill="none" stroke="white" stroke-width="4" d="M10 22l22-12 22 12-22 12zM10 32l22 12 22-12M10 42l22 12 22-12"/></svg>')
            shutil.copy2(app/"converter.svg",app/".DirIcon")
            shutil.copy2(guide,app)
            squash = tmp/"payload"
            subprocess.run(["mksquashfs",str(app),str(squash),"-root-owned","-noappend","-comp","gzip","-processors","2"],check=True)
            asset,digest = PINS[machine]
            url = f"https://api.github.com/repos/AppImage/type2-runtime/releases/assets/{asset}"
            req = Request(url,headers={"Accept":"application/octet-stream","User-Agent":"EasyPath-native-build"})
            staged = os.environ.get("EASYPATH_APPIMAGE_RUNTIME")
            with (Path(staged).open("rb") if staged else urlopen(req, timeout=60)) as response:
                runtime = response.read(4*1024*1024+1)
            if hashlib.sha256(runtime).hexdigest() != digest:
                raise ValueError("AppImage runtime digest mismatch.")
            with (tmp/output.name).open("xb") as out, squash.open("rb") as inp:
                out.write(runtime)
                shutil.copyfileobj(inp,out)
            (tmp/output.name).chmod(0o755)
        os.link(tmp/output.name,output)
    return output

if __name__ == "__main__":
    print(package(Path(sys.argv[1]).resolve(strict=True),Path(sys.argv[2]).resolve(strict=True)))
