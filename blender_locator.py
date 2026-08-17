"""
Find a Blender executable on Windows, macOS or Linux.

Used by both front ends (easymixamo.py and gui.py). The scripts that run *inside*
Blender never import this.

Search order:
  1. the BLENDER environment variable
  2. blender on PATH
  3. the usual install locations for this platform, newest version first
     (including Steam libraries, which is how a lot of people have Blender)
"""

import glob
import os
import re
import shutil
import string
import sys

ENV_VAR = "BLENDER"


def _newest(paths):
    """Sort by the version number embedded in the path, highest first.

    'Blender 4.2' sorts below 'Blender 5.2'; a plain 'Blender' folder (Steam installs
    have no version in the name) sorts last but is still returned if it is all there is.
    """
    def key(p):
        nums = [int(n) for n in re.findall(r"(\d+)", os.path.dirname(p))]
        return (nums or [0])
    return sorted(paths, key=key, reverse=True)


def _windows_candidates():
    out = []
    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        out += glob.glob(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe"))
    # Steam: the library can live on any drive, under either name
    drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    for d in drives:
        for lib in ("Steam", "SteamLibrary", os.path.join("Program Files (x86)", "Steam")):
            p = os.path.join(d, lib, "steamapps", "common", "Blender", "blender.exe")
            if os.path.isfile(p):
                out.append(p)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out += glob.glob(os.path.join(local, "Programs", "Blender*", "blender.exe"))
    return out


def _macos_candidates():
    out = []
    for root in ("/Applications", os.path.expanduser("~/Applications")):
        out += glob.glob(os.path.join(root, "Blender*.app", "Contents", "MacOS", "Blender"))
    out += glob.glob(os.path.expanduser(
        "~/Library/Application Support/Steam/steamapps/common/Blender/"
        "Blender.app/Contents/MacOS/Blender"))
    return out


def _linux_candidates():
    out = ["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"]
    out += glob.glob("/opt/blender*/blender")
    out += glob.glob(os.path.expanduser("~/blender*/blender"))
    for steam in ("~/.steam/steam/steamapps/common/Blender/blender",
                  "~/.local/share/Steam/steamapps/common/Blender/blender"):
        out.append(os.path.expanduser(steam))
    return out


def candidates():
    if sys.platform == "win32":
        found = _windows_candidates()
    elif sys.platform == "darwin":
        found = _macos_candidates()
    else:
        found = _linux_candidates()
    return _newest([p for p in found if os.path.isfile(p)])


def find_blender():
    """Return a path to blender, or '' if nothing was found."""
    env = os.environ.get(ENV_VAR, "").strip().strip('"')
    if env and os.path.isfile(env):
        return env

    on_path = shutil.which("blender") or shutil.which("blender.exe")
    if on_path:
        return on_path

    found = candidates()
    return found[0] if found else ""


def not_found_message():
    return (
        "Could not find Blender.\n"
        "Install it from https://www.blender.org/download/ (4.2 LTS or newer), then either\n"
        "  - add it to your PATH, or\n"
        f"  - set the {ENV_VAR} environment variable to the executable, or\n"
        "  - pass --blender <path> on the command line."
    )


if __name__ == "__main__":
    b = find_blender()
    print(b or not_found_message())
