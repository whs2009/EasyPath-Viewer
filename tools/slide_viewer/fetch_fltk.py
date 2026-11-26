"""Fetch the one reviewed FLTK source archive into an empty project build directory."""
import hashlib
from pathlib import Path
import tarfile
import tempfile
from urllib.request import Request, urlopen

URL="https://github.com/fltk/fltk/releases/download/release-1.4.5/fltk-1.4.5-source.tar.gz"
SHA="eede1fb2b8e9c2e581e77082e15252145855c79aad30070ee3b24aabe2f926f1"
root=Path(__file__).resolve().parents[2]/"build-deps"
root.mkdir(exist_ok=True)
dest=root/"fltk-1.4.5"
if dest.exists():
    raise SystemExit("FLTK directory already exists; refusing to replace it.")
with tempfile.TemporaryDirectory(dir=root) as temp:
    tmp=Path(temp)
    with urlopen(Request(URL,headers={"User-Agent":"EasyPath-native-build"}),timeout=90) as r:
        raw=r.read(40*1024*1024+1)
    if hashlib.sha256(raw).hexdigest()!=SHA:
        raise SystemExit("FLTK archive digest mismatch.")
    archive=tmp/"source.tgz"
    archive.write_bytes(raw)
    with tarfile.open(archive) as f:
        f.extractall(tmp,filter="data")
    if not (tmp/dest.name/"COPYING").is_file():
        raise SystemExit("Unexpected FLTK archive layout.")
    (tmp/dest.name).rename(dest)
