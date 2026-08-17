#!/usr/bin/env python3
"""
easy-mixamo command line front end.

Runs the Blender scripts for you: finds Blender, adds --factory-startup, forwards the
arguments and reports the exit code. Works the same on Windows, macOS and Linux.

  cd path/to/my/character/fbx
  python easymixamo.py inspect
  python easymixamo.py build --target-height 1.75 --in-place Walking
  python easymixamo.py verify --expect-height 1.75
  python easymixamo.py preview

  python easymixamo.py all --target-height 1.75      # all four, in order
  python easymixamo.py gui                           # open the graphical front end
  python easymixamo.py doctor                        # check the setup

--src defaults to the current directory and --out to <src>/Character.glb, so most of
the time you only need the subcommand.
"""

import argparse
import os
import subprocess
import sys
import threading
import time

from blender_locator import ENV_VAR, find_blender, not_found_message

APP_DIR = os.path.dirname(os.path.abspath(__file__))

SCRIPTS = {
    "inspect": "inspect_fbx.py",
    "build": "build_character.py",
    "verify": "verify_glb.py",
    "preview": "render_preview.py",
}
STEPS = ["inspect", "build", "verify", "preview"]

# stderr lines Blender emits during normal operation - noise, not problems
NOISE = ("Blender quit", "found bundled python", "Warning: Cannot open",
         "Fra:", "Info: Deleted", "AL lib:")


class Colors:
    """ANSI colors, disabled when the output is not a terminal."""
    on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    @classmethod
    def _c(cls, code, s):
        return f"\033[{code}m{s}\033[0m" if cls.on else s

    @classmethod
    def head(cls, s):
        return cls._c("36;1", s)

    @classmethod
    def ok(cls, s):
        return cls._c("32;1", s)

    @classmethod
    def err(cls, s):
        return cls._c("31;1", s)

    @classmethod
    def dim(cls, s):
        return cls._c("90", s)


# --------------------------------------------------------------------- running

def run_script(blender, script, args, show_log):
    """Run one Blender script. Streams stdout live; stderr is buffered and only shown
    if something goes wrong (or always, with --show-log)."""
    cmd = [blender, "--factory-startup", "-b", "--python", script, "--", *args]
    print(Colors.dim("  " + " ".join(cmd)))
    print()

    errbuf = []

    def drain_stderr(pipe):
        for line in pipe:
            line = line.rstrip("\r\n")
            if show_log:
                print(line)
            elif not any(n in line for n in NOISE):
                errbuf.append(line)

    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
    except OSError as e:
        print(Colors.err(f"could not start Blender: {e}"))
        return 127, 0.0

    t = threading.Thread(target=drain_stderr, args=(proc.stderr,), daemon=True)
    t.start()
    for line in proc.stdout:
        print(line.rstrip("\r\n"))
    rc = proc.wait()
    t.join(timeout=2)

    if rc != 0 and not show_log and errbuf:
        print()
        print(Colors.err("--- Blender stderr ---"))
        for line in errbuf[-40:]:
            print(line)
    return rc, time.time() - t0


# --------------------------------------------------------- argument assembly

def script_args(step, a):
    src = os.path.abspath(a.src)
    out = (os.path.abspath(a.out) if getattr(a, "out", None)
           else os.path.join(src, "Character.glb"))
    glb = os.path.abspath(a.glb) if getattr(a, "glb", None) else out

    if step == "inspect":
        return ["--src", src]

    if step == "build":
        args = ["--src", src, "--out", out]
        if a.skin:
            args += ["--skin", a.skin]
        for x in a.anim or []:
            args += ["--anim", x]
        if a.rest:
            args += ["--rest", a.rest]
        if a.target_height is not None:
            args += ["--target-height", str(a.target_height)]
        for x in a.in_place or []:
            args += ["--in-place", x]
        if a.root_bone:
            args += ["--root-bone", a.root_bone]
        if a.fps is not None:
            args += ["--fps", str(a.fps)]
        if a.keep_prefix:
            args += ["--keep-prefix"]
        if a.tolerance is not None:
            args += ["--tolerance", str(a.tolerance)]
        if a.blend:
            args += ["--blend", a.blend]
        return args

    if step == "verify":
        args = ["--glb", glb]
        if a.expect_anims:
            args += ["--expect-anims", a.expect_anims]
        if a.expect_height is not None:
            args += ["--expect-height", str(a.expect_height)]
        elif a.command == "all" and a.target_height is not None:
            args += ["--expect-height", str(a.target_height)]
        return args

    if step == "preview":
        args = ["--glb", glb]
        args += ["--out", os.path.abspath(a.preview_out) if a.preview_out
                 else os.path.splitext(glb)[0] + "_preview"]
        if a.shots is not None:
            args += ["--shots", str(a.shots)]
        return args

    raise ValueError(step)


def add_common(p):
    p.add_argument("--blender", default=None,
                   help=f"path to the Blender executable (or set ${ENV_VAR})")
    p.add_argument("--show-log", action="store_true",
                   help="show Blender's full output, including stderr")


