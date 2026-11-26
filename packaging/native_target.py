"""Identify a native package build target; never label an arbitrary Linux build as CentOS."""
import platform


def system_name():
    system = platform.system()
    if system in {"Windows", "Darwin"}:
        return {"Windows": "windows", "Darwin": "macos"}[system]
    if system == "Linux":
        release = platform.freedesktop_os_release()
        if release.get("ID") == "ubuntu":
            return "ubuntu"
        if (release.get("ID"), release.get("NAME"), release.get("VERSION_ID"), platform.machine()) == (
                "centos", "CentOS Stream", "9", "x86_64"):
            return "centos-stream9"
    raise ValueError("Unsupported native build host")
