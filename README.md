# EasyPath Viewer · General edition

Explore local whole-slide images and Z planes with calibrated measurements, a rotation-aware minimap and exportable views. No server or account is required.

## Downloads

Use the [GitHub Releases page](https://github.com/whs2009/EasyPath-Viewer/releases) for standalone packages and `SHA256SUMS`. Choose the **asset** matching your machine, not GitHub's automatic source ZIP. Releases are published only after all five native jobs pass; the [build status](https://github.com/whs2009/EasyPath-Viewer/actions/workflows/general.yml) shows pending or failed builds.

| Platform | Release asset |
|---|---|
| Windows x86-64 | `EasyPath-Slide-Viewer-windows-x64.exe` |
| macOS Apple silicon | `EasyPath-Slide-Viewer-macos-arm64.dmg` |
| macOS Intel | `EasyPath-Slide-Viewer-macos-x64.dmg` |
| Ubuntu x86-64 | `EasyPath-Slide-Viewer-ubuntu-x64.AppImage` |
| Ubuntu ARM64 | `EasyPath-Slide-Viewer-ubuntu-arm64.AppImage` |

On Windows, compare `Get-FileHash .\<filename> -Algorithm SHA256` with the published checksum. On macOS use `shasum -a 256`, then open the DMG. On Ubuntu use `sha256sum` and give the AppImage execute permission before launching it. No Python installation is needed for packaged downloads. Follow your organization's signing and execution policies.

## Supported formats

| Input | General reader | What to check |
|---|---|---|
| EasyPath `.ep` | EP v1/v2 tiles; v3 focal stack | Z changes frame; pyramid levels change zoom. |
| Aperio `.svs`, Leica `.scn`, Ventana `.bif`, Sakura `.svslide` | OpenSlide | Must be a supported scanner encoding, not just a matching extension. |
| Hamamatsu `.ndpi` | OpenSlide; recognized focal stacks have a separate bounded reader | NDP.read is **not** included. Not all scanner variants are supported. |
| MIRAX `.mrxs` | OpenSlide | Keep the index file and its companion directory together. |
| Tiled `.tif` / `.tiff` | EasyPath transformed pyramids, supported focal TIFFs and recognized scanner WSI | Generic flat photographs and arbitrary TIFFs are not supported WSI. |
| DICOM WSI `.dcm` / `.dicom` | VL Whole Slide Microscopy Image Storage | Select an instance and keep its companion series available. CT and MR are not WSI. |
| Philips JPEG XL **DICOM WSI** | Standards-based DICOM + JPEG XL decoder, **not** PixelEngine | Supported unsigned three-channel display cases; JPEG XL transfer syntax UIDs ending in .110, .111 and .112. High-bit samples are mapped to RGB8 for display. Not every instrument, profile or series is qualified. |
| Philips native `.isyntax` | **Not included** | Distinct from Philips DICOM WSI. Requires a separately authorized reader. |

## Use the viewer

Open one or more slides. Page Up/Down switches slides; Shift+Page Up/Down switches focal planes. Pan, wheel to zoom around the pointer, rotate, or click the minimap. **Slide preview** shows the scanner macro/label when available, not a substitute tissue thumbnail. Metadata distinguishes native calibration from inferred magnification. With calibrated axes the ruler measures physical length; right-click to recolor or remove a line. Save PNG/JPEG views with optional scale and measurements, or use F11 for fullscreen. Measurements are session-only.

See the [full controls and shortcuts](tools/slide_viewer/USER-GUIDE.md).

## Scope and licensing

This General distribution contains **no Philips PixelEngine or Hamamatsu NDP.read SDK**. OpenSlide support for an NDPI is not a vendor license. For a restricted edition, first obtain vendor authorization covering the intended use and platform, then contact the EasyPath core team. Use public issues only for nonconfidential inquiries. Do not post slide images, barcodes, patient identifiers, license letters, credentials or annotation text.

The EasyPath-authored standalone source and examples in this repository are licensed under [Apache-2.0](LICENSE). Third-party components retain their own licenses and notices. The private EasyPath Server is not distributed or licensed by this repository. This software is not clinically validated.

## Build from source

Use a clean Python 3.12 environment on a supported native machine, a C++17 compiler and CMake. Install `tools/slide_viewer/requirements-build.txt`, then run `python packaging/build.py` from a fresh checkout (Linux: under Xvfb for GUI tests). The [release workflow](.github/workflows/general.yml) lists Linux build dependencies. The build downloads hash-checked FLTK, compiles the native UI, freezes the local reader/codec engine with its license notices and tests the packaged app with generated nonclinical data. No credentials or private server checkout are required to build.
