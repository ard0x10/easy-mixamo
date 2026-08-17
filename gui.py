"""
easy-mixamo GUI - Mixamo FBX files -> one GLB for Godot.

This file does not change the Blender scripts (inspect_fbx.py / build_character.py /
verify_glb.py / render_preview.py). It runs them through Blender and collects their
output in one window, so the command line, --factory-startup and the argument wiring
stay out of your way.

Run:      python gui.py        (or EasyMixamo.bat / easymixamo.sh, or `easymixamo gui`)
Needs:    Python 3.9+ with tkinter (bundled with the standard installer) and Blender
"""

import glob
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from blender_locator import find_blender, not_found_message

APP_NAME = "easy-mixamo"
APP_DIR = os.path.dirname(os.path.abspath(__file__))

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

SCRIPTS = {
    "inspect": "inspect_fbx.py",
    "build": "build_character.py",
    "verify": "verify_glb.py",
    "preview": "render_preview.py",
}

# ------------------------------------------------------------------- palette
BG = "#1e1f22"
BG2 = "#26282c"
FG = "#e6e6e6"
DIM = "#8a8f98"
ACCENT = "#4a9eff"
OK = "#5fd35f"
WARN = "#e8b23a"
ERR = "#ff6b6b"

DPI = 1.0


def config_path():
    """Per-user settings file, in the place each platform expects."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME, "gui_settings.json")


CFG_PATH = config_path()


def enable_dpi_awareness():
    """Turn off Windows' blurry bitmap scaling on high DPI displays.
    Returns 1.0 for 96 dpi. Fonts are in points, so 'tk scaling' fixes them."""
    if os.name != "nt":
        return 1.0
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # system DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            return 1.0
    try:
        dc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)     # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, dc)
        return (dpi or 96) / 96.0
    except Exception:
        return 1.0


def pick_font(root, prefer, fallback):
    available = {f.lower() for f in tkfont.families(root)}
    for name in prefer:
        if name.lower() in available:
            return name
    return fallback


def open_in_file_manager(path):
    """Open a file or folder with the OS default handler."""
    if sys.platform == "win32":
        os.startfile(path)                                  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg):
    try:
        os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# --------------------------------------------------------------------- runner

class Runner:
    """Runs Blender jobs one after another in the background, pushing lines to a queue."""

    def __init__(self, out_q):
        self.q = out_q
        self.proc = None
        self.jobs = []
        self.cancelled = False
        self.lock = threading.Lock()

    def busy(self):
        with self.lock:
            return self.proc is not None or bool(self.jobs)

    def run_chain(self, jobs):
        """jobs: [(label, blender_path, script_path, [args]), ...]"""
        self.cancelled = False
        self.jobs = list(jobs)
        self._next()

    def cancel(self):
        self.cancelled = True
        self.jobs = []
        p = self.proc
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass

    def _next(self):
        if self.cancelled or not self.jobs:
            self.q.put(("chain_done", self.cancelled))
            return
        job = self.jobs.pop(0)
        threading.Thread(target=self._run, args=job, daemon=True).start()

    def _run(self, label, blender, script, args):
        cmd = [blender, "--factory-startup", "-b", "--python", script, "--", *args]
        self.q.put(("start", (label, cmd)))
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        t0 = time.time()
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=env, creationflags=CREATE_NO_WINDOW, cwd=APP_DIR)
        except OSError as e:
            self.q.put(("line", f"ERROR: could not start Blender: {e}"))
            self.jobs = []
            self.q.put(("done", (label, -1, 0.0)))
            self.q.put(("chain_done", False))
            return
        with self.lock:
            self.proc = proc
        try:
            for line in proc.stdout:
                self.q.put(("line", line.rstrip("\r\n")))
        except Exception:
            pass
        rc = proc.wait()
        with self.lock:
            self.proc = None
        self.q.put(("done", (label, rc, time.time() - t0)))
        if rc == 0 and not self.cancelled:
            self._next()
        else:
            self.jobs = []
            self.q.put(("chain_done", self.cancelled))


# ------------------------------------------------------------------- file row

class FileRow:
    """One UI row for a single FBX in the source folder."""

    def __init__(self, parent, fname, app, row):
        self.fname = fname
        self.app = app
        self.include = tk.BooleanVar(value=True)
        self.name = tk.StringVar(value=os.path.splitext(fname)[0])
        self.in_place = tk.BooleanVar(value=False)
        self.kind = ""          # filled in after an inspect run

        self.rb = ttk.Radiobutton(parent, variable=app.skin_var, value=fname,
                                  command=app.on_skin_change)
        self.cb = ttk.Checkbutton(parent, variable=self.include, command=app.refresh_cmd)
        self.lbl = ttk.Label(parent, text=fname, style="Row.TLabel")
        self.ent = ttk.Entry(parent, textvariable=self.name, width=18)
        self.ip = ttk.Checkbutton(parent, variable=self.in_place, command=app.refresh_cmd)
        self.info = ttk.Label(parent, text="", style="Dim.TLabel")

        self.rb.grid(row=row, column=0, padx=(8, 2), pady=2)
        self.cb.grid(row=row, column=1, padx=2, pady=2)
        self.lbl.grid(row=row, column=2, sticky="w", padx=(4, 8), pady=2)
        self.ent.grid(row=row, column=3, padx=4, pady=2)
        self.ip.grid(row=row, column=4, padx=2, pady=2)
        self.info.grid(row=row, column=5, sticky="w", padx=(8, 8), pady=2)

        self.name.trace_add("write", lambda *_: app.refresh_cmd())

    def set_is_skin(self, is_skin):
        state = "disabled" if is_skin else "normal"
        for w in (self.cb, self.ent, self.ip):
            w.configure(state=state)

    def set_info(self, text, color=DIM):
        self.info.configure(text=text, foreground=color)

    def destroy(self):
        for w in (self.rb, self.cb, self.lbl, self.ent, self.ip, self.info):
            w.destroy()


# ------------------------------------------------------------------------ app

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = load_cfg()
        self.title("easy-mixamo - Mixamo FBX to Godot GLB")
        self.tk.call("tk", "scaling", 96.0 * DPI / 72.0)
        self.geometry(self.cfg.get("geometry", f"{int(1180 * DPI)}x{int(800 * DPI)}"))
        self.minsize(int(1020 * DPI), int(660 * DPI))
        self.configure(bg=BG)

        self.q = queue.Queue()
        self.runner = Runner(self.q)
        self.rows = []
        self.thumbs = []
        self.out_overridden = False
        self.current_label = ""

        self._init_style()
        self._build_vars()
        self._build_ui()
        self._restore_cfg()
        self.after(120, self._place_sash)
        self.after(60, self._pump)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if not self.blender.get():
            self.log_line("Blender not found. Use 'Browse' on the top row to select it.", ERR)
            self.log_line(not_found_message(), DIM)
        self.refresh_files()

    # ------------------------------------------------------------- appearance
    def _init_style(self):
        self.ui_font = pick_font(self, ["Segoe UI", "SF Pro Text", "Helvetica Neue",
                                        "DejaVu Sans", "Cantarell"], "TkDefaultFont")
        self.ui_bold = pick_font(self, ["Segoe UI Semibold", "SF Pro Text Semibold",
                                        "DejaVu Sans"], self.ui_font)
        self.mono = pick_font(self, ["Consolas", "Menlo", "DejaVu Sans Mono",
                                     "Liberation Mono", "Courier New"], "TkFixedFont")

        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=BG2,
                     bordercolor="#3a3d42", lightcolor=BG2, darkcolor=BG2)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=BG2)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Row.TLabel", background=BG, foreground=FG)
        st.configure("Dim.TLabel", background=BG, foreground=DIM)
        st.configure("Head.TLabel", background=BG, foreground=ACCENT,
                     font=(self.ui_bold, 10))
        st.configure("Title.TLabel", background=BG, foreground=FG,
                     font=(self.ui_bold, 11))
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.map("TCheckbutton", background=[("active", BG)])
        st.configure("TRadiobutton", background=BG, foreground=FG)
        st.map("TRadiobutton", background=[("active", BG)])
        st.configure("TEntry", foreground=FG, insertcolor=FG)
        st.configure("TButton", padding=(10, 5))
        st.configure("Go.TButton", padding=(14, 7), font=(self.ui_bold, 9))
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=BG2, foreground=DIM, padding=(14, 6))
        st.map("TNotebook.Tab", background=[("selected", BG)],
               foreground=[("selected", FG)])
        st.configure("TLabelframe", background=BG, foreground=ACCENT)
        st.configure("TLabelframe.Label", background=BG, foreground=ACCENT)
        st.configure("TSpinbox", fieldbackground=BG2, foreground=FG)
        st.configure("TCombobox", fieldbackground=BG2, foreground=FG)
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=BG2)

    def _build_vars(self):
        self.blender = tk.StringVar(value=self.cfg.get("blender") or find_blender())
        self.src = tk.StringVar(value=self.cfg.get("src", ""))
        self.out = tk.StringVar(value=self.cfg.get("out", ""))
        self.skin_var = tk.StringVar(value="")          # "" = automatic

        self.use_height = tk.BooleanVar(value=self.cfg.get("use_height", True))
        self.height = tk.StringVar(value=self.cfg.get("height", "1.75"))
        self.rest_mode = tk.StringVar(value=self.cfg.get("rest_mode", "auto"))
        self.rest_custom = tk.StringVar(value=self.cfg.get("rest_custom", ""))
        self.use_fps = tk.BooleanVar(value=self.cfg.get("use_fps", False))
        self.fps = tk.StringVar(value=self.cfg.get("fps", "30"))
        self.keep_prefix = tk.BooleanVar(value=self.cfg.get("keep_prefix", False))
        self.root_bone = tk.StringVar(value=self.cfg.get("root_bone", ""))
        self.tolerance = tk.StringVar(value=self.cfg.get("tolerance", "1e-4"))

        self.expect_anims = tk.BooleanVar(value=self.cfg.get("expect_anims", True))
        self.expect_height = tk.BooleanVar(value=self.cfg.get("expect_height", True))
        self.shots = tk.StringVar(value=self.cfg.get("shots", "3"))

        self.status = tk.StringVar(value="ready")

    # ------------------------------------------------------------- interface
    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ---- top: paths
        top = ttk.Frame(self, padding=(12, 10, 12, 6))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        def path_row(r, label, var, cmd, hint=""):
            ttk.Label(top, text=label, style="Head.TLabel").grid(
                row=r, column=0, sticky="w", padx=(0, 8), pady=3)
            e = ttk.Entry(top, textvariable=var)
            e.grid(row=r, column=1, sticky="ew", pady=3)
            ttk.Button(top, text="Browse", command=cmd, width=8).grid(
                row=r, column=2, padx=(6, 0), pady=3)
            if hint:
                ttk.Label(top, text=hint, style="Dim.TLabel").grid(
                    row=r, column=3, sticky="w", padx=(8, 0))
            return e

        path_row(0, "Blender", self.blender, self.pick_blender)
        path_row(1, "FBX folder", self.src, self.pick_src,
                 "the folder holding this character's FBX files")
        path_row(2, "Output GLB", self.out, self.pick_out)
        self.src.trace_add("write", lambda *_: self.on_src_change())
        self.out.trace_add("write", lambda *_: self.refresh_cmd())

        # ---- middle: tabs on the left, log on the right
        mid = ttk.PanedWindow(self, orient="horizontal")
        mid.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        self.mid = mid

        left = ttk.Frame(mid)
        nb = ttk.Notebook(left)
        nb.pack(fill="both", expand=True)
        self.nb = nb
        nb.add(self._tab_files(nb), text="  Files  ")
        nb.add(self._tab_options(nb), text="  Options  ")
        nb.add(self._tab_preview(nb), text="  Preview  ")
        nb.add(self._tab_help(nb), text="  Help  ")
        mid.add(left, weight=3)

        right = ttk.Frame(mid)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        bar = ttk.Frame(right)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(bar, text="Blender output", style="Title.TLabel").pack(side="left")
        ttk.Button(bar, text="Clear", width=9,
                   command=lambda: self._clear_log()).pack(side="right")
        ttk.Button(bar, text="Copy", width=9,
                   command=self.copy_log).pack(side="right", padx=(0, 6))

        logwrap = ttk.Frame(right)
        logwrap.grid(row=1, column=0, sticky="nsew")
        logwrap.rowconfigure(0, weight=1)
        logwrap.columnconfigure(0, weight=1)
        self.log = tk.Text(logwrap, wrap="none", bg="#141517", fg=FG, bd=0,
                           insertbackground=FG, font=(self.mono, 9),
                           padx=10, pady=8, state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logwrap, orient="vertical", command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        sbx = ttk.Scrollbar(logwrap, orient="horizontal", command=self.log.xview)
        sbx.grid(row=1, column=0, sticky="ew")
        self.log.configure(yscrollcommand=sb.set, xscrollcommand=sbx.set)
        for tag, col in (("ok", OK), ("err", ERR), ("warn", WARN),
                         ("dim", DIM), ("accent", ACCENT)):
            self.log.tag_configure(tag, foreground=col)
        self.log.tag_configure("hdr", foreground=ACCENT, font=(self.mono, 9, "bold"))
        mid.add(right, weight=4)

        # ---- bottom: commands and status
        bot = ttk.Frame(self, padding=(12, 4, 12, 10))
        bot.grid(row=2, column=0, sticky="ew")
        bot.columnconfigure(6, weight=1)

        self.btn_inspect = ttk.Button(bot, text="1. Inspect", style="Go.TButton",
                                      command=lambda: self.run(["inspect"]))
        self.btn_build = ttk.Button(bot, text="2. Build (GLB)", style="Go.TButton",
                                    command=lambda: self.run(["build"]))
        self.btn_verify = ttk.Button(bot, text="3. Verify", style="Go.TButton",
                                     command=lambda: self.run(["verify"]))
        self.btn_preview = ttk.Button(bot, text="4. Preview", style="Go.TButton",
                                      command=lambda: self.run(["preview"]))
        self.btn_all = ttk.Button(bot, text="All (1-4)", style="Go.TButton",
                                  command=lambda: self.run(["inspect", "build",
                                                            "verify", "preview"]))
        self.btn_cancel = ttk.Button(bot, text="Stop", command=self.cancel, state="disabled")
        for i, b in enumerate([self.btn_inspect, self.btn_build, self.btn_verify,
                               self.btn_preview, self.btn_all, self.btn_cancel]):
            b.grid(row=0, column=i, padx=(0, 6))

        self.pb = ttk.Progressbar(bot, mode="indeterminate", length=int(140 * DPI))
        self.pb.grid(row=0, column=7, sticky="e", padx=(6, 8))
        self.pb.grid_remove()                       # only visible while running
        ttk.Label(bot, textvariable=self.status, style="Dim.TLabel").grid(
            row=0, column=8, sticky="e")

        # preview of the command that will be run
        self.cmd_lbl = ttk.Label(bot, text="", style="Dim.TLabel", wraplength=int(1100 * DPI),
                                 justify="left", font=(self.mono, 8))
        self.cmd_lbl.grid(row=1, column=0, columnspan=9, sticky="w", pady=(8, 0))

    def _tab_files(self, parent):
        f = ttk.Frame(parent, padding=10)
        f.rowconfigure(2, weight=1)
        f.columnconfigure(0, weight=1)

        head = ttk.Frame(f)
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="FBX files in the source folder",
                  style="Title.TLabel").pack(side="left")
        ttk.Button(head, text="Refresh", width=8,
                   command=self.refresh_files).pack(side="right")
        ttk.Label(f, text="Skin = the file that contains the mesh.  "
                          "Name = the animation name as it appears in Godot.  "
                          "In place = strip horizontal root motion.",
                  style="Dim.TLabel", wraplength=520, justify="left").grid(
            row=1, column=0, sticky="w", pady=(2, 8))

        wrap = ttk.Frame(f)
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        vs.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=vs.set)
        inner = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.bind_all("<MouseWheel>", lambda e: self._wheel(canvas, e))
        canvas.bind_all("<Button-4>", lambda e: self._wheel(canvas, e))    # X11
        canvas.bind_all("<Button-5>", lambda e: self._wheel(canvas, e))
        self.rows_frame = inner

        for i, (txt, w) in enumerate((("Skin", 5), ("Include", 7), ("File", 26),
                                      ("Name", 18), ("In place", 8), ("Status", 20))):
            ttk.Label(inner, text=txt, style="Head.TLabel").grid(
                row=0, column=i, padx=6, pady=(0, 4), sticky="w")
        ttk.Radiobutton(inner, text="auto", variable=self.skin_var, value="",
                        command=self.on_skin_change).grid(row=1, column=0,
                                                          columnspan=2, sticky="w", padx=8)
        ttk.Label(inner, text="(let Blender detect the skinned file)",
                  style="Dim.TLabel").grid(row=1, column=2, columnspan=4, sticky="w")
        self.rows_start = 2
        return f

    def _wheel(self, canvas, e):
        try:
            if not str(canvas.winfo_containing(e.x_root, e.y_root)).startswith(str(canvas)):
                return
            if getattr(e, "num", 0) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(e, "num", 0) == 5:
                canvas.yview_scroll(1, "units")
            else:
                canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1),
                                    "units")
        except Exception:
            pass

    def _tab_options(self, parent):
        outer = ttk.Frame(parent, padding=10)
        outer.columnconfigure(0, weight=1)

        g1 = ttk.LabelFrame(outer, text=" Build ", padding=10)
        g1.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        g1.columnconfigure(1, weight=1)

        r = 0
        ttk.Checkbutton(g1, text="Fixed height (metres)", variable=self.use_height,
                        command=self.refresh_cmd).grid(row=r, column=0, sticky="w")
        ttk.Entry(g1, textvariable=self.height, width=10).grid(row=r, column=1,
                                                               sticky="w", padx=6)
        ttk.Label(g1, text="1.75 is typical for a human; off = keep the source scale",
                  style="Dim.TLabel").grid(row=r, column=2, sticky="w")
        r += 1

        ttk.Label(g1, text="Rest pose").grid(row=r, column=0, sticky="w", pady=(8, 0))
        cb = ttk.Combobox(g1, textvariable=self.rest_mode, width=12, state="readonly",
                          values=("auto", "bind", "custom"))
        cb.grid(row=r, column=1, sticky="w", padx=6, pady=(8, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_cmd())
        ttk.Label(g1, text="auto = the animations' rest pose (recommended) | "
                           "bind = leave alone | custom = <Anim>:<frame>",
                  style="Dim.TLabel").grid(row=r, column=2, sticky="w", pady=(8, 0))
        r += 1
        ttk.Entry(g1, textvariable=self.rest_custom, width=20).grid(
            row=r, column=1, sticky="w", padx=6)
        ttk.Label(g1, text="when 'custom' is selected, e.g. Walking:12",
                  style="Dim.TLabel").grid(row=r, column=2, sticky="w")
        self.rest_custom.trace_add("write", lambda *_: self.refresh_cmd())
        r += 1

        ttk.Checkbutton(g1, text="Scene fps", variable=self.use_fps,
                        command=self.refresh_cmd).grid(row=r, column=0, sticky="w",
                                                       pady=(8, 0))
        ttk.Entry(g1, textvariable=self.fps, width=10).grid(row=r, column=1, sticky="w",
                                                            padx=6, pady=(8, 0))
        ttk.Label(g1, text="pin this if the files disagree on fps", style="Dim.TLabel").grid(
            row=r, column=2, sticky="w", pady=(8, 0))
        r += 1

        ttk.Label(g1, text="Root bone").grid(row=r, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(g1, textvariable=self.root_bone, width=20).grid(
            row=r, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(g1, text="empty = the skeleton's root bone (used to measure root motion)",
                  style="Dim.TLabel").grid(row=r, column=2, sticky="w", pady=(8, 0))
        self.root_bone.trace_add("write", lambda *_: self.refresh_cmd())
        r += 1

        ttk.Label(g1, text="Tolerance (m)").grid(row=r, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(g1, textvariable=self.tolerance, width=10).grid(
            row=r, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(g1, text="verification threshold, default 1e-4 (0.1 mm)",
                  style="Dim.TLabel").grid(row=r, column=2, sticky="w", pady=(8, 0))
        self.tolerance.trace_add("write", lambda *_: self.refresh_cmd())
        r += 1

        ttk.Checkbutton(g1, text="Keep the 'mixamorig:' prefix (not recommended for Godot)",
                        variable=self.keep_prefix, command=self.refresh_cmd).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))

        g2 = ttk.LabelFrame(outer, text=" Verify ", padding=10)
        g2.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Checkbutton(g2, text="Expected animations = the names on the Files tab",
                        variable=self.expect_anims, command=self.refresh_cmd).grid(
            row=0, column=0, sticky="w")
        ttk.Checkbutton(g2, text="Expected height = the height entered above",
                        variable=self.expect_height, command=self.refresh_cmd).grid(
            row=1, column=0, sticky="w")

        g3 = ttk.LabelFrame(outer, text=" Preview ", padding=10)
        g3.grid(row=2, column=0, sticky="ew")
        ttk.Label(g3, text="Frames per animation").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(g3, from_=1, to=12, textvariable=self.shots, width=6,
                    command=self.refresh_cmd).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(g3, text="render folder: <GLB name>_preview", style="Dim.TLabel").grid(
            row=0, column=2, sticky="w", padx=6)
        return outer

    def _tab_preview(self, parent):
        f = ttk.Frame(parent, padding=10)
        f.rowconfigure(1, weight=1)
        f.columnconfigure(0, weight=1)
        head = ttk.Frame(f)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(head, text="Renders", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="  (click an image to open it full size)",
                  style="Dim.TLabel").pack(side="left")
        ttk.Button(head, text="Open folder", width=12,
                   command=self.open_preview_dir).pack(side="right")
        ttk.Button(head, text="Refresh", width=8,
                   command=self.load_previews).pack(side="right", padx=(0, 6))

        wrap = ttk.Frame(f)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.gal_canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        self.gal_canvas.grid(row=0, column=0, sticky="nsew")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.gal_canvas.yview)
        vs.grid(row=0, column=1, sticky="ns")
        self.gal_canvas.configure(yscrollcommand=vs.set)
        self.gal = ttk.Frame(self.gal_canvas)
        gwin = self.gal_canvas.create_window((0, 0), window=self.gal, anchor="nw")
        self.gal.bind("<Configure>",
                      lambda e: self.gal_canvas.configure(
                          scrollregion=self.gal_canvas.bbox("all")))
        self.gal_canvas.bind("<Configure>",
                             lambda e: self.gal_canvas.itemconfigure(gwin, width=e.width))
        self.gal_empty = ttk.Label(self.gal, style="Dim.TLabel",
                                   text="No renders yet. Run '4. Preview'.")
        self.gal_empty.grid(row=0, column=0, sticky="w", padx=4, pady=4)
        return f

    def _tab_help(self, parent):
        f = ttk.Frame(parent, padding=10)
        f.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        t = tk.Text(f, wrap="word", bg="#141517", fg=FG, bd=0, padx=12, pady=10,
                    font=(self.ui_font, 9), insertbackground=FG)
        t.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        sb.grid(row=0, column=1, sticky="ns")
        t.configure(yscrollcommand=sb.set)
        t.tag_configure("h", foreground=ACCENT, font=(self.ui_bold, 11),
                        spacing1=10, spacing3=4)
        t.tag_configure("b", font=(self.ui_bold, 9))
        t.tag_configure("d", foreground=DIM)
        t.tag_configure("code", foreground=WARN, font=(self.mono, 9))

        def h(s): t.insert("end", s + "\n", "h")
        def p(s): t.insert("end", s + "\n")
        def d(s): t.insert("end", s + "\n", "d")
        def c(s): t.insert("end", s + "\n", "code")

        h("How to download from Mixamo")
        p("Character (once):   FBX Binary (.fbx), With Skin, T-Pose")
        p("Every animation:    FBX Binary (.fbx), Without Skin, 30 fps, "
          "keyframe reduction: none")
        d("Download the animations WITH THAT CHARACTER SELECTED. If they come from a "
          "different character the bone lengths will not match and this tool refuses to "
          "build (that would need real retargeting).")
        d("Forgot to tick 'In Place'? No problem: tick the 'In place' box for that "
          "animation on the Files tab.")

        h("How to use it")
        p("1. FBX folder: select the folder holding this character's FBX files.")
        p("2. Inspect: shows which file is the skinned one, whether the bone sets match, "
          "and how far apart the rest poses are. Run it once before building.")
        p("3. Build: writes a single Character.glb. It verifies itself; if the deviation "
          "exceeds the tolerance it fails instead of writing a broken file.")
        p("4. Verify: checks the generated GLB independently (raw glTF + reload).")
        p("5. Preview: renders the rest pose, frames from every animation, and a hand "
          "close-up.")
        d("Always look at the rest pose shot and the hand close-up - a rest pose mismatch "
          "shows up there before anywhere else.")

        h("Why this tool? (what makes copying channels break)")
        p("On Mixamo the skinned file and the animation files do not necessarily share "
          "the same rest (bind) pose. Blender stores animation channels relative to rest, "
          "so moving an action to a different rig silently corrupts it: the torso looks "
          "plausible while the arms and hands twist. This tool never copies channels; it "
          "samples the bone matrices in a common world space frame by frame and solves "
          "the target rig's pose.")

        h("On the Godot side")
        p("Scale:       if you did not use a fixed height, set Root Scale on the Import "
          "tab; the build output prints the right number.")
        p("Looping:     animations arrive without looping. Advanced Import Settings -> "
          "select the animation -> Loop Mode = Linear.")
        p("Root motion: AnimationTree -> Root Motion Track = Skeleton3D:<rootBone>, then "
          "get_root_motion_position(). Or tick 'In place' here and move the character "
          "from code.")
        p("Foot height: the Verify step tells you whether the feet sit at z=0.")

        h("Common errors")
        t.insert("end", "bone lengths differ ...\n", "b")
        d("  The animations came from a different character. Download them again from "
          "Mixamo with the same character selected.")
        t.insert("end", "no FBX contains a mesh\n", "b")
        d("  You forgot to download the character 'With Skin'.")
        t.insert("end", "<file> contains no action\n", "b")
        d("  No animation was selected when that file was downloaded.")
        t.insert("end", "shape keys - the rest pose cannot be changed\n", "b")
        d("  Set Rest pose = bind on the Options tab and try again.")
        t.insert("end", "baked animation does not match the source\n", "b")
        d("  Verification did its job and no broken file was written. If your Blender "
          "version changed, see the pitfalls section in README.md.")

        h("Files")
        c("inspect_fbx.py      pre-flight check, rest pose comparison")
        c("build_character.py  the real work: merge, retarget, verify, write the GLB")
        c("verify_glb.py       verify the generated GLB independently")
        c("render_preview.py   preview renders for eyeball checks")
        c("easymixamo.py       command line front end (does what this GUI does, in a shell)")
        d("The build also saves <output>_source.blend, so you can carry on there to fix "
          "something by hand or add another animation.")
        t.configure(state="disabled")
        return f

    def _place_sash(self):
        """Give the log panel a sensible default width (or the saved one)."""
        try:
            w = self.mid.winfo_width()
            if w > 200:
                self.mid.sashpos(0, int(self.cfg.get("sash", 0)) or int(w * 0.52))
        except Exception:
            pass

    # ------------------------------------------------------------- state
    def _restore_cfg(self):
        skin = self.cfg.get("skin", "")
        if skin:
            self.skin_var.set(skin)
        self.out_overridden = bool(self.cfg.get("out_overridden"))

    def collect_cfg(self):
        try:
            sash = self.mid.sashpos(0)
        except Exception:
            sash = 0
        return {
            "geometry": self.geometry(),
            "sash": sash,
            "blender": self.blender.get(),
            "src": self.src.get(),
            "out": self.out.get(),
            "out_overridden": self.out_overridden,
            "skin": self.skin_var.get(),
            "use_height": self.use_height.get(),
            "height": self.height.get(),
            "rest_mode": self.rest_mode.get(),
            "rest_custom": self.rest_custom.get(),
            "use_fps": self.use_fps.get(),
            "fps": self.fps.get(),
            "keep_prefix": self.keep_prefix.get(),
            "root_bone": self.root_bone.get(),
            "tolerance": self.tolerance.get(),
            "expect_anims": self.expect_anims.get(),
            "expect_height": self.expect_height.get(),
            "shots": self.shots.get(),
            "rows": {r.fname: {"name": r.name.get(),
                               "include": r.include.get(),
                               "in_place": r.in_place.get()} for r in self.rows},
        }

    def on_close(self):
        if self.runner.busy():
            if not messagebox.askyesno("Running",
                                       "A job is still running. Close anyway?"):
                return
            self.runner.cancel()
        save_cfg(self.collect_cfg())
        self.destroy()

    # ------------------------------------------------------------- file pickers
    def pick_blender(self):
        types = [("blender.exe", "blender.exe"), ("All files", "*.*")] \
            if sys.platform == "win32" else [("All files", "*.*")]
        p = filedialog.askopenfilename(title="Select the Blender executable",
                                       filetypes=types)
        if p:
            self.blender.set(p)
            self.refresh_cmd()

    def pick_src(self):
        p = filedialog.askdirectory(title="Select the FBX folder",
                                    initialdir=self.src.get() or os.path.expanduser("~"))
        if p:
            self.src.set(os.path.normpath(p))

    def pick_out(self):
        init = self.out.get() or os.path.join(self.src.get() or "", "Character.glb")
        p = filedialog.asksaveasfilename(title="Output GLB", defaultextension=".glb",
                                         initialfile=os.path.basename(init) or "Character.glb",
                                         initialdir=os.path.dirname(init) or None,
                                         filetypes=[("glTF Binary", "*.glb")])
        if p:
            self.out.set(os.path.normpath(p))
            self.out_overridden = True

    def on_src_change(self):
        src = self.src.get()
        if not self.out_overridden and os.path.isdir(src):
            self.out.set(os.path.join(src, "Character.glb"))
        self.refresh_files()

    def refresh_files(self):
        for r in self.rows:
            r.destroy()
        self.rows = []
        src = self.src.get()
        if not os.path.isdir(src):
            self.refresh_cmd()
            return
        saved = self.cfg.get("rows", {})
        files = sorted(f for f in os.listdir(src) if f.lower().endswith(".fbx"))
        for i, f in enumerate(files):
            row = FileRow(self.rows_frame, f, self, self.rows_start + i)
            s = saved.get(f)
            if isinstance(s, dict):
                row.name.set(s.get("name", row.name.get()))
                row.include.set(s.get("include", True))
                row.in_place.set(s.get("in_place", False))
            self.rows.append(row)
        if self.skin_var.get() and self.skin_var.get() not in files:
            self.skin_var.set("")
        self.on_skin_change()
        if not files:
            self.log_line(f"No .fbx files in this folder: {src}", WARN)

    def on_skin_change(self):
        skin = self.skin_var.get()
        for r in self.rows:
            r.set_is_skin(r.fname == skin)
        self.refresh_cmd()

    # ------------------------------------------------------------- arguments
    def anim_rows(self):
        skin = self.skin_var.get()
        return [r for r in self.rows if r.fname != skin and r.include.get()]

    def auto_skin(self):
        """True when no skinned file has been picked and Blender should decide."""
        return not self.skin_var.get()

    def row_defaults_touched(self):
        """Has anything been changed that 'auto' cannot express?"""
        return any(not r.include.get()
                   or r.name.get().strip() != os.path.splitext(r.fname)[0]
                   for r in self.rows)

    def anim_names(self):
        """Animation names as they will end up in the GLB."""
        if self.auto_skin():
            # build_character.py picks the skinned file itself and names the rest after
            # their file names - which is exactly what an untouched row holds
            return [os.path.splitext(r.fname)[0] for r in self.rows]
        return [r.name.get().strip() or os.path.splitext(r.fname)[0]
                for r in self.anim_rows()]

    def args_for(self, cmd):
        src = self.src.get()
        out = self.out.get() or os.path.join(src, "Character.glb")
        if cmd == "inspect":
            return ["--src", src]
        if cmd == "build":
            a = ["--src", src, "--out", out]
            # In auto mode pass neither --skin nor --anim: naming one without the other
            # would hand the skinned file to the build as an animation as well.
            if not self.auto_skin():
                a += ["--skin", self.skin_var.get()]
                for r in self.anim_rows():
                    name = r.name.get().strip() or os.path.splitext(r.fname)[0]
                    a += ["--anim", f"{r.fname}={name}"]
            mode = self.rest_mode.get()
            rest = self.rest_custom.get().strip() if mode == "custom" else mode
            if rest and rest != "auto":
                a += ["--rest", rest]
            if self.use_height.get() and self.height.get().strip():
                a += ["--target-height", self.height.get().strip()]
            for r in (self.rows if self.auto_skin() else self.anim_rows()):
                if r.in_place.get():
                    a += ["--in-place", os.path.splitext(r.fname)[0] if self.auto_skin()
                          else (r.name.get().strip() or os.path.splitext(r.fname)[0])]
            if self.root_bone.get().strip():
                a += ["--root-bone", self.root_bone.get().strip()]
            if self.use_fps.get() and self.fps.get().strip():
                a += ["--fps", self.fps.get().strip()]
            if self.keep_prefix.get():
                a += ["--keep-prefix"]
            tol = self.tolerance.get().strip()
            if tol and tol != "1e-4":
                a += ["--tolerance", tol]
            return a
        if cmd == "verify":
            a = ["--glb", out]
            # In auto mode we do not know which file is the skinned one, so we cannot
            # say which names should be in the GLB - skip the check rather than fail it.
            if self.expect_anims.get() and not self.auto_skin():
                names = self.anim_names()
                if names:
                    a += ["--expect-anims", ",".join(names)]
            if self.expect_height.get() and self.use_height.get() and self.height.get().strip():
                a += ["--expect-height", self.height.get().strip()]
            return a
        if cmd == "preview":
            a = ["--glb", out, "--out", self.preview_dir()]
            if self.shots.get().strip() and self.shots.get().strip() != "3":
                a += ["--shots", self.shots.get().strip()]
            return a
        return []

    def preview_dir(self):
        out = self.out.get() or os.path.join(self.src.get(), "Character.glb")
        return os.path.splitext(out)[0] + "_preview"

    def validate(self, cmds):
        b = self.blender.get()
        if not b or not os.path.isfile(b):
            messagebox.showerror("No Blender",
                                 "Blender was not found. Select it on the top row.")
            return False
        src = self.src.get()
        if {"inspect", "build"} & set(cmds):
            if not os.path.isdir(src):
                messagebox.showerror("No folder", "Select a valid FBX folder.")
                return False
            if not [f for f in os.listdir(src) if f.lower().endswith(".fbx")]:
                messagebox.showerror("No FBX", f"There are no .fbx files here:\n{src}")
                return False
        if "build" in cmds:
            if self.auto_skin() and self.row_defaults_touched():
                messagebox.showerror(
                    "Pick the skinned file",
                    "Renaming an animation or leaving one out only works once the "
                    "skinned file is known.\n\nRun '1. Inspect' (it selects the skinned "
                    "file for you), or tick it in the Skin column.")
                return False
            if not self.auto_skin() and not self.anim_rows():
                messagebox.showerror("No animation",
                                     "At least one animation file must be included.")
                return False
            names = self.anim_names()
            if len(set(names)) != len(names):
                messagebox.showerror("Duplicate name", "Animation names must be unique.")
                return False
            if self.use_height.get():
                try:
                    if float(self.height.get()) <= 0:
                        raise ValueError
                except ValueError:
                    messagebox.showerror("Height",
                                         "Height must be a positive number, e.g. 1.75.")
                    return False
            if self.rest_mode.get() == "custom" and ":" not in self.rest_custom.get():
                messagebox.showerror("Rest pose", "Custom rest format: <AnimName>:<frame>")
                return False
        if {"verify", "preview"} & set(cmds) and "build" not in cmds:
            out = self.out.get()
            if not out or not os.path.isfile(out):
                messagebox.showerror("No GLB",
                                     f"Build the GLB first, or select an existing "
                                     f"file:\n{out}")
                return False
        return True

    # ------------------------------------------------------------- running
    def run(self, cmds):
        if self.runner.busy():
            messagebox.showinfo("Busy", "A job is already running.")
            return
        if not self.validate(cmds):
            return
        save_cfg(self.collect_cfg())
        jobs = []
        for c in cmds:
            script = os.path.join(APP_DIR, SCRIPTS[c])
            if not os.path.isfile(script):
                messagebox.showerror("Missing script", f"Not found: {script}")
                return
            jobs.append((c, self.blender.get(), script, self.args_for(c)))
        self._set_running(True)
        self.log.configure(state="normal")
        self.log.insert("end", "\n")
        self.log.configure(state="disabled")
        self.runner.run_chain(jobs)

    def cancel(self):
        self.runner.cancel()
        self.log_line("stopped by the user", WARN)

    def _set_running(self, on):
        for b in (self.btn_inspect, self.btn_build, self.btn_verify,
                  self.btn_preview, self.btn_all):
            b.configure(state="disabled" if on else "normal")
        self.btn_cancel.configure(state="normal" if on else "disabled")
        if on:
            self.pb.grid()
            self.pb.start(12)
        else:
            self.pb.stop()
            self.pb.grid_remove()
            self.status.set("ready")

    # ------------------------------------------------------------- log
    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.log.get("1.0", "end-1c"))
        self.status.set("log copied to the clipboard")

    def log_line(self, text, color=None, tag=None):
        self.log.configure(state="normal")
        t = tag or self._tag_for(text, color)
        self.log.insert("end", text + "\n", t)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _tag_for(self, text, color):
        if color == OK:
            return "ok"
        if color == ERR:
            return "err"
        if color == WARN:
            return "warn"
        if color == DIM:
            return "dim"
        low = text.lower()
        if "error:" in low or text.strip().startswith("!!") or "failed" in low \
                or ("problem" in low and "no problems" not in low):
            return "err"
        if "warning" in low or low.startswith("  note:") or "!!" in text:
            return "warn"
        if "no problems" in low or "wrote " in low or "all animations" in low \
                or low.startswith("ok "):
            return "ok"
        if set(text.strip()) in ({"="}, {"-"}) and len(text.strip()) > 4:
            return "dim"
        return ""

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self.log_line(payload)
                elif kind == "start":
                    label, cmd = payload
                    self.current_label = label
                    self.status.set(f"running {label}...")
                    self.log_line(f"> {label}", tag="hdr")
                    self.log_line("  " + subprocess.list2cmdline(cmd), tag="dim")
                elif kind == "done":
                    label, rc, secs = payload
                    if rc == 0:
                        self.log_line(f"OK ({label}) - {secs:.1f}s", tag="ok")
                        self._after_success(label)
                    else:
                        self.log_line(f"FAILED ({label}), exit code {rc} - {secs:.1f}s",
                                      tag="err")
                elif kind == "chain_done":
                    self._set_running(False)
                    self.status.set("stopped" if payload else "done")
        except queue.Empty:
            pass
        self.after(60, self._pump)

    def _after_success(self, label):
        if label == "inspect":
            self._apply_inspect_result()
        elif label == "preview":
            self.load_previews()
            self.nb.select(2)

    def _apply_inspect_result(self):
        """Read which file is the skinned one out of the inspect output, apply it to the UI."""
        text = self.log.get("1.0", "end")
        skinned, animonly = [], []
        for line in text.splitlines():
            if "-> SKINNED" in line:
                skinned.append(line.split("->")[0].strip())
            elif "-> animation-only" in line:
                animonly.append(line.split("->")[0].strip())
        for r in self.rows:
            if r.fname in skinned:
                r.set_info("skinned (has a mesh)", ACCENT)
            elif r.fname in animonly:
                r.set_info("animation", DIM)
        if len(skinned) == 1 and not self.skin_var.get():
            self.skin_var.set(skinned[0])
            self.on_skin_change()
            self.log_line(f"  -> skinned file selected: {skinned[0]}", tag="dim")

    # ------------------------------------------------------------- preview
    def load_previews(self):
        for w in self.gal.winfo_children():
            w.destroy()
        self.thumbs = []
        d = self.preview_dir()
        pngs = sorted(glob.glob(os.path.join(d, "*.png"))) if os.path.isdir(d) else []
        if not pngs:
            ttk.Label(self.gal, style="Dim.TLabel",
                      text=f"No renders found:\n{d}\n\nRun '4. Preview'.",
                      justify="left").grid(row=0, column=0, sticky="w", padx=4, pady=4)
            return
        cols = 3
        for i, p in enumerate(pngs):
            try:
                img = tk.PhotoImage(file=p)
            except tk.TclError:
                continue
            target = int(220 * DPI)
            k = max(1, -(-img.width() // target))
            th = img.subsample(k, k)
            self.thumbs.append(th)
            cell = ttk.Frame(self.gal, padding=4)
            cell.grid(row=(i // cols) * 2, column=i % cols, padx=4, pady=4, sticky="n")
            lb = tk.Label(cell, image=th, bg=BG2, bd=0, cursor="hand2")
            lb.pack()
            lb.bind("<Button-1>", lambda e, path=p: self._open(path))
            ttk.Label(cell, text=os.path.basename(p), style="Dim.TLabel",
                      wraplength=target).pack(pady=(3, 0))
        self.status.set(f"{len(pngs)} renders loaded")

    def open_preview_dir(self):
        d = self.preview_dir()
        if os.path.isdir(d):
            self._open(d)
        else:
            messagebox.showinfo("No folder", f"It does not exist yet:\n{d}")

    def _open(self, path):
        try:
            open_in_file_manager(path)
        except Exception as e:
            messagebox.showerror("Could not open", str(e))

    # ------------------------------------------------------------- command preview
    def refresh_cmd(self, *_):
        if not hasattr(self, "cmd_lbl"):
            return
        try:
            args = self.args_for("build")
        except Exception:
            return
        script = os.path.join(APP_DIR, SCRIPTS["build"])
        cmd = subprocess.list2cmdline([self.blender.get() or "blender",
                                       "--factory-startup", "-b", "--python", script,
                                       "--", *args])
        self.cmd_lbl.configure(text="build command:  " + cmd)


def main():
    global DPI
    DPI = enable_dpi_awareness()
    missing = [s for s in SCRIPTS.values() if not os.path.isfile(os.path.join(APP_DIR, s))]
    app = App()
    if missing:
        messagebox.showwarning(
            "Missing scripts",
            "These files must sit next to the GUI:\n  " + "\n  ".join(missing))
    app.mainloop()


if __name__ == "__main__":
    main()
