"""PixelCouple 桌面悬浮小人（Windows）。

一个透明、无边框、置顶的桌面宠物：
- 一小块透明“舞台”，里面走动、待机、跳跃、偶尔自动拥抱
- 按住任意小人拖动，可以把整块舞台放到桌面任意位置
- 左键小人跳一下，双击拥抱，右键打开菜单
- 默认在全屏应用（视频/游戏）前台时自动隐藏
- Ctrl+Alt+Q 强制退出

用法：
    python tools/desktop_pet.py
    start_pet.bat
"""
from __future__ import annotations

import argparse
import ctypes
import json
import random
import sys
import time
import traceback
from ctypes import wintypes
from pathlib import Path

import tkinter as tk
from PIL import Image, ImageTk

if sys.platform != "win32":
    raise SystemExit("这个桌面宠物只支持 Windows（依赖分层透明窗口）。")

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "web" / "assets"
DEBUG = ROOT / "debug_build"

KEY_COLOR = "#010203"          # 精灵里没有这个颜色，用它做透明色键
TICK_MS = 33
AUTO_ATTACK = False            # 暂时关闭桌宠的自动攻击动作
SPI_GETWORKAREA = 0x0030
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
ERROR_ALREADY_EXISTS = 183

HEART_PATTERN = (
    ".oo.oo.",
    "ohhffoo",
    "ohffffo",
    "offfffo",
    ".offfo.",
    "..ofo..",
    "...o...",
)
HEART_COLORS = {
    "o": (196, 74, 118),
    "f": (255, 159, 190),
    "h": (255, 220, 232),
}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.SetWindowLongW.restype = wintypes.LONG
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE


# ----------------------------------------------------------------------------
# Windows 小工具
# ----------------------------------------------------------------------------
def set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def get_work_area() -> RECT:
    rc = RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rc), 0)
    return rc


def choose_zoom(work_h: int, override: int | None) -> int:
    if override:
        return override
    if work_h >= 1800:
        return 4
    if work_h >= 1200:
        return 3
    return 2


def acquire_single_instance() -> wintypes.HANDLE | None:
    """避免双击两次出现两只桌宠。"""
    handle = kernel32.CreateMutexW(None, False, "Local\\PixelCoupleDesktopPet")
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        user32.MessageBoxW(None, "像素桌宠已经在运行啦。", "PixelCouple", 0x40)
        return None
    return handle


def root_hwnd(root: tk.Tk) -> int:
    return user32.GetAncestor(root.winfo_id(), GA_ROOT) or root.winfo_id()


