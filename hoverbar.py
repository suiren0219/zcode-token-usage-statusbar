#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZCode Token 用量 · 独立悬浮条（不依赖 asar 注入）

为什么有这个：注入方案改的是 resources/app.asar，官方每次升级都会整包替换把它抹掉，
重装还得先完全退出客户端。这个悬浮条走另一条路——独立进程 + 无边框窗口，
只读 ~/.zcode/cli/db/db.sqlite，跟客户端怎么升级完全无关。

外观照注入版状态条还原：半透明深色圆角胶囊、1px 细描边、数值三档配色、
上下文带微进度条；位置默认贴在输入卡片下方。

用法：
    python hoverbar.py              # 启动（已在运行则打开设置面板）
    python hoverbar.py --settings   # 打开设置面板
    python hoverbar.py --quit       # 关掉正在运行的悬浮条
    python hoverbar.py --once       # 只在终端打印一次数据，不开窗（排查用）
    python hoverbar.py --debug      # 开窗并打印定位/取数过程

打开设置（三种等价入口）：点条面最右端的切角方块（悬停变酸黄）、双击条面、
右键菜单「设置…」，或命令行 --settings。设置面板用终末地（endfield-ui）风格：
acid yellow #FFFA00 唯一强调色、切角=可点、展示容器直角、1px 细线分区。