def add_build_opts(p):
    p.add_argument("--src", default=".", help="folder with the FBX files (default: .)")
    p.add_argument("--out", default=None, help="output GLB (default: <src>/Character.glb)")
    p.add_argument("--skin", default=None, help="the skinned FBX, e.g. 'Character.fbx'")
    p.add_argument("--anim", action="append", default=[],
                   help="'File.fbx' or 'File.fbx=Name'. Repeatable")
    p.add_argument("--rest", default=None,
                   help="'auto' (default), 'bind', or '<AnimName>:<frame>'")
    p.add_argument("--target-height", type=float, default=None,
                   help="scale the character to this height in metres, e.g. 1.75")
    p.add_argument("--in-place", action="append", default=[],
                   help="strip horizontal root motion from this animation. Repeatable")
    p.add_argument("--root-bone", default=None, help="bone used to measure root motion")
    p.add_argument("--fps", type=int, default=None, help="scene fps")
    p.add_argument("--keep-prefix", action="store_true",
                   help="keep the 'mixamorig:' prefix (not recommended for Godot)")
    p.add_argument("--tolerance", type=float, default=None,
                   help="verification threshold in metres (default 1e-4)")
    p.add_argument("--blend", default=None, help="where to save the .blend")


def add_verify_opts(p):
    p.add_argument("--expect-anims", default=None,
                   help="comma separated animation names that must be present")
    p.add_argument("--expect-height", type=float, default=None,
                   help="expected height in metres")


def add_preview_opts(p):
    p.add_argument("--preview-out", default=None,
                   help="render folder (default: <glb>_preview)")
    p.add_argument("--shots", type=int, default=None, help="frames per animation")


def build_parser():
    p = argparse.ArgumentParser(
        prog="easymixamo",
        description="Merge Mixamo FBX exports into one Godot-ready GLB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run 'easymixamo <command> --help' for the options of one command.")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("inspect", help="check the FBX files before building")
    c.add_argument("--src", default=".", help="folder with the FBX files (default: .)")
    add_common(c)

    c = sub.add_parser("build", help="merge everything into one GLB")
    add_build_opts(c)
    add_common(c)

    c = sub.add_parser("verify", help="independently verify a generated GLB")
    c.add_argument("--glb", default=None, help="the GLB (default: ./Character.glb)")
    c.add_argument("--src", default=".", help=argparse.SUPPRESS)
    c.add_argument("--out", default=None, help=argparse.SUPPRESS)
    add_verify_opts(c)
    add_common(c)

    c = sub.add_parser("preview", help="render preview stills from a GLB")
    c.add_argument("--glb", default=None, help="the GLB (default: ./Character.glb)")
    c.add_argument("--src", default=".", help=argparse.SUPPRESS)
    c.add_argument("--out", default=None, help=argparse.SUPPRESS)
    add_preview_opts(c)
    add_common(c)

    c = sub.add_parser("all", help="inspect, build, verify and preview in order")
    add_build_opts(c)
    add_verify_opts(c)
    add_preview_opts(c)
    c.add_argument("--glb", default=None, help=argparse.SUPPRESS)
    add_common(c)

    c = sub.add_parser("gui", help="open the graphical front end")
    c.add_argument("--blender", default=None, help=argparse.SUPPRESS)

    c = sub.add_parser("doctor", help="check that Blender and the scripts are in place")
    add_common(c)

    return p


# ------------------------------------------------------------------ commands

def cmd_doctor(a):
    print(Colors.head("easy-mixamo doctor"))
    print(f"  python  : {sys.version.split()[0]}  ({sys.platform})")

    missing = [s for s in SCRIPTS.values() if not os.path.isfile(os.path.join(APP_DIR, s))]
    if missing:
        print(Colors.err(f"  scripts : MISSING {missing}"))
    else:
        print(f"  scripts : all present in {APP_DIR}")

    blender = a.blender or find_blender()
    if not blender:
        print(Colors.err("  blender : not found"))
        print()
        print(not_found_message())
        return 1
    print(f"  blender : {blender}")
    try:
        v = subprocess.run([blender, "--version"], capture_output=True, text=True,
                           timeout=60).stdout.splitlines()
        print(f"  version : {v[0].strip() if v else '?'}")
    except Exception as e:
        print(Colors.err(f"  version : could not run it ({e})"))
        return 1

    try:
        import tkinter  # noqa: F401
        print("  tkinter : available (the GUI will work)")
    except ImportError:
        print(Colors.dim("  tkinter : missing - the GUI will not start, the CLI is fine"))

    print()
    print(Colors.ok("setup looks good"))
    return 0


def cmd_gui(a):
    gui = os.path.join(APP_DIR, "gui.py")
    if not os.path.isfile(gui):
        print(Colors.err(f"not found: {gui}"))
        return 1
    return subprocess.call([sys.executable, gui])


def cmd_steps(a, steps):
    blender = a.blender or find_blender()
    if not blender:
        print(Colors.err("Blender not found."))
        print(not_found_message())
        return 1

    for step in steps:
        script = os.path.join(APP_DIR, SCRIPTS[step])
        if not os.path.isfile(script):
            print(Colors.err(f"script not found: {script}"))
            return 1

        print(Colors.head(f"> {step}"))
        rc, secs = run_script(blender, script, script_args(step, a), a.show_log)
        print()
        if rc == 0:
            print(Colors.ok(f"OK ({step}) - {secs:.1f}s"))
        else:
            print(Colors.err(f"FAILED ({step}), exit code {rc} - {secs:.1f}s"))
            if not a.show_log:
                print(Colors.dim("  run again with --show-log for Blender's full output"))
            return rc
        print()
    return 0


def main():
    a = build_parser().parse_args()
    if a.command == "doctor":
        return cmd_doctor(a)
    if a.command == "gui":
        return cmd_gui(a)
    if a.command == "all":
        return cmd_steps(a, STEPS)
    return cmd_steps(a, [a.command])


if __name__ == "__main__":
    sys.exit(main())