def foreground_is_fullscreen(own_hwnd: int) -> bool:
    """判断前台窗口是否铺满了它所在的显示器（视频/游戏全屏）。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd == own_hwnd:
        return False
    cls = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, cls, len(cls))
    if cls.value in {"Progman", "WorkerW", "Shell_TrayWnd", "MultitaskingViewFrame"}:
        return False
    mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not mon:
        return False
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return False
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    m, tol = info.rcMonitor, 2
    return (
        rect.left <= m.left + tol
        and rect.top <= m.top + tol
        and rect.right >= m.right - tol
        and rect.bottom >= m.bottom - tol
    )


def hide_taskbar_icon(hwnd: int) -> None:
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW)


# ----------------------------------------------------------------------------
# 素材
# ----------------------------------------------------------------------------
def load_manifest() -> dict:
    path = ASSETS / "manifest.js"
    if not path.exists():
        raise SystemExit("找不到 web/assets/manifest.js，请先运行 python tools/process.py")
    text = path.read_text(encoding="utf-8")
    data = json.loads(text[text.index("{") : text.rindex("}") + 1])
    if not data.get("chars"):
        raise SystemExit("manifest 里没有角色素材，请重新运行 python tools/process.py")
    return data


def make_heart(cell: int) -> ImageTk.PhotoImage:
    h, w = len(HEART_PATTERN), len(HEART_PATTERN[0])
    im = Image.new("RGBA", (w * cell, h * cell), (0, 0, 0, 0))
    px = im.load()
    for y, line in enumerate(HEART_PATTERN):
        for x, ch in enumerate(line):
            color = HEART_COLORS.get(ch)
            if not color:
                continue
            for dy in range(cell):
                for dx in range(cell):
                    px[x * cell + dx, y * cell + dy] = (*color, 255)
    return ImageTk.PhotoImage(im)


class SpriteBank:
    def __init__(self, manifest: dict, zoom: int, hug_zoom: int):
        frame = int(manifest["meta"]["frame"])
        self.frame = frame
        self.zoom = zoom
        self.hug_zoom = hug_zoom
        self.chars: dict[str, dict[str, dict[str, list[ImageTk.PhotoImage]]]] = {}

        for char, anims in manifest["chars"].items():
            self.chars[char] = {}
            for anim, count in anims.items():
                right: list[ImageTk.PhotoImage] = []
                left: list[ImageTk.PhotoImage] = []
                for i in range(1, int(count) + 1):
                    path = ASSETS / f"{char}_{anim}_{i}.png"
                    if not path.exists():
                        raise SystemExit(f"缺少素材 {path.name}，请重新运行 python tools/process.py")
                    im = Image.open(path).convert("RGBA")
                    im = im.resize((frame * zoom, frame * zoom), Image.Resampling.NEAREST)
                    right.append(ImageTk.PhotoImage(im))
                    left.append(ImageTk.PhotoImage(im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)))
                self.chars[char][anim] = {"right": right, "left": left}

        self.hug: list[ImageTk.PhotoImage] = []
        hug_count = int(manifest.get("couples", {}).get("couple", {}).get("hug", 0))
        for i in range(1, hug_count + 1):
            path = ASSETS / f"couple_hug_{i}.png"
            if not path.exists():
                raise SystemExit(f"缺少素材 {path.name}，请重新运行 python tools/process.py")
            im = Image.open(path).convert("RGBA")
            im = im.resize((frame * hug_zoom, frame * hug_zoom), Image.Resampling.NEAREST)
            self.hug.append(ImageTk.PhotoImage(im))

        cell = max(2, zoom)
        self.hearts = [make_heart(cell), make_heart(cell + 1)]


# ----------------------------------------------------------------------------
# 角色
# ----------------------------------------------------------------------------
class Walker:
    def __init__(self, pet: "PetApp", char: str, x: int, facing: int):
        self.pet = pet
        self.char = char
        self.canvas = pet.canvas
        self.x = x
        self.facing = facing
        self.img_w = pet.frame * pet.zoom
        self.speed = random.uniform(22, 32) * (pet.zoom / 2)
        self.mode = "idle"
        self.anim = "idle"
        self.frames = pet.frame_count(char, "idle")
        self.frame_index = 0
        self.elapsed = 0.0
        self.mode_left = random.uniform(1.5, 3.5)
        self.one_shot: str | None = None
        self.suspended = False
        self.current = None

        sw = 44 * pet.zoom
        sh = 7 * pet.zoom
        sy = pet.ground_y + 2
        self.shadow = self.canvas.create_oval(
            0, 0, sw, sh, fill="#0a0713", outline="", stipple="gray25",
        )
        self.item = self.canvas.create_image(
            0, 0, anchor="nw", tags=(f"pet_{char}", f"pet_char"),
        )
        self.canvas.tag_bind(self.item, "<Enter>", self.on_hover_enter)
        self.canvas.tag_bind(self.item, "<Leave>", self.on_hover_leave)
        pet.walker_by_item[self.item] = self
        self.set_base("idle")
        self.render()

    # ---- 位置 / 外观 ----
    def center_x(self) -> float:
        return self.x + self.img_w / 2

    def head_y(self) -> float:
        return self.pet.ground_y - self.pet.baseline * self.pet.zoom + 12 * self.pet.zoom

    def render(self) -> None:
        top = self.pet.ground_y - self.pet.baseline * self.pet.zoom
        self.canvas.coords(self.item, self.x, top)
        sx, sy = self.center_x(), self.pet.ground_y + 2
        hw = 22 * self.pet.zoom
        hh = 3.5 * self.pet.zoom
        self.canvas.coords(self.shadow, sx - hw, sy - hh, sx + hw, sy + hh)

    def apply_frame(self) -> None:
        bank = self.pet.bank.chars[self.char][self.anim]
        side = "left" if self.facing < 0 else "right"
        images = bank[side]
        self.current = images[self.frame_index % len(images)]
        self.canvas.itemconfigure(self.item, image=self.current)

    def set_anim(self, name: str) -> None:
        self.anim = name
        self.frames = self.pet.frame_count(self.char, name)
        self.elapsed = 0.0
        self.frame_index = 0
        self.apply_frame()

    def set_base(self, mode: str) -> None:
        self.mode = mode
        self.one_shot = None
        self.set_anim(mode)
        if mode == "walk":
            self.mode_left = random.uniform(1.8, 3.8)
            if random.random() < 0.25:
                self.facing *= -1
        else:
            self.mode_left = random.uniform(1.6, 4.2)

    # ---- 动作 ----
    def jump(self, with_heart: bool = True) -> None:
        if self.suspended or self.one_shot or self.pet.hug_running or self.pet.dragging:
            return
        self.one_shot = "jump"
        self.set_anim("jump")
        if with_heart:
            self.pet.spawn_hearts(self.center_x(), self.head_y(), 1)

    def attack(self, toward_partner: bool = True) -> None:
        if self.suspended or self.one_shot or self.pet.hug_running or self.pet.dragging:
            return
        if toward_partner:
            others = [w for w in self.pet.walkers if w is not self and not w.suspended]
            if others:
                other = others[0]
                self.facing = 1 if other.center_x() >= self.center_x() else -1
        self.one_shot = "attack"
        self.set_anim("attack")

    # ---- 拖拽 ----
    def on_hover_enter(self, _event: tk.Event) -> None:
        if not self.pet.dragging:
            self.canvas.configure(cursor="fleur")

    def on_hover_leave(self, _event: tk.Event) -> None:
        if not self.pet.dragging:
            self.canvas.configure(cursor="")

    def stop_for_drag(self) -> None:
        self.one_shot = None
        self.set_base("idle")

    def resume_after_drag(self) -> None:
        self.set_base("idle")
        # 松手后先站一会儿，方便把位置摆好
        self.mode_left = random.uniform(2.4, 3.8)

    # ---- 每帧 ----
    def tick(self, dt: float, now: float) -> None:
        if self.suspended or self.pet.dragging:
            return
        if self.pet.paused and not self.one_shot:
            return

        self.elapsed += dt

        if self.one_shot:
            fps = 9.0 if self.one_shot == "jump" else 8.0
            idx = int(self.elapsed * fps)
            if idx >= self.frames:
                self.one_shot = None
                self.set_base("idle")
            else:
                self.frame_index = idx
                self.apply_frame()
            return

        self.mode_left -= dt
        if self.mode_left <= 0:
            self.set_base("walk" if self.mode == "idle" else "idle")

        if self.mode == "walk":
            self.x += self.facing * self.speed * dt
            min_x, max_x = 8, max(8, self.pet.stage_w - self.img_w - 8)
            if self.x <= min_x:
                self.x = min_x
                self.facing = 1
            elif self.x >= max_x:
                self.x = max_x
                self.facing = -1
            self.frame_index = int(self.elapsed * 7.0) % self.frames
            self.apply_frame()
        else:
            self.frame_index = int(self.elapsed * 2.4) % self.frames
            self.apply_frame()
        self.render()

    def suspend(self) -> None:
        self.suspended = True
        self.one_shot = None
        self.canvas.itemconfigure(self.item, state="hidden")
        self.canvas.itemconfigure(self.shadow, state="hidden")

    def resume(self) -> None:
        self.suspended = False
        self.canvas.itemconfigure(self.item, state="normal")
        self.canvas.itemconfigure(self.shadow, state="normal")
        self.set_base("idle")
        self.render()


# ----------------------------------------------------------------------------
# 主程序
# ----------------------------------------------------------------------------
class PetApp:
    def __init__(
        self,
        root: tk.Tk,
        manifest: dict,
        work: RECT,
        zoom: int,
        hide_fullscreen: bool,
    ):
        self.root = root
        self.manifest = manifest
        self.work = work
        self.work_w = work.right - work.left
        self.work_h = work.bottom - work.top
        self.zoom = zoom
        self.hug_zoom = zoom + 1
        self.frame = int(manifest["meta"]["frame"])
        self.baseline = int(manifest["meta"]["baseline"])

        # 一小块透明舞台：够两个人活动，但只占屏幕一小部分；整块可拖动
        self.stage_w = min(self.work_w, max(430, int(2.3 * self.frame * zoom)))
        self.stage_h = min(self.work_h, self.frame * self.hug_zoom + 56)
        self.ground_y = self.stage_h - 9
        self.start_x = self.work.left + max(0, (self.work_w - self.stage_w) // 2)
        self.start_y = self.work.bottom - self.stage_h
        self.alive = True
        self.paused = False
        self.hidden = False
        self.hug_running = False
        self.dragging = False
        self.drag_origin: tuple[int, int] | None = None
        self.win_origin = (self.start_x, self.start_y)
        self.drag_walker: Walker | None = None
        self.last_release = 0.0
        self.double_press = False
        self.pending_jump: str | None = None
        self.pending_walker: Walker | None = None
        self.walker_by_item: dict[int, Walker] = {}
        self.hearts: list[dict] = []
        self.last = time.perf_counter()
        self.next_fs_check = self.last + 1.0
        self.next_hug = self.last + random.uniform(15, 24)
        self.next_emote = self.last + random.uniform(8, 14)
        self.next_heart = self.last + random.uniform(6, 12)

        self.bank = SpriteBank(manifest, zoom, self.hug_zoom)

        # 透明置顶窗口：颜色键是 #010203，画布上这个颜色会变成“洞”
        root.overrideredirect(True)
        root.configure(bg=KEY_COLOR)
        root.attributes("-topmost", True)
        root.geometry(
            f"{self.stage_w}x{self.stage_h}"
            f"+{self.start_x}+{self.start_y}"
        )
        root.update_idletasks()
        try:
            root.attributes("-transparentcolor", KEY_COLOR)
        except tk.TclError as exc:
            raise SystemExit("当前 Tk 不支持透明色键，无法创建桌宠窗口。") from exc

        self.canvas = tk.Canvas(
            root, width=self.stage_w, height=self.stage_h,
            bg=KEY_COLOR, highlightthickness=0, bd=0,
        )
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.hwnd = root_hwnd(root)
        hide_taskbar_icon(self.hwnd)

        self.paused_var = tk.BooleanVar(value=False)
        self.hide_fs_var = tk.BooleanVar(value=hide_fullscreen)
        self.menu = tk.Menu(root, tearoff=0, font=("Microsoft YaHei UI", 10))
        self.menu.add_command(label="按住小人拖到任意位置", state="disabled")
        self.menu.add_separator()
        self.menu.add_command(label="♥  抱一抱", command=self.start_hug)
        self.menu.add_separator()
        self.menu.add_checkbutton(
            label="暂停自动活动", variable=self.paused_var, command=self.apply_pause,
        )
        self.menu.add_checkbutton(
            label="全屏应用时隐藏", variable=self.hide_fs_var,
        )
        self.menu.add_separator()
        self.menu.add_command(label="退出（Ctrl+Alt+Q）", command=self.quit)

        ids = [c for c in ("me", "her") if c in manifest["chars"]]
        if not ids:
            ids = list(manifest["chars"])[:2]
        self.walkers: list[Walker] = []
        starts = (0.36, 0.64)
        for i, char in enumerate(ids):
            frac = starts[i] if i < len(starts) else 0.3 + i * 0.15
            x = int(self.stage_w * frac - self.frame * zoom / 2)
            facing = 1 if i == 0 else -1
            self.walkers.append(Walker(self, char, x, facing))

        # 拥抱特写（在舞台内居中）
        hug_w = 32 * self.hug_zoom
        hug_h = 3 * self.hug_zoom
        hx, hy = self.stage_w // 2, self.ground_y + 2
        self.hug_shadow = self.canvas.create_oval(
            hx - hug_w, hy - hug_h, hx + hug_w, hy + hug_h,
            fill="#080614", outline="", stipple="gray25", state="hidden",
        )
        self.hug_top = self.ground_y - self.baseline * self.hug_zoom
        self.hug_item = self.canvas.create_image(
            self.stage_w // 2, self.hug_top, anchor="n",
            image=self.bank.hug[0], state="hidden", tags=("pet_hug",),
        )
        self.canvas.tag_bind("pet_hug", "<Button-1>", self.on_hug_click)
        self.canvas.tag_bind("pet_hug", "<Button-3>", self.open_menu)
        for w in self.walkers:
            self.canvas.tag_bind(f"pet_{w.char}", "<Button-3>", self.open_menu)

    # ---- 素材辅助 ----
    def frame_count(self, char: str, anim: str) -> int:
        return len(self.bank.chars[char][anim]["right"])

    def hug_heart_y(self) -> float:
        # 从双人头顶上方冒心，别糊在脸上
        return self.ground_y - 80 * self.hug_zoom

    # ---- 拖动整块舞台 ----
    def clamp_window(self, x: int, y: int) -> tuple[int, int]:
        max_x = max(self.work.left, self.work.right - self.stage_w)
        max_y = max(self.work.top, self.work.bottom - self.stage_h)
        return (
            min(max(int(x), self.work.left), max_x),
            min(max(int(y), self.work.top), max_y),
        )

    def cancel_pending_jump(self) -> None:
        if self.pending_jump is None:
            return
        try:
            self.root.after_cancel(self.pending_jump)
        except tk.TclError:
            pass
        self.pending_jump = None
        self.pending_walker = None

    def fire_pending_jump(self) -> None:
        self.pending_jump = None
        walker = self.pending_walker
        self.pending_walker = None
        if walker is not None and not self.hug_running and not self.dragging:
            walker.jump(True)

    def on_press(self, event: tk.Event) -> None:
        item = self.canvas.find_withtag("current")
        walker = self.walker_by_item.get(item[0]) if item else None
        if walker is None or self.hug_running:
            return
        # 先别急着跳：等一小会儿，确认不是双击（双击要抱抱）
        self.cancel_pending_jump()
        self.double_press = time.monotonic() - self.last_release <= 0.38
        self.drag_walker = walker
        self.drag_origin = (int(event.x_root), int(event.y_root))
        self.win_origin = (self.root.winfo_x(), self.root.winfo_y())

    def on_drag(self, event: tk.Event) -> None:
        if self.drag_origin is None:
            return
        dx = int(event.x_root) - self.drag_origin[0]
        dy = int(event.y_root) - self.drag_origin[1]
        if not self.dragging and max(abs(dx), abs(dy)) > 5:
            self.dragging = True
            self.double_press = False
            self.canvas.configure(cursor="fleur")
            for w in self.walkers:
                w.stop_for_drag()
        if not self.dragging:
            return
        x, y = self.clamp_window(
            self.win_origin[0] + dx, self.win_origin[1] + dy,
        )
        self.root.geometry(f"+{x}+{y}")

    def on_release(self, _event: tk.Event) -> None:
        walker = self.drag_walker
        was_drag = self.dragging
        self.drag_origin = None
        self.drag_walker = None
        self.dragging = False
        if walker is None:
            return
        if was_drag:
            self.double_press = False
            self.last_release = 0.0
            for w in self.walkers:
                w.resume_after_drag()
            self.canvas.configure(cursor="fleur")
            return

        self.canvas.configure(cursor="fleur")
        if self.double_press:
            self.double_press = False
            self.last_release = 0.0
            self.start_hug()
        else:
            self.last_release = time.monotonic()
            # 单击时稍等一下再跳；第二次按下会取消这次跳跃
            self.pending_walker = walker
            self.pending_jump = self.root.after(300, self.fire_pending_jump)

    # ---- 心 ----
    def spawn_hearts(
        self, cx: float, cy: float, count: int = 1, spread: float | None = None,
    ) -> None:
        if spread is None:
            spread = 13 * self.zoom
        for _ in range(count):
            img = random.choice(self.bank.hearts)
            x = cx + random.uniform(-spread, spread)
            y = cy + random.uniform(-14, 6)
            item = self.canvas.create_image(x, y, image=img, anchor="center")
            self.hearts.append({
                "item": item,
                "x": x,
                "y": y,
                "vy": random.uniform(24, 40),
                "drift": random.uniform(-8, 8),
                "life": 0.0,
                "ttl": random.uniform(1.3, 2.0),
            })

    def update_hearts(self, dt: float) -> None:
        for heart in list(self.hearts):
            heart["life"] += dt
            heart["y"] -= heart["vy"] * dt
            heart["x"] += heart["drift"] * dt
            if heart["life"] >= heart["ttl"]:
                self.canvas.delete(heart["item"])
                self.hearts.remove(heart)
            else:
                self.canvas.coords(heart["item"], heart["x"], heart["y"])

    # ---- 拥抱 ----
    def start_hug(self) -> None:
        if (
            not self.alive or self.hug_running or not self.bank.hug
            or self.dragging or self.drag_origin is not None
        ):
            return
        self.hug_running = True
        self.next_hug = time.perf_counter() + random.uniform(48, 80)
        for w in self.walkers:
            w.suspend()
        self.canvas.itemconfigure(self.hug_shadow, state="normal")
        self.canvas.itemconfigure(self.hug_item, state="normal")
        self.canvas.tag_raise(self.hug_item)
        self.canvas.itemconfigure(self.hug_item, image=self.bank.hug[0])

        sequence = ((0, 520), (1, 650), (2, 1900), (3, 850))
        hx, hy = self.stage_w // 2, self.hug_heart_y()
        spread = 22 * self.hug_zoom

        def step(i: int) -> None:
            if not self.alive or not self.hug_running:
                return
            if i >= len(sequence):
                self.finish_hug()
                return
            frame, delay = sequence[i]
            self.canvas.itemconfigure(self.hug_item, image=self.bank.hug[frame])
            if frame == 1:
                self.spawn_hearts(hx, hy, 3, spread=spread)
            elif frame == 2:
                self.spawn_hearts(hx, hy, 6, spread=spread)
                self.root.after(
                    750,
                    lambda: self.hug_running and self.spawn_hearts(
                        hx, hy, 4, spread=spread,
                    ),
                )
            self.root.after(delay, lambda: step(i + 1))

        step(0)

    def finish_hug(self) -> None:
        self.hug_running = False
        self.canvas.itemconfigure(self.hug_item, state="hidden")
        self.canvas.itemconfigure(self.hug_shadow, state="hidden")
        for w in self.walkers:
            w.resume()
            self.spawn_hearts(w.center_x(), w.head_y(), 1)
        self.next_hug = time.perf_counter() + random.uniform(48, 80)

    def on_hug_click(self, _event: tk.Event) -> None:
        if self.hug_running:
            self.spawn_hearts(self.stage_w // 2, self.hug_heart_y(), 3)

    # ---- 菜单 ----
    def open_menu(self, event: tk.Event) -> str:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
        return "break"

    def apply_pause(self) -> None:
        self.paused = bool(self.paused_var.get())
        if not self.paused:
            now = time.perf_counter()
            self.next_hug = now + random.uniform(20, 35)
            self.next_emote = now + random.uniform(7, 14)
            self.next_heart = now + random.uniform(6, 12)

    # ---- 自动行为 ----
    def auto_emote(self) -> None:
        idle = [
            w for w in self.walkers
            if not w.suspended and not w.one_shot and w.mode == "idle"
        ]
        if not idle:
            return
        w = random.choice(idle)
        if AUTO_ATTACK and random.random() < 0.4:
            w.attack(True)
        else:
            w.jump(False)

    def ambient_heart(self) -> None:
        active = [w for w in self.walkers if not w.suspended]
        if not active:
            return
        w = random.choice(active)
        self.spawn_hearts(w.center_x(), w.head_y(), 1)

    def check_fullscreen(self) -> None:
        if not self.hide_fs_var.get():
            if self.hidden:
                self.show_window()
            return
        full = foreground_is_fullscreen(self.hwnd)
        if full and not self.hidden:
            self.hidden = True
            self.root.withdraw()
        elif not full and self.hidden:
            self.show_window()

    def show_window(self) -> None:
        if not self.alive:
            return
        self.hidden = False
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", KEY_COLOR)
        except tk.TclError:
            pass
        self.last = time.perf_counter()
        self.next_hug = self.last + random.uniform(20, 35)
        self.next_emote = self.last + random.uniform(7, 14)
        self.next_heart = self.last + random.uniform(6, 12)

    # ---- 主循环 ----
    def tick(self) -> None:
        if not self.alive:
            return
        now = time.perf_counter()
        dt = min(0.08, now - self.last)
        self.last = now

        if now >= self.next_fs_check:
            self.next_fs_check = now + 1.0
            self.check_fullscreen()

        if not self.hidden:
            for w in self.walkers:
                w.tick(dt, now)
            self.update_hearts(dt)
            if not self.paused and not self.hug_running and self.drag_origin is None:
                if now >= self.next_emote:
                    self.next_emote = now + random.uniform(11, 20)
                    self.auto_emote()
                if now >= self.next_heart:
                    self.next_heart = now + random.uniform(9, 16)
                    self.ambient_heart()
                if now >= self.next_hug:
                    self.start_hug()

        self.root.after(TICK_MS, self.tick)

    def poll_hotkey(self) -> None:
        if not self.alive:
            return
        down = user32.GetAsyncKeyState
        if (
            down(0x11) & 0x8000        # Ctrl
            and down(0x12) & 0x8000    # Alt
            and down(0x51) & 0x8000    # Q
        ):
            self.quit()
            return
        self.root.after(120, self.poll_hotkey)

    def quit(self) -> None:
        if not self.alive:
            return
        self.alive = False
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ----------------------------------------------------------------------------
def log_error(exc_type, exc, tb) -> None:
    try:
        DEBUG.mkdir(parents=True, exist_ok=True)
        with open(DEBUG / "desktop_pet.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n")
            f.write("".join(traceback.format_exception(exc_type, exc, tb)))
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="PixelCouple 桌面悬浮小人")
    parser.add_argument("--scale", type=int, choices=(2, 3, 4), help="覆盖自动缩放")
    parser.add_argument("--no-fullscreen-hide", action="store_true", help="全屏时不隐藏")
    parser.add_argument("--demo-hug", action="store_true", help="启动 1.2 秒后自动抱一次（调试）")
    args = parser.parse_args()

    set_dpi_awareness()
    mutex = acquire_single_instance()
    if not mutex:
        return
    manifest = load_manifest()
    work = get_work_area()
    zoom = choose_zoom(work.bottom - work.top, args.scale)

    root = tk.Tk()
    root.title("PixelCouple Desktop Pet")
    app = PetApp(
        root, manifest, work, zoom,
        hide_fullscreen=not args.no_fullscreen_hide,
    )
    root.report_callback_exception = log_error
    root.after(TICK_MS, app.tick)
    root.after(150, app.poll_hotkey)
    if args.demo_hug:
        root.after(1200, app.start_hug)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        msg = str(exc)
        if msg:
            log_error(SystemExit, exc, exc.__traceback__)
            user32.MessageBoxW(None, msg, "PixelCouple 桌宠", 0x10)
        raise
    except Exception:
        log_error(*sys.exc_info())
        user32.MessageBoxW(
            None,
            "桌宠启动失败。详情见 debug_build/desktop_pet.log",
            "PixelCouple 桌宠",
            0x10,
        )
        raise