设置项：位置 / 跟随窗口还是整块屏幕 / 偏移 / 透明度 / 字号 / 显示哪些项 /
失焦是否隐藏 / 是否置顶。拖动条面可直接改位置。
"""
from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

HERE = Path(__file__).parent.resolve()
ZUSAGE = HERE / "zusage.py"
CONFIG = HERE / "hoverbar.json"
LOCK = Path(os.environ.get("TEMP", ".")) / "zusage-hoverbar.lock"
IPC_PORT = 47615          # --settings 唤起已在运行实例的控制端口

# 注入版状态条的配色（overlay.js 同名变量）
BG = "#0f1219"
KEY = "#010203"           # -transparentcolor 键色：胶囊外的直角区域
BORDER = "#2b2f39"
FG = "#e9edf4"
DIM = "#7e8899"
LABEL = "#8791a3"
OK = "#3ecf8e"
WARM = "#f5b944"
HOT = "#ff6b57"
BADGE = "#ff8a73"

# 终末地（endfield-ui）配色：设置面板用。唯一强调色 acid yellow 只做填充，
# 只表示"当前 / 可点 / 注意"；切角=可点，展示容器一律直角；radius 全程 0；
# 1px 细线分区，无阴影无发光无渐变。
EF_PAPER = "#191919"
EF_PANEL = "#222222"
EF_INK = "#EDEDED"
EF_INK2 = "#A3A3A3"
EF_RULE = "#353535"
EF_CHIP = "#2E2E2E"
EF_SIGNAL = "#FFFA00"
EF_ON_SIGNAL = "#161616"
EF_DANGER = "#FF8A7A"
EF_TRI_A = "#FF00F0"
EF_TRI_B = "#FFFA00"
EF_TRI_C = "#00FFA2"
EF_CMD = "#161616"        # 顶部命令栏（两套主题都深色）

ANCHORS = ("bottom-left", "bottom-center", "bottom-right",
           "top-left", "top-center", "top-right", "free")
ANCHOR_TXT = {
    "bottom-left": "左下", "bottom-center": "下中", "bottom-right": "右下",
    "top-left": "左上", "top-center": "上中", "top-right": "右上",
    "free": "自定义（拖动过）",
}

DEFAULTS = {
    "anchor": "bottom-left",
    "scope": "window",        # window=跟随 ZCode 窗口 / screen=整块屏幕
    "offset_x": 16,
    "offset_y": 6,
    "free_x": 0,
    "free_y": 0,
    "opacity": 0.92,
    "font_px": 13,
    "font_family": "",
    "always_on_top": False,   # 默认靠"属主窗口"绑定：只浮在 ZCode 之上，不盖别的程序
    "hide_when_inactive": False,  # 属主绑定已随 ZCode 最小化/切换自动让路
    "show": {"speed": True, "ctx": True, "turn": True,
             "session": True, "today": True, "tools": True},
    "labels": True,
    "poll_ms": 1500,
}

DEBUG = False


def log(*a):
    if DEBUG:
        print("[hoverbar]", *a, flush=True)


# ---------------------------------------------------------------- DPI / Win32
def _init_dpi() -> float:
    """开进程级 DPI 感知，返回缩放比（96dpi=1.0）。不开的话坐标会被系统再缩放一次。"""
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    gdi = ctypes.WinDLL("gdi32")
    u32 = ctypes.WinDLL("user32")
    hdc = u32.GetDC(0)
    try:
        dpi = gdi.GetDeviceCaps(hdc, 88) or 96      # LOGPIXELSX
    finally:
        u32.ReleaseDC(0, hdc)
    return dpi / 96.0


SCALE = _init_dpi()


def _u32():
    import ctypes
    return ctypes.WinDLL("user32", use_last_error=True)


def zcode_hwnd() -> int:
    """ZCode 主窗口：可见、有标题含 ZCode、非工具窗口、足够大，取最大的那个。"""
    import ctypes
    from ctypes import wintypes

    u32 = _u32()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        if "ZCode" not in buf.value:
            return True
        if u32.GetWindowLongW(hwnd, -20) & 0x00000080:   # WS_EX_TOOLWINDOW
            return True
        r = wintypes.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w < 400 or h < 300:
            return True
        found.append((w * h, hwnd))
        return True

    u32.EnumWindows(cb, 0)
    return max(found)[1] if found else 0


def win_state(hwnd: int):
    """(left, top, w, h)；最小化或句柄失效返回 None。"""
    import ctypes
    from ctypes import wintypes

    if not hwnd:
        return None
    u32 = _u32()
    if not u32.IsWindow(hwnd) or u32.IsIconic(hwnd):
        return None
    r = wintypes.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def monitor_work(hwnd: int):
    """ZCode 所在显示器的工作区（已排除任务栏）。"""
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    u32 = _u32()
    MONITOR_DEFAULTTONEAREST = 2
    mon = u32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if mon and u32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        r = mi.rcWork
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    r = wintypes.RECT()
    u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)   # SPI_GETWORKAREA
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def foreground_is(hwnd: int) -> bool:
    import ctypes
    u32 = _u32()
    fg = u32.GetForegroundWindow()
    if not fg or not hwnd:
        return False
    return fg == hwnd or u32.GetAncestor(fg, 2) == hwnd   # GA_ROOT=2


# ---------------------------------------------------------------- 设置读写
def load_settings() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))     # 深拷贝
    try:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    for k, v in raw.items():
        if k == "show" and isinstance(v, dict):
            cfg["show"].update({kk: bool(vv) for kk, vv in v.items()
                                if kk in cfg["show"]})
        elif k in cfg:
            cfg[k] = v
    return cfg


def save_settings(cfg: dict):
    try:
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    except OSError as e:
        log("保存设置失败", e)


# ---------------------------------------------------------------- 取数
class SnapshotSource:
    """常驻 `zusage.py serve`：stdin 一行请求，stdout 一行 JSON。
    比每次起解释器省 60–100ms，也不会每 1.5s 闪一个控制台。"""

    def __init__(self, interval_ms: int):
        self.interval = max(300, interval_ms) / 1000.0
        self.q: queue.Queue = queue.Queue()
        self._proc = None
        self._stop = False
        self._th = None

    def start(self):
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def stop(self):
        self._stop = True
        self._kill()

    def _kill(self):
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass

    def alive(self) -> bool:
        return bool(self._th and self._th.is_alive())

    def _spawn(self):
        self._proc = subprocess.Popen(
            [sys.executable, str(ZUSAGE), "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _loop(self):
        while not self._stop:
            t0 = time.time()
            try:
                if self._proc is None:
                    self._spawn()
                self._proc.stdin.write("\n")
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
                if not line:
                    raise RuntimeError("serve 已退出")
                d = json.loads(line)
                if isinstance(d, dict) and not d.get("error"):
                    self.q.put(d)
                else:
                    log("远端查询报错", d.get("error") if isinstance(d, dict) else d)
            except Exception as e:
                log("取数失败，重启 serve：", e)
                self._kill()
                time.sleep(0.5)
            time.sleep(max(0.2, self.interval - (time.time() - t0)))


def fmt(n) -> str:
    n = float(n or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return ("%.1f" % (n / div)).rstrip("0").rstrip(".") + unit
    return str(int(n))


def sec(ms) -> str:
    ms = float(ms or 0)
    if ms <= 0:
        return "-"
    if ms < 1000:
        return "%dms" % ms
    s = ms / 1000.0
    if s < 60:
        return "%.1fs" % s
    return "%d:%02d" % (s // 60, s % 60)


# ---------------------------------------------------------------- 悬浮条
class HoverBar:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.data: dict | None = None
        self._last_draw = None
        self._hwnd = 0
        self._bar_hwnd = 0
        self._drag = None
        self._hover = None
        self._gear_armed = False
        self._win_rect = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.configure(bg=KEY)
        try:
            self.root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            self.root.configure(bg=BG)
        self._apply_winprops()

        self.canvas = tk.Canvas(self.root, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack()

        self.font = None
        self._pick_font()

        self.canvas.bind("<Button-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Double-Button-1>", lambda e: self.open_settings())
        self.canvas.bind("<Button-3>", self._menu)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))
        self.root.bind("<Escape>", lambda e: None)

        self.src = SnapshotSource(cfg["poll_ms"])
        self.src.start()

        self._start_ipc()
        self.root.after(60, self._tick_data)
        self.root.after(80, self._tick_anchor)

    # -- 窗口属性 --
    def _apply_winprops(self):
        self.root.attributes("-alpha", min(1.0, max(0.25, float(self.cfg["opacity"]))))
        try:
            self.root.attributes("-topmost", bool(self.cfg["always_on_top"]))
        except tk.TclError:
            pass

    def _pick_font(self):
        fams = set(tkfont.families())
        want = self.cfg.get("font_family") or ""
        order = [want] if want else []
        order += ["Cascadia Mono", "Consolas", "JetBrains Mono", "Sarasa Mono SC",
                  "Microsoft YaHei UI", "Microsoft YaHei"]
        fam = next((f for f in order if f and f in fams), "Consolas")
        px = max(9, int(round(float(self.cfg["font_px"]) * SCALE)))
        self.font = tkfont.Font(family=fam, size=-px)
        self._px = px

    # -- 数据轮询 --
    def _tick_data(self):
        got = False
        while True:
            try:
                self.data = self.src.q.get_nowait()
                got = True
            except queue.Empty:
                break
        if got:
            self._draw()
        if not self.src.alive():           # 后台线程挂了 → 拉起来
            self.src = SnapshotSource(self.cfg["poll_ms"])
            self.src.start()
        if got:
            self._draw()
        self.root.after(self.cfg["poll_ms"], self._tick_data)

    # -- 拼内容 --
    def _items(self):
        d = self.data or {}
        s = d.get("session") or {}
        lt = d.get("last_turn") or {}
        last = d.get("last") or {}
        today = d.get("today") or {}
        tls = d.get("tools") or {}
        show = self.cfg["show"]
        lab = self.cfg["labels"]
        items = []

        tps = last.get("tps") or 0
        if show.get("speed") and tps:
            col = OK if tps >= 70 else WARM if tps >= 40 else HOT
            it = []
            if lab:
                it.append(("t", "速度", LABEL))
            it += [("t", "%.1f" % tps, col), ("t", "t/s", DIM)]
            items.append(it)

        cw = int(d.get("context_window") or 0)
        ctx = s.get("ctx") or 0
        if show.get("ctx") and cw and ctx:
            pct = ctx / cw * 100.0
            big = cw >= 1_000_000
            col = HOT if pct >= (60 if big else 85) else \
                WARM if pct >= (40 if big else 70) else OK
            it = []
            if lab:
                it.append(("t", "上下文", LABEL))
            it += [("bar", min(100.0, pct), col), ("t", "%.1f%%" % pct, col)]
            items.append(it)

        if show.get("turn") and (lt.get("total") or lt.get("requests")):
            it = []
            if lab:
                it.append(("t", "本轮", LABEL))
            it.append(("t", fmt(lt.get("total")), FG))
            tail = "%d次" % (lt.get("requests") or 0)
            if lt.get("duration_ms"):
                tail += " " + sec(lt["duration_ms"])
            it.append(("t", tail, DIM))
            items.append(it)

        if show.get("session") and s.get("total"):
            it = []
            if lab:
                it.append(("t", "会话", LABEL))
            it += [("t", fmt(s["total"]), FG),
                   ("t", "%d轮 %d次" % (s.get("turns") or 0, s.get("requests") or 0), DIM)]
            items.append(it)

        if show.get("today") and today.get("total"):
            it = []
            if lab:
                it.append(("t", "今日", LABEL))
            it.append(("t", fmt(today["total"]), FG))
            items.append(it)

        if show.get("tools") and tls.get("total"):
            it = []
            if lab:
                it.append(("t", "工具", LABEL))
            it.append(("t", str(tls["total"]), FG))
            if tls.get("errors"):
                it.append(("t", "✕%d" % tls["errors"], BADGE))
            items.append(it)

        if d.get("remote"):
            items.insert(0, [("t", "远端", LABEL)])
        if d.get("not_local"):
            items.insert(0, [("t", "不在本地库", WARM)])
        return items
    # -- 绘制 --
    def _draw(self):
        items = self._items()
        if not items:
            items = [[("t", "等待数据…", DIM)]]

        pad, gap, inner = 10, 9, 4
        GAP = self.font.measure(" ")
        BAR_W_HINT = 46

        def twidth(t):
            return self.font.measure(t)

        blocks = []       # (宽, [draw ops])
        for it in items:
            ops, w = [], 0
            for tok in it:
                if tok[0] == "t":
                    _, txt, col = tok
                    ops.append(("t", txt, col))
                    w += twidth(txt)
                else:
                    _, pct, col = tok
                    ops.append(("bar", pct, col))
                    w += BAR_W_HINT
                w += GAP
            w -= GAP
            blocks.append((max(0, w), ops))

        sep_w = 1
        # 右端设置入口：方块边长跟着字号走，前面留 12px 与最后一项断开
        gear_s = self._gear_size()
        gear_gap = 12
        total = (pad * 2 + sum(b[0] for b in blocks)
                 + sep_w * len(blocks) + gap * 2 * (len(blocks) - 1)
                 + gear_gap + gear_s)
        h = self._px + 14
        total = int(total)
        if total < 60:
            total = 60

        self.canvas.configure(width=total, height=h)
        self.canvas.delete("all")

        r = 9
        x1, y1, x2, y2 = 0.5, 0.5, total - 1.0, h - 1.0
        pts = [x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2, y2 - r, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1, x1 + r, y1]
        self.canvas.create_polygon(pts, smooth=True, splinesteps=20,
                                   fill=BG, outline=BORDER, width=1)

        cy = h / 2.0
        x = pad
        for i, (w, ops) in enumerate(blocks):
            for op in ops:
                if op[0] == "t":
                    _, txt, col = op
                    self.canvas.create_text(x, cy, text=txt, fill=col,
                                            font=self.font, anchor="w")
                    x += twidth(txt) + GAP
                else:
                    _, pct, col = op
                    bh = 5
                    bw = BAR_W_HINT
                    by = cy - bh / 2.0
                    self.canvas.create_polygon(
                        [x + 2.5, by, x + bw - 2.5, by, x + bw, by + bh / 2,
                         x + bw - 2.5, by + bh, x + 2.5, by + bh, x, by + bh / 2],
                        smooth=False, fill="#272c36", outline="")
                    fw = max(0.0, bw * pct / 100.0)
                    if fw > 1:
                        self.canvas.create_polygon(
                            [x + 2.5, by, x + max(2.6, fw - 2.5), by,
                             x + fw, by + bh / 2, x + max(2.6, fw - 2.5), by + bh,
                             x + 2.5, by + bh, x, by + bh / 2],
                            smooth=False, fill=col, outline="")
                    x += bw + GAP
            x -= GAP
            if i < len(blocks) - 1:
                x += gap
                self.canvas.create_line(x, cy - 7, x, cy + 7, fill="#2f343e")
                x += sep_w + gap

        # 右端 ⚙：整条最后一项，切角方块 —— 终末地语义里"切角=可点"。
        # 悬停变 acid yellow 反白，点击开设置面板。
        # 方块右边距与左边 pad 对齐，视觉上才是"紧贴末端"而不是悬在中间。
        self._draw_gear(total - pad - gear_s, cy, h)

        self._last_draw = (total, h)
        self._place()

    def _gear_size(self):
        """齿轮方块边长：跟字号走，但不小于 14，免得小字号点不中。"""
        size = max(14, self._px)
        return int(size)

    def _gear_rect(self):
        """返回齿轮热区 (x1, y1, x2, y2)；没画过就是空。"""
        return getattr(self, "_gear_box", (0, 0, 0, 0))

    def _draw_gear(self, x, cy, h):
        s = self._gear_size()
        y1 = cy - s / 2.0
        y2 = cy + s / 2.0
        x2 = x + s
        # 右下角切 4px：终末地"可点/可操作"的视觉标记，不是装饰
        cut = 4
        pts = [x, y1, x2, y1, x2, y2 - cut, x2 - cut, y2, x, y2]
        hover = isinstance(self._hover, str) and self._hover == "gear"
        self.gear_bg = self.canvas.create_polygon(
            pts, smooth=False,
            fill=EF_SIGNAL if hover else EF_CHIP,
            outline="")
        gcol = EF_ON_SIGNAL if hover else EF_INK2
        c = s / 2.0
        r = max(3.0, s * 0.30)
        self.gear_glyph = self.canvas.create_oval(
            x + c - r, cy - r, x + c + r, cy + r,
            outline=gcol, width=1.2, fill="")
        # 齿：四根短线，绕中心 45° 交叉，够小仍能读成齿轮
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            self.canvas.create_line(
                x + c + dx * r * 0.72, cy + dy * r * 0.72,
                x + c + dx * (r + 1.8), cy + dy * (r + 1.8),
                fill=gcol, width=1.2)
        self._gear_box = (x, y1, x2, y2)

    # -- 定位 --
    def _place(self):
        if not self._last_draw:
            return
        bw, bh = self._last_draw
        cfg = self.cfg
        anchor = cfg["anchor"]

        if anchor == "free":
            x, y = int(cfg["free_x"]), int(cfg["free_y"])
            if not self._visible:
                self._show()
            self.root.geometry("%dx%d+%d+%d" % (bw, bh, x, y))
            return

        self._hwnd = zcode_hwnd()
        wr = win_state(self._hwnd)
        if not wr or (cfg["hide_when_inactive"] and not foreground_is(self._hwnd)):
            self._hide()
            return
        self._win_rect = wr

        if cfg["scope"] == "screen":
            rx, ry, rw, rh = monitor_work(self._hwnd)
        else:
            rx, ry, rw, rh = wr
        ox, oy = int(cfg["offset_x"]), int(cfg["offset_y"])

        v, hpart = anchor.split("-", 1)          # bottom / top, left / center / right
        x = rx + (rw - bw) // 2 + ox if hpart == "center" else \
            rx + rw - bw - ox if hpart == "right" else rx + ox
        y = ry + rh - bh - oy if v == "bottom" else ry + oy

        self._show()
        self.root.geometry("%dx%d+%d+%d" % (bw, bh, x, y))
        self._bind_zorder()

    _visible = False

    def _show(self):
        if not self._visible:
            self.root.deiconify()
            self._visible = True

    def _hide(self):
        if self._visible:
            self.root.withdraw()
            self._visible = False

    def _bind_zorder(self):
        """把条设成 ZCode 的「属主窗口」(GWLP_HWNDPARENT)。
        Windows 原生绑定：属主窗口恒浮在属主之上、随属主最小化一起隐藏、不抢焦点、
        不进任务栏——不用 -topmost 也不会盖住别的程序，这才是真"绑在 ZCode 上"。"""
        if self._hwnd <= 0:
            return
        import ctypes
        u32 = _u32()
        if not self._bar_hwnd:
            self._bar_hwnd = u32.GetAncestor(self.root.winfo_id(), 2)   # GA_ROOT
            if not self._bar_hwnd:
                return
            ex = u32.GetWindowLongW(self._bar_hwnd, -20)               # GWL_EXSTYLE
            u32.SetWindowLongW(self._bar_hwnd, -20,
                               ex | 0x00000080 | 0x08000000)           # TOOLWINDOW|NOACTIVATE
        if getattr(self, "_owner", None) == self._hwnd:
            return
        try:
            set_owner = ctypes.windll.user32.SetWindowLongPtrW
        except AttributeError:
            set_owner = u32.SetWindowLongW
        set_owner(self._bar_hwnd, -8, self._hwnd)                      # GWLP_HWNDPARENT
        self._owner = self._hwnd

    def _tick_anchor(self):
        try:
            self._place()
        except Exception as e:
            log("定位失败", e)
        self.root.after(400, self._tick_anchor)

    # -- 拖动 / 菜单 --
    def _hit_gear(self, x, y):
        x1, y1, x2, y2 = self._gear_rect()
        return x1 <= x <= x2 and y1 <= y <= y2

    def _set_hover(self, what):
        if getattr(self, "_hover", None) == what:
            return
        self._hover = what
        if self._last_draw:
            self._draw()

    def _on_motion(self, e):
        # 悬停在齿轮上就反白，让它读起来像个按钮；其它地方不折腾，省重绘
        self._set_hover("gear" if self._hit_gear(e.x, e.y) else None)

    def _drag_start(self, e):
        if self._hit_gear(e.x, e.y):        # 齿轮是按钮，不是拖动把手
            self._gear_armed = True
            return
        self._gear_armed = False
        self._drag = (e.x_root - self.root.winfo_x(),
                      e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if not self._drag:
            return
        x, y = e.x_root - self._drag[0], e.y_root - self._drag[1]
        self.cfg["anchor"] = "free"
        self.cfg["free_x"], self.cfg["free_y"] = x, y
        self.root.geometry("+%d+%d" % (x, y))

    def _drag_end(self, e):
        if getattr(self, "_gear_armed", False):
            self._gear_armed = False
            if self._hit_gear(e.x, e.y):
                self.open_settings()
            return
        if self._drag:
            self._drag = None
            save_settings(self.cfg)

    def _menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="设置…", command=self.open_settings)
        m.add_command(label="重置位置", command=self._reset_pos)
        m.add_separator()
        m.add_command(label="退出", command=self.quit)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _reset_pos(self):
        self.cfg.update({"anchor": DEFAULTS["anchor"], "scope": DEFAULTS["scope"],
                         "offset_x": DEFAULTS["offset_x"],
                         "offset_y": DEFAULTS["offset_y"]})
        save_settings(self.cfg)
        self._place()

    # -- 设置面板 --
    def open_settings(self):
        dlg = getattr(self, "_dlg", None)
        if dlg is not None:            # 已经开着就抬起来，别开一摞
            try:
                dlg.top.deiconify()
                dlg.top.lift()
                dlg.top.focus_force()
                return dlg
            except tk.TclError:
                self._dlg = None
        self._dlg = SettingsDialog(self)
        return self._dlg

    def apply_settings(self):
        self.cfg["poll_ms"] = max(400, int(self.cfg["poll_ms"]))
        self._apply_winprops()
        self._pick_font()
        self._draw()

    # -- IPC：--settings 唤起已运行实例 --
    def _start_ipc(self):
        def srv():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", IPC_PORT))
                s.listen(2)
            except OSError as e:
                log("IPC 端口占用", e)
                return
            while True:
                try:
                    c, _ = s.accept()
                    msg = c.recv(64).decode("utf-8", "replace").strip()
                    c.close()
                    if msg == "settings":
                        self.root.after(0, self.open_settings)
                    elif msg == "quit":
                        self.root.after(0, self.quit)
                except OSError:
                    return

        threading.Thread(target=srv, daemon=True).start()

    def quit(self):
        try:
            self.src.stop()
            LOCK.unlink()
        except OSError:
            pass
        self.root.destroy()

    def run(self):
        try:
            LOCK.write_text(str(os.getpid()))
        except OSError:
            pass
        self.root.mainloop()


# ---------------------------------------------------------------- 设置面板
# 风格：终末地（endfield-ui）
#   唯一强调色 acid yellow #FFFA00，只做填充，只表示"当前 / 可点 / 注意"；
#   切角 = 可点击（按钮、勾选项），展示容器一律直角，radius 全程 0；
#   1px 细线分区，无阴影无发光无渐变；宽体大写拉丁眉标 + 粗体中文；
#   数字用等宽字体。
EF_UI = ("Saira", "Noto Sans SC", "Microsoft YaHei UI", "Microsoft YaHei")
EF_MONO = ("Cascadia Mono", "Consolas")


def _ef_font(fams, size, bold=False):
    have = set(tkfont.families())
    fam = next((f for f in fams if f in have), "Microsoft YaHei UI")
    return (fam, size, "bold") if bold else (fam, size)


class _EfButton(tk.Canvas):
    """切角按钮。终末地里切角代表"可操作"，所以按钮必须有切角；
    主按钮实心 acid yellow + 反白字，次按钮只有 1px 边线。"""

    def __init__(self, master, text, command, primary=False, width=None):
        self.text = text
        self.command = command
        self.primary = primary
        self._hover = False
        f = _ef_font(EF_UI, 9, bold=primary)
        probe = tkfont.Font(root=master, font=f)
        h, cut = 30, 8
        w = width or (probe.measure(text) + 34)
        super().__init__(master, width=w, height=h, bg=EF_PANEL,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._font = f
        self._cw, self._ch, self._cut = w, h, cut
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", lambda e: self.command())
        self._paint()

    def _set_hover(self, v):
        if self._hover != v:
            self._hover = v
            self._paint()

    def _paint(self):
        self.delete("all")
        w, h, cut = self._cw, self._ch, self._cut
        pts = [0.5, 0.5, w - cut, 0.5, w - 0.5, cut + 0.5, w - 0.5, h - 0.5,
               0.5, h - 0.5]
        if self.primary:
            fill = EF_SIGNAL if not self._hover else "#FFF86B"
            self.create_polygon(pts, smooth=False, fill=fill,
                                outline=fill, width=1)
            fg = EF_ON_SIGNAL
        else:
            fill = EF_CHIP if self._hover else EF_PANEL
            self.create_polygon(pts, smooth=False, fill=fill,
                                outline=EF_RULE, width=1)
            fg = EF_INK if self._hover else EF_INK2
        self.create_text(w / 2.0, h / 2.0, text=self.text, fill=fg,
                         font=self._font)


class _EfCheck(tk.Canvas):
    """勾选项。未选中是 1px 空方框，选中是实心 acid yellow 方块 + 墨色 ✓；
    文字跟着变亮——状态靠"填满/未填满"，不靠颜色区分。"""

    def __init__(self, master, text, var, big=False):
        self.var = var
        self.text = text
        self._hover = False
        f = _ef_font(EF_UI, 10 if big else 9, bold=big)
        probe = tkfont.Font(root=master, font=f)
        self._box = 15 if big else 13
        self._h = 24 if big else 21
        w = self._box + 9 + probe.measure(text)
        super().__init__(master, width=w, height=self._h, bg=EF_PANEL,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._font = f
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<Button-1>", lambda e: self._toggle())
        var.trace_add("write", lambda *_: self._paint())
        self._paint()

    def _hov(self, v):
        if self._hover != v:
            self._hover = v
            self._paint()

    def _toggle(self):
        self.var.set(not self.var.get())

    def _paint(self):
        self.delete("all")
        b, hgt = self._box, self._h
        on = bool(self.var.get())
        top = (hgt - b) / 2.0
        if on:
            self.create_rectangle(0.5, top + 0.5, b - 0.5, top + b - 0.5,
                                  fill=EF_SIGNAL, outline=EF_SIGNAL)
            self.create_line(3.5, top + b / 2.0, b * 0.42, top + b - 4,
                             b - 3.5, top + 4, fill=EF_ON_SIGNAL, width=2)
        else:
            self.create_rectangle(0.5, top + 0.5, b - 0.5, top + b - 0.5,
                                  fill=EF_PANEL,
                                  outline=EF_SIGNAL if self._hover else EF_RULE)
        self.create_text(b + 9, hgt / 2.0, text=self.text, anchor="w",
                         fill=EF_INK if on else EF_INK2, font=self._font)


class _EfScale(tk.Frame):
    """1px 轨道 + 实心方块把手。终末地不用圆点滑块，用方块。"""

    def __init__(self, master, var, lo, hi, fmt="%g", suffix="", width=190):
        super().__init__(master, bg=EF_PANEL)
        self.var, self.lo, self.hi = var, lo, hi
        self.fmt, self.suffix = fmt, suffix
        self.cv = tk.Canvas(self, width=width, height=22, bg=EF_PANEL,
                            highlightthickness=0, bd=0, cursor="hand2")
        self.cv.pack(side="left")
        self.lb = tk.Label(self, text="", bg=EF_PANEL, fg=EF_SIGNAL,
                           font=_ef_font(EF_MONO, 9, bold=True), width=6,
                           anchor="e")
        self.lb.pack(side="left", padx=(8, 0))
        self.cv.bind("<Button-1>", self._set_from_event)
        self.cv.bind("<B1-Motion>", self._set_from_event)
        self.cv.bind("<Configure>", lambda e: self._paint())
        var.trace_add("write", lambda *_: self._paint())
        self._paint()

    def _set_from_event(self, e):
        w = max(1, self.cv.winfo_width())
        pad = 6
        r = min(1.0, max(0.0, (e.x - pad) / float(max(1, w - pad * 2))))
        self.var.set(round(self.lo + (self.hi - self.lo) * r, 2))

    def _paint(self):
        self.cv.delete("all")
        w = self.cv.winfo_width() or int(self.cv["width"])
        h, pad = 22, 6
        y = h / 2.0
        try:
            frac = (float(self.var.get()) - self.lo) / float(self.hi - self.lo)
        except (ValueError, ZeroDivisionError):
            frac = 0.0
        frac = min(1.0, max(0.0, frac))
        x = pad + (w - pad * 2) * frac
        self.cv.create_line(pad, y, w - pad, y, fill=EF_RULE, width=1)
        self.cv.create_line(pad, y, x, y, fill=EF_SIGNAL, width=1)
        self.cv.create_rectangle(x - 3.5, y - 6.5, x + 3.5, y + 6.5,
                                 fill=EF_SIGNAL, outline=EF_SIGNAL)
        self.lb.configure(text=self.fmt % float(self.var.get()) + self.suffix)


class _EfChips(tk.Frame):
    """切角 chip 单选组。选中 = 实心 acid yellow（唯一"当前"表示）。"""

    def __init__(self, master, var, pairs, bg=EF_PAPER):
        super().__init__(master, bg=bg)
        self.var = var
        for val, txt in pairs:
            cv = tk.Canvas(self, height=24,
                           width=int(tkfont.Font(root=master,
                                                 font=_ef_font(EF_UI, 8)
                                                 ).measure(txt)) + 24,
                           bg=bg, highlightthickness=0, bd=0, cursor="hand2")
            cv._hover = False
            cv.pack(side="left", padx=(0, 5))

            def paint(c=cv, v=val, t=txt):
                c.delete("all")
                w, h, cut = int(c["width"]), 24, 6
                on = self.var.get() == v
                hov = c._hover
                pts = [0.5, 0.5, w - cut, 0.5, w - 0.5, cut + 0.5,
                       w - 0.5, h - 0.5, 0.5, h - 0.5]
                if on:
                    c.create_polygon(pts, smooth=False, fill=EF_SIGNAL,
                                     outline=EF_SIGNAL)
                    fg = EF_ON_SIGNAL
                else:
                    c.create_polygon(pts, smooth=False,
                                     fill=EF_CHIP if hov else bg,
                                     outline=EF_RULE)
                    fg = EF_INK if hov else EF_INK2
                c.create_text(w / 2.0, h / 2.0, text=t, fill=fg,
                              font=_ef_font(EF_UI, 8))

            cv.bind("<Enter>",
                    lambda e, c=cv, p=paint: (setattr(c, "_hover", True), p()))
            cv.bind("<Leave>",
                    lambda e, c=cv, p=paint: (setattr(c, "_hover", False), p()))
            cv.bind("<Button-1>", lambda e, v=val: self.var.set(v))
            self.var.trace_add("write", lambda *_, p=paint: p())
            paint()


class SettingsDialog:
    def __init__(self, bar: HoverBar):
        self.bar = bar
        self.cfg = bar.cfg
        top = tk.Toplevel(bar.root)
        self.top = top
        top.title("悬浮条设置")
        top.configure(bg=EF_PAPER)
        top.attributes("-topmost", True)
        top.resizable(False, False)
        top.protocol("WM_DELETE_WINDOW", self.close)

        # 三色条：屏幕开始处，只出现一次（终末地签名元素）
        tri = tk.Frame(top, bg=EF_PAPER, height=3)
        tri.pack(fill="x")
        for col in (EF_TRI_A, EF_TRI_B, EF_TRI_C):
            tk.Frame(tri, bg=col, height=3).pack(side="left", fill="both",
                                                 expand=True)

        # 命令栏：品牌 + 面包屑，拉丁字母大写拉开
        cmd = tk.Frame(top, bg=EF_CMD)
        cmd.pack(fill="x")
        mark = tk.Canvas(cmd, width=11, height=11, bg=EF_CMD,
                         highlightthickness=0, bd=0)
        mark.create_polygon([0, 0, 11, 0, 11, 4.4, 4.4, 11, 0, 11],
                            smooth=False, fill=EF_SIGNAL, outline="")
        mark.pack(side="left", padx=(16, 8), pady=12)
        tk.Label(cmd, text="Z C O D E   T O K E N", bg=EF_CMD, fg=EF_INK,
                 font=_ef_font(EF_UI, 9, bold=True)).pack(side="left")
        crumb = tk.Frame(cmd, bg=EF_CMD)
        crumb.pack(side="left", padx=(16, 0))
        tk.Label(crumb, text="B A R", bg=EF_CMD, fg="#8b8b8b",
                 font=_ef_font(EF_UI, 8)).pack(side="left")
        on = tk.Frame(crumb, bg=EF_SIGNAL)
        on.pack(side="left", padx=(10, 0))
        tk.Label(on, text="S E T T I N G S", bg=EF_SIGNAL, fg=EF_ON_SIGNAL,
                 font=_ef_font(EF_UI, 8, bold=True)).pack(padx=8, pady=3)

        # 内容区可滚动：125% 缩放下整套控件约 900 逻辑像素高，1080p 屏幕放不下，
        # 装进 Canvas + 竖向滚动条（细线风格，不用 tk 原生沟槽）。
        outer = tk.Frame(top, bg=EF_PAPER)
        outer.pack(fill="both", expand=True, padx=(20, 0), pady=(14, 0))
        body = tk.Frame(outer, bg=EF_PAPER)
        self._scroll_cv = tk.Canvas(outer, bg=EF_PAPER, highlightthickness=0,
                                    bd=0)
        self._scroll_cv.pack(side="left", fill="both", expand=True)
        self._scrollbar = tk.Canvas(outer, width=3, bg=EF_PAPER,
                                    highlightthickness=0, bd=0)
        self._scrollbar.pack(side="right", fill="y", padx=(6, 8))
        body_id = self._scroll_cv.create_window((0, 0), window=body,
                                                anchor="nw")
        body.bind("<Configure>", lambda e: self._sync_scroll(body_id))
        self._scroll_cv.bind("<Configure>", lambda e: self._sync_scroll(body_id))
        for w in (self._scroll_cv, body):
            w.bind("<MouseWheel>", self._on_wheel)
        top.bind("<MouseWheel>", self._on_wheel)

        tk.Label(body, text="C O N F I G U R A T I O N", bg=EF_PAPER,
                 fg=EF_INK2, font=_ef_font(EF_UI, 8)).pack(anchor="w")
        tk.Label(body, text="悬浮条设置", bg=EF_PAPER, fg=EF_INK,
                 font=_ef_font(EF_UI, 15, bold=True)).pack(anchor="w",
                                                           pady=(2, 0))
        tk.Label(body, text="拖动条面可直接移动；条最右端的方块就是设置入口。",
                 bg=EF_PAPER, fg=EF_INK2,
                 font=_ef_font(EF_UI, 9)).pack(anchor="w", pady=(3, 8))

        sec_no = [0]

        def section(cn, en):
            """分区标题：编号 + 中文 + 拉开字距的拉丁眉标 + 1px 通栏细线。"""
            sec_no[0] += 1
            head = tk.Frame(body, bg=EF_PAPER)
            head.pack(fill="x", pady=(10, 0))
            tk.Label(head, text="%02d" % sec_no[0], bg=EF_PAPER,
                     fg=EF_SIGNAL,
                     font=_ef_font(EF_MONO, 9, bold=True)
                     ).pack(side="left", padx=(0, 10))
            tk.Label(head, text=cn, bg=EF_PAPER, fg=EF_INK,
                     font=_ef_font(EF_UI, 10, bold=True)).pack(side="left")
            tk.Label(head, text=en, bg=EF_PAPER, fg=EF_INK2,
                     font=_ef_font(EF_UI, 8)).pack(side="right")
            tk.Frame(body, bg=EF_RULE, height=1).pack(fill="x", pady=(4, 8))

        def row(cn, note=None):
            """一行：左边说明（中文 + 小字注释），右边控件区。"""
            r = tk.Frame(body, bg=EF_PAPER)
            r.pack(fill="x", pady=3)
            left = tk.Frame(r, bg=EF_PAPER)
            left.pack(side="left", fill="x", expand=True)
            tk.Label(left, text=cn, bg=EF_PAPER, fg=EF_INK,
                     font=_ef_font(EF_UI, 9)).pack(anchor="w")
            if note:
                tk.Label(left, text=note, bg=EF_PAPER, fg=EF_INK2,
                         font=_ef_font(EF_UI, 8)).pack(anchor="w")
            right = tk.Frame(r, bg=EF_PAPER)
            right.pack(side="right")
            return right

        # ---- 01 位置 ----
        section("位置", "P O S I T I O N")
        self.v_anchor = tk.StringVar(value=self.cfg["anchor"])
        _EfChips(row("贴靠", "相对 ZCode 窗口或整块屏幕"), self.v_anchor,
                 [(a, ANCHOR_TXT[a]) for a in ANCHORS if a != "free"]
                 ).pack(anchor="e")

        self.v_scope = tk.StringVar(value=self.cfg["scope"])
        _EfChips(row("范围", "跟随窗口 = 只贴 ZCode；整块屏幕 = 贴显示器"),
                 self.v_scope,
                 [("window", "跟随窗口"), ("screen", "整块屏幕")]
                 ).pack(anchor="e")

        self.v_ox = tk.IntVar(value=int(self.cfg["offset_x"]))
        self.v_oy = tk.IntVar(value=int(self.cfg["offset_y"]))
        r = row("边距", "左右 / 上下，单位 px")
        for i, (v, tip) in enumerate(((self.v_ox, "左右"), (self.v_oy, "上下"))):
            sp = tk.Spinbox(r, from_=0, to=600, textvariable=v, width=5,
                            bg=EF_CHIP, fg=EF_INK, insertbackground=EF_SIGNAL,
                            buttonbackground=EF_CHIP, relief="flat", bd=0,
                            highlightthickness=1,
                            highlightbackground=EF_RULE,
                            highlightcolor=EF_SIGNAL,
                            font=_ef_font(EF_MONO, 9))
            sp.pack(side="left", padx=(0, 8))
            tk.Label(r, text=tip, bg=EF_PAPER, fg=EF_INK2,
                     font=_ef_font(EF_UI, 8)).pack(side="left", padx=(0, 12))

        # ---- 02 外观 ----
        section("外观", "A P P E A R A N C E")
        self.v_op = tk.DoubleVar(value=float(self.cfg["opacity"]))
        _EfScale(row("透明度", "拖动即时预览，最终值点\u201c应用\u201d才落盘"),
                 self.v_op, 0.30, 1.0, fmt="%.2f").pack(anchor="e")

        self.v_fs = tk.IntVar(value=int(self.cfg["font_px"]))
        _EfScale(row("字号", "px；上下文微进度条会一起缩放"),
                 self.v_fs, 10, 22, fmt="%d", suffix="px").pack(anchor="e")

        # ---- 03 显示项 ----
        section("显示项", "E L E M E N T S")
        self.v_show = {}
        grid = tk.Frame(body, bg=EF_PAPER)
        grid.pack(fill="x", pady=(0, 2))
        names = [("speed", "速度"), ("ctx", "上下文"), ("turn", "本轮"),
                 ("session", "会话"), ("today", "今日"), ("tools", "工具")]
        for i, (k, txt) in enumerate(names):
            v = tk.BooleanVar(value=bool(self.cfg["show"].get(k, True)))
            self.v_show[k] = v
            _EfCheck(grid, txt, v).grid(row=i // 3, column=i % 3,
                                        sticky="w", padx=(0, 14), pady=2)

        self.v_lab = tk.BooleanVar(value=bool(self.cfg["labels"]))
        _EfCheck(body, "显示文字标签（速度 / 上下文 / 本轮 …）", self.v_lab,
                 big=True).pack(anchor="w", pady=(6, 2))

        # ---- 04 行为 ----
        section("行为", "B E H A V I O U R")
        self.v_top = tk.BooleanVar(value=bool(self.cfg["always_on_top"]))
        _EfCheck(body, "总是置顶（会盖住其它程序）", self.v_top,
                 big=True).pack(anchor="w", pady=2)
        tk.Label(body, text="默认不勾：靠 Windows 属主窗口绑定，只浮在 ZCode 上，"
                            "随它最小化一起让路。",
                 bg=EF_PAPER, fg=EF_INK2,
                 font=_ef_font(EF_UI, 8)).pack(anchor="w", pady=(0, 6))

        self.v_hide = tk.BooleanVar(value=bool(self.cfg["hide_when_inactive"]))
        _EfCheck(body, "ZCode 不在前台时隐藏", self.v_hide,
                 big=True).pack(anchor="w", pady=(0, 4))

        # ---- 底部按钮 ----
        tk.Frame(top, bg=EF_RULE, height=1).pack(fill="x", pady=(12, 0))
        foot = tk.Frame(top, bg=EF_PANEL)
        foot.pack(fill="x")
        inner = tk.Frame(foot, bg=EF_PANEL)
        inner.pack(fill="x", padx=20, pady=12)
        _EfButton(inner, "恢复默认", self.reset).pack(side="left")
        _EfButton(inner, "应用", self.apply, primary=True).pack(side="right")
        _EfButton(inner, "关闭", self.close).pack(side="right", padx=(0, 8))

        # 透明度/字号拖动即生效：先在条上看效果，再决定要不要"应用"
        for v in (self.v_op, self.v_fs):
            v.trace_add("write", lambda *_: self._preview())

        top.update_idletasks()
        # Canvas 不会把内嵌 body 的尺寸传上去，得自己算：
        # 足够高就全长显示，超过屏幕 86% 就封顶，剩下的靠滚动。
        bh = body.winfo_reqheight()
        bw = body.winfo_reqwidth()
        maxh = max(360, int(top.winfo_screenheight() * 0.86))
        chrome = top.winfo_reqheight() - self._scroll_cv.winfo_reqheight()
        vh = min(bh, max(280, maxh - chrome))
        self._scroll_cv.configure(width=bw, height=vh)
        top.update_idletasks()
        self._sync_scroll(None)
        self._place_near_pointer()

    def _sync_scroll(self, body_id):
        """内容比视口高时，画出可滚动的细线指示条。"""
        cv, sb = self._scroll_cv, self._scrollbar
        if body_id is None:
            items = cv.find_all()
            if not items:
                return
            body_id = items[0]
        h = cv.winfo_height()
        total = cv.bbox("all")
        if not total:
            return
        th = total[3]
        sb.delete("all")
        if th <= h + 1:
            return
        frac = h / float(th)
        top_y = max(0.0, min(1.0, -cv.canvasy(0) / float(max(1, th - h))))
        y1 = top_y * h
        y2 = y1 + max(18.0, h * frac)
        sb.configure(height=h)
        sb.create_rectangle(1, y1, 2, y2, fill=EF_SIGNAL, outline=EF_SIGNAL)

    def _on_wheel(self, e):
        cv = self._scroll_cv
        total = cv.bbox("all")
        if not total or total[3] <= cv.winfo_height() + 1:
            return
        cv.yview_scroll(int(-1 * (e.delta / 120)) * 3, "units")
        self.top.after_idle(lambda: self._sync_scroll(None))

    def _place_near_pointer(self):
        """把面板摆在鼠标附近，但不许出屏（125% 缩放下很容易掉底边）。"""
        top = self.top
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        px, py = top.winfo_pointerx(), top.winfo_pointery()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        x = min(max(8, px - w // 2), max(8, sw - w - 8))
        y = min(max(8, py - 40), max(8, sh - h - 48))
        top.geometry("+%d+%d" % (x, y))

    def _preview(self):
        """只实时预览外观两项，不落盘；其余项点"应用"才生效。"""
        try:
            self.bar.root.attributes(
                "-alpha", min(1.0, max(0.25, float(self.v_op.get()))))
            px = max(9, int(round(float(self.v_fs.get()) * SCALE)))
            self.bar.font.configure(size=-px)
            self.bar._px = px
            self.bar._draw()
        except (tk.TclError, ValueError):
            pass

    def _collect(self):
        c = self.cfg
        c["anchor"] = self.v_anchor.get()
        c["scope"] = self.v_scope.get()
        c["offset_x"] = int(self.v_ox.get())
        c["offset_y"] = int(self.v_oy.get())
        c["opacity"] = round(float(self.v_op.get()), 2)
        c["font_px"] = int(self.v_fs.get())
        c["labels"] = bool(self.v_lab.get())
        c["always_on_top"] = bool(self.v_top.get())
        c["hide_when_inactive"] = bool(self.v_hide.get())
        for k, v in self.v_show.items():
            c["show"][k] = bool(v.get())

    def apply(self):
        self._collect()
        save_settings(self.cfg)
        self.bar.apply_settings()

    def reset(self):
        self.cfg.update(json.loads(json.dumps(DEFAULTS)))
        save_settings(self.cfg)
        self.bar.apply_settings()
        self.close()
        self.bar.open_settings()

    def close(self):
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        if getattr(self.bar, "_dlg", None) is self:
            self.bar._dlg = None


# ---------------------------------------------------------------- CLI
def alive(pid: int) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True,
                             encoding="mbcs", errors="replace").stdout
        return str(pid) in out
    except Exception:
        return False


def running_pid() -> int | None:
    try:
        pid = int(LOCK.read_text().strip())
    except (OSError, ValueError):
        return None
    if alive(pid):
        return pid
    try:
        LOCK.unlink()
    except OSError:
        pass
    return None


def send(msg: str) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=2) as s:
            s.sendall(msg.encode("utf-8"))
        return True
    except OSError:
        return False


def once():
    """排查用：直接跑一次 json。"""
    try:
        p = subprocess.run([sys.executable, str(ZUSAGE), "json"],
                           capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        d = json.loads(p.stdout.decode("utf-8", "replace"))
    except Exception as e:
        print("查询失败：", e)
        return 1
    s, lt, last, today = (d.get("session") or {}, d.get("last_turn") or {},
                          d.get("last") or {}, d.get("today") or {})
    print("会话总量 :", fmt(s.get("total")))
    print("本轮     :", fmt(lt.get("total")))
    print("速度     : %.1f t/s" % (last.get("tps") or 0))
    print("上下文   : %.1f%%" % (s.get("ctx", 0) / max(1, d.get("context_window") or 1) * 100))
    print("今日     :", fmt(today.get("total")))
    print("DPI 缩放 : %.3f" % SCALE)
    return 0


def main(argv):
    global DEBUG
    DEBUG = "--debug" in argv
    if "--once" in argv:
        return once()
    if "--quit" in argv:
        if send("quit"):
            print("已通知退出")
            return 0
        pid = running_pid()
        if pid:
            try:
                os.kill(pid, 9)
                print("已关闭", pid)
                return 0
            except OSError as e:
                print("关闭失败", e)
                return 1
        print("没有正在运行的悬浮条")
        return 0
    if "--settings" in argv:
        if send("settings"):
            return 0
        print("悬浮条未运行，先启动：python hoverbar.py")
        return 1

    if running_pid():
        if not send("settings"):
            print("悬浮条已在运行（右键条面可关）")
        return 0
    if not ZUSAGE.is_file():
        print("找不到 zusage.py：", ZUSAGE)
        return 1
    HoverBar(load_settings()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
