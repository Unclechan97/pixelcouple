"""把 raw/ 里的 AI 像素素材切成网页可用的动画条带。

素材约定（当前这对图就是按这个格式出的）：
  raw/{char}_idle_sheet.jpg    横向 4 格待机
  raw/{char}_actions.jpg       3 行 × 4 列：行走 / 跳跃 / 攻击

处理步骤：
  抠绿幕（含绿色地面阴影）→ 去掉行间浅色接缝 → 遮住左上角动作标签
  → 按格子切帧 → 按脚底基线和身体锚点对齐 → 统一缩放到目标身高
  → 每帧放到 96×96 画布 → 共用调色板限色 → 输出条带 + manifest

用法：
  python tools/process.py                # 生成网页素材
  python tools/process.py --colors 0     # 不限制颜色，看原色
  python tools/process.py --idle bob     # 待机改为 2 帧轻微呼吸（默认用原图 4 帧）
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
OUT = ROOT / "web" / "assets"
DEBUG = ROOT / "debug_build"
EXTS = {".png", ".jpg", ".jpeg", ".webp"}

FRAME = 96          # 单帧画布（素材像素）
BASELINE = 94       # 脚底在画布中的 y（页面用 manifest 里的值定位）
PAD = 12            # 裁切角色时的留白（原始像素）
ZOOM = 2            # 网页默认放大倍数
LO, HI = 12, 42     # 绿色度 -> 透明度 的软阈值
TARGET_HEIGHTS = {"her": 64.0, "me": 68.0}
DEFAULT_HEIGHT = 60.0
HUG_TARGET_HEIGHT = 76.0   # 双人拥抱整体高度：比单人略大，像镜头拉近
ANIM_ROWS = ("walk", "jump", "attack")
IDLE_COLS = 4


# ----------------------------------------------------------------------------
# 基础图像处理
# ----------------------------------------------------------------------------
def find_sheets() -> dict[str, dict[str, Path]]:
    found: dict[str, dict[str, Path]] = {}
    for path in sorted(RAW.iterdir()) if RAW.is_dir() else []:
        if path.suffix.lower() not in EXTS:
            continue
        stem = path.stem.lower()
        if stem.endswith("_idle_sheet") and stem.count("_") >= 1:
            char = stem[: -len("_idle_sheet")]
            found.setdefault(char, {})["idle"] = path
        elif stem.endswith("_actions"):
            char = stem[: -len("_actions")]
            found.setdefault(char, {})["actions"] = path

    usable = {}
    for char, paths in found.items():
        missing = [k for k in ("idle", "actions") if k not in paths]
        if missing:
            print(f"[跳过] {char}: 缺少 {char}_{missing[0]}_sheet" if "idle" in missing
                  else f"[跳过] {char}: 缺少 {char}_actions")
            continue
        usable[char] = paths
    if not usable:
        sys.exit("raw/ 里没有找到成对的 {角色}_idle_sheet 和 {角色}_actions 图片")
    return usable


def find_hug_sheets() -> dict[str, Path]:
    """可选：找 {名字}_hug_sheet.* 或 {名字}_hug.*，2×2 双人拥抱表。"""
    found: dict[str, Path] = {}
    for path in sorted(RAW.iterdir()) if RAW.is_dir() else []:
        if path.suffix.lower() not in EXTS:
            continue
        stem = path.stem.lower()
        if stem.endswith("_hug_sheet"):
            found[stem[: -len("_hug_sheet")]] = path
        elif stem.endswith("_hug"):
            found.setdefault(stem[: -len("_hug")], path)
    return found


def load_keyed(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (原始RGB, 去绿边后的RGB, alpha[0..1])。"""
    rgb = np.asarray(Image.open(path).convert("RGB")).astype(np.float32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    green = g - np.maximum(r, b)

    alpha = np.clip((HI - green) / (HI - LO), 0.0, 1.0)
    alpha = alpha * alpha * (3.0 - 2.0 * alpha)

    # 去掉绿幕反光：绿通道不许超过 R/B 中较大的那个
    out = rgb.copy()
    out[..., 1] = np.minimum(g, np.maximum(r, b))
    return rgb.astype(np.uint8), out.astype(np.uint8), alpha.astype(np.float32)


def longest_true_run(row: np.ndarray) -> int:
    idx = np.flatnonzero(row)
    if idx.size == 0:
        return 0
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[0, breaks + 1]
    ends = np.r_[breaks, idx.size - 1]
    return int((idx[ends] - idx[starts] + 1).max())


def remove_pale_seams(raw_rgb: np.ndarray, alpha: np.ndarray) -> list[int]:
    """去掉 AI 精灵表里贯穿整行的浅色接缝线（不是白裙子）。"""
    h, w, _ = raw_rgb.shape
    pale = np.minimum(raw_rgb[..., 0], raw_rgb[..., 2]) > 150
    threshold = max(120, int(w * 0.30))
    runs = np.array([longest_true_run(pale[y]) for y in range(h)])
    seam = runs > threshold
    removed: list[int] = []
    for y in np.flatnonzero(seam):
        for yy in (y - 1, y, y + 1):
            if 0 <= yy < h and (yy == y or runs[yy] > max(60, int(w * 0.08))):
                alpha[yy, :] = 0.0
                removed.append(int(yy))
    return sorted(set(removed))


def connected_components(mask: np.ndarray, min_area: int = 200) -> list[tuple[int, tuple[int, int, int, int]]]:
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    comps: list[tuple[int, tuple[int, int, int, int]]] = []
    for y in range(h):
        for x in range(w):
            if not mask[y, x] or seen[y, x]:
                continue
            queue = deque([(y, x)])
            seen[y, x] = True
            area = 0
            minx = miny = 10 ** 9
            maxx = maxy = -1
            while queue:
                cy, cx = queue.popleft()
                area += 1
                minx, maxx = min(minx, cx), max(maxx, cx)
                miny, maxy = min(miny, cy), max(maxy, cy)
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            queue.append((ny, nx))
            if area >= min_area:
                comps.append((area, (minx, miny, maxx, maxy)))
    return comps


def find_label_rects(raw_rgb: np.ndarray, nrows: int) -> list[tuple[int, int, int, int]]:
    """找到每行左上角的深色标签（行走/跳跃/攻击）。只遮标签，不碰角色。"""
    h, w, _ = raw_rgb.shape
    band = h // nrows
    rects: list[tuple[int, int, int, int]] = []
    for row in range(nrows):
        y0 = row * band
        region = raw_rgb[y0:y0 + min(200, band), 0:min(340, w)]
        dark = region.max(axis=2) < 160
        candidates = [
            c for c in connected_components(dark, 200)
            if c[1][2] - c[1][0] >= 80
            and 20 <= c[1][3] - c[1][1] <= 150
            and c[1][1] < 130
        ]
        if not candidates:
            print(f"[提示] 第 {row + 1} 行没找到动作标签，跳过遮罩")
            continue
        _, (x0, yy0, x1, yy1) = min(candidates, key=lambda c: c[1][1])
        rects.append((max(0, x0 - 2), max(0, y0 + yy0 - 2),
                      min(w - 1, x1 + 2), min(h - 1, y0 + yy1 + 2)))
    return rects


def solid_bottom(mask: np.ndarray) -> int:
    ys, _ = np.where(mask)   # 二维 mask：要行号，不是展平后的下标
    if ys.size == 0:
        return 0
    counts = np.bincount(ys, minlength=mask.shape[0])
    rows = np.flatnonzero(counts >= 3)
    return int(rows.max()) if rows.size else int(ys.max())


def anchor_metrics(mask: np.ndarray, bbox: tuple[int, int, int, int]) -> dict[str, float]:
    """所有值都用格子内坐标。head 用于走/跳，feet8 用于攻击。"""
    x0, y0, x1, y1 = bbox
    height = max(1, y1 - y0 + 1)

    def median_in(cut: int) -> float:
        rows = np.arange(mask.shape[0])[:, None]
        yy, xx = np.where(mask & (rows >= cut))
        if xx.size == 0:
            return (x0 + x1) / 2.0
        return float(np.median(xx))

    return {
        "head": median_in(y0 + int(height * 0.20)),
        "feet8": median_in(y1 - int(height * 0.08)),
    }


# ----------------------------------------------------------------------------
# 读表 + 分析每帧
# ----------------------------------------------------------------------------
def analyze_sheet(path: Path, kind: str) -> dict:
    raw, rgb, alpha = load_keyed(path)
    h, w, _ = raw.shape
    seams = remove_pale_seams(raw, alpha)

    if kind == "actions":
        nrows, ncols = 3, 4
        labels = find_label_rects(raw, nrows)
        for x0, y0, x1, y1 in labels:
            alpha[y0:y1 + 1, x0:x1 + 1] = 0.0
    else:
        nrows, ncols = 1, IDLE_COLS
        labels = []

    x_edges = [round(i * w / ncols) for i in range(ncols + 1)]
    y_edges = [round(i * h / nrows) for i in range(nrows + 1)]

    rows = []
    for r in range(nrows):
        frames = []
        for c in range(ncols):
            x0, x1 = x_edges[c], x_edges[c + 1]
            y0, y1 = y_edges[r], y_edges[r + 1]

            if kind == "actions":
                # 格子边缘常残留接缝/相邻格子的特效，往里收一点点
                alpha[y0:y0 + 2, x0:x1] = 0.0
                alpha[y1 - 2:y1, x0:x1] = 0.0
                alpha[y0:y1, x0:x0 + 4] = 0.0
                alpha[y0:y1, x1 - 4:x1] = 0.0

            mask = alpha[y0:y1, x0:x1] >= 0.5
            ys, xs = np.where(mask)
            if xs.size == 0:
                raise ValueError(f"{path.name} 第 {r + 1} 行第 {c + 1} 格是空的")
            bbox_local = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
            bbox = (bbox_local[0] + x0, bbox_local[1] + y0,
                    bbox_local[2] + x0, bbox_local[3] + y0)
            anchors_local = anchor_metrics(mask, bbox_local)
            anchors = {k: v + x0 for k, v in anchors_local.items()}
            frames.append({
                "col": c,
                "bbox": bbox,
                "solid": solid_bottom(mask) + y0,
                "anchors": anchors,
            })
        baseline = max(f["solid"] for f in frames)
        rows.append({"row": r, "frames": frames, "baseline": baseline})

    if kind == "actions":
        walk_heights = [f["bbox"][3] - f["bbox"][1] + 1 for f in rows[0]["frames"]]
        ref_height = float(np.median(walk_heights))
    else:
        idle_heights = [f["bbox"][3] - f["bbox"][1] + 1 for f in rows[0]["frames"]]
        ref_height = float(np.median(idle_heights))

    print(f"  {path.name}: {w}×{h}, 参考身高 {ref_height:.1f}px"
          + (f", 去掉接缝 {len(seams)} 行" if seams else "")
          + (f", 标签 {len(labels)} 个" if labels else ""))
    return {
        "path": path, "kind": kind, "raw": raw, "rgb": rgb, "alpha": alpha,
        "rows": rows, "ref_height": ref_height, "labels": labels,
    }


def analyze_hug_sheet(path: Path) -> dict:
    """2×2 双人拥抱表：整组一起抠、一起缩，四帧共用缩放比例。"""
    raw, rgb, alpha = load_keyed(path)
    seams = remove_pale_seams(raw, alpha)
    h, w, _ = raw.shape
    nrows = ncols = 2
    x_edges = [round(i * w / ncols) for i in range(ncols + 1)]
    y_edges = [round(i * h / nrows) for i in range(nrows + 1)]

    frames = []
    for r in range(nrows):
        for c in range(ncols):
            x0, x1 = x_edges[c], x_edges[c + 1]
            y0, y1 = y_edges[r], y_edges[r + 1]
            mask = alpha[y0:y1, x0:x1] >= 0.5
            ys, xs = np.where(mask)
            if xs.size == 0:
                raise ValueError(f"{path.name} 第 {r + 1} 行第 {c + 1} 格是空的")
            bbox = (int(xs.min()) + x0, int(ys.min()) + y0,
                    int(xs.max()) + x0, int(ys.max()) + y0)
            frames.append({
                "bbox": bbox,
                "solid": solid_bottom(mask) + y0,
            })

    heights = [f["bbox"][3] - f["bbox"][1] + 1 for f in frames]
    ref_height = float(np.median(heights))
    bases = [f["solid"] for f in frames]
    print(f"  {path.name}: {w}×{h}, 双人高 {heights}, 基线 {bases}"
          + (f", 去掉接缝 {len(seams)} 行" if seams else ""))
    return {
        "path": path, "kind": "hug", "raw": raw, "rgb": rgb, "alpha": alpha,
        "frames": frames, "ref_height": ref_height,
    }


def premultiplied_resize(crop: Image.Image, new_w: int, new_h: int) -> tuple[np.ndarray, np.ndarray]:
    arr = np.asarray(crop).astype(np.float32)
    rgb = arr[..., :3]
    a = arr[..., 3:4] / 255.0
    pre = rgb * a

    def resize_plane(plane: np.ndarray) -> np.ndarray:
        img = Image.fromarray(plane.astype(np.float32), mode="F")
        return np.asarray(img.resize((new_w, new_h), Image.Resampling.BOX)).astype(np.float32)

    pre2 = np.stack([resize_plane(pre[..., i]) for i in range(3)], axis=-1)
    a2 = resize_plane(a[..., 0])[..., None]
    rgb2 = np.divide(pre2, np.maximum(a2, 1e-6))
    return np.clip(rgb2, 0, 255), np.clip(a2, 0, 1)


def render_frames(sheet: dict, target_height: float) -> dict[str, list[Image.Image]]:
    s = target_height / sheet["ref_height"]
    anim_names = ANIM_ROWS if sheet["kind"] == "actions" else ("idle",)
    anim_anchor = {"idle": "head", "walk": "head", "jump": "head", "attack": "feet8"}
    h, w, _ = sheet["raw"].shape
    rgb, alpha = sheet["rgb"], sheet["alpha"]
    result: dict[str, list[Image.Image]] = {}

    for row, anim in zip(sheet["rows"], anim_names):
        for frame in row["frames"]:
            bx0, by0, bx1, by1 = frame["bbox"]
            cx0, cy0 = max(0, bx0 - PAD), max(0, by0 - PAD)
            cx1, cy1 = min(w, bx1 + PAD + 1), min(h, by1 + PAD + 1)
            crop = np.dstack([
                rgb[cy0:cy1, cx0:cx1],
                (alpha[cy0:cy1, cx0:cx1] * 255).astype(np.uint8),
            ])
            crop_img = Image.fromarray(crop, "RGBA")
            nw = max(1, round(crop_img.width * s))
            nh = max(1, round(crop_img.height * s))
            rgb2, a2 = premultiplied_resize(crop_img, nw, nh)
            a2 = np.where(a2 >= 0.5, 1.0, 0.0)
            rgba = np.dstack([rgb2, a2 * 255.0]).astype(np.uint8)
            # 透明区域颜色清零，避免缩放/调色板把背景色带进来
            rgba[rgba[..., 3] == 0, :3] = 0
            fg = Image.fromarray(rgba, "RGBA")

            anchor_x = frame["anchors"][anim_anchor[anim]]
            dx = round(FRAME / 2 - (anchor_x - cx0) * s)
            dy = round(BASELINE - (row["baseline"] - cy0) * s)
            canvas = Image.new("RGBA", (FRAME, FRAME), (0, 0, 0, 0))
            sx0, sy0 = max(0, -dx), max(0, -dy)
            sx1, sy1 = min(nw, FRAME - dx), min(nh, FRAME - dy)
            if sx1 > sx0 and sy1 > sy0:
                canvas.alpha_composite(fg.crop((sx0, sy0, sx1, sy1)), (dx + sx0, dy + sy0))

            # 只警告角色本体被裁（留白被裁无所谓）
            left = dx + (bx0 - cx0) * s
            right = dx + (bx1 + 1 - cx0) * s
            top = dy + (by0 - cy0) * s
            bottom = dy + (by1 + 1 - cy0) * s
            if left < -1 or right > FRAME + 1 or top < -1 or bottom > FRAME + 1:
                print(f"  [注意] {anim} 第 {frame['col'] + 1} 帧贴近画布边缘: "
                      f"x={left:.1f}..{right:.1f}, y={top:.1f}..{bottom:.1f}")
            result.setdefault(anim, []).append(canvas)
    return result


def render_hug_frames(sheet: dict, target_height: float) -> list[Image.Image]:
    """双人帧按“整组包围盒中心 + 每帧脚底”对齐，保证拥抱时两人不会被拉歪。"""
    s = target_height / sheet["ref_height"]
    h, w, _ = sheet["raw"].shape
    rgb, alpha = sheet["rgb"], sheet["alpha"]
    out: list[Image.Image] = []

    for frame in sheet["frames"]:
        bx0, by0, bx1, by1 = frame["bbox"]
        cx0, cy0 = max(0, bx0 - PAD), max(0, by0 - PAD)
        cx1, cy1 = min(w, bx1 + PAD + 1), min(h, by1 + PAD + 1)
        crop = np.dstack([
            rgb[cy0:cy1, cx0:cx1],
            (alpha[cy0:cy1, cx0:cx1] * 255).astype(np.uint8),
        ])
        crop_img = Image.fromarray(crop, "RGBA")
        nw = max(1, round(crop_img.width * s))
        nh = max(1, round(crop_img.height * s))
        rgb2, a2 = premultiplied_resize(crop_img, nw, nh)
        a2 = np.where(a2 >= 0.5, 1.0, 0.0)
        rgba = np.dstack([rgb2, a2 * 255.0]).astype(np.uint8)
        rgba[rgba[..., 3] == 0, :3] = 0
        fg = Image.fromarray(rgba, "RGBA")

        center_x = (bx0 + bx1 + 1) / 2
        dx = round(FRAME / 2 - (center_x - cx0) * s)
        dy = round(BASELINE - (frame["solid"] - cy0) * s)
        canvas = Image.new("RGBA", (FRAME, FRAME), (0, 0, 0, 0))
        sx0, sy0 = max(0, -dx), max(0, -dy)
        sx1, sy1 = min(nw, FRAME - dx), min(nh, FRAME - dy)
        if sx1 > sx0 and sy1 > sy0:
            canvas.alpha_composite(fg.crop((sx0, sy0, sx1, sy1)), (dx + sx0, dy + sy0))

        left = dx + (bx0 - cx0) * s
        right = dx + (bx1 + 1 - cx0) * s
        top = dy + (by0 - cy0) * s
        bottom = dy + (by1 + 1 - cy0) * s
        if left < -1 or right > FRAME + 1 or top < -1 or bottom > FRAME + 1:
            print(f"  [注意] 拥抱第 {len(out) + 1} 帧贴近画布边缘: "
                  f"x={left:.1f}..{right:.1f}, y={top:.1f}..{bottom:.1f}")
        out.append(canvas)
    return out


# ----------------------------------------------------------------------------
# 调色板
# ----------------------------------------------------------------------------
def apply_shared_palette(frames: dict[str, list[Image.Image]], colors: int) -> dict[str, list[Image.Image]]:
    if colors <= 0:
        return frames
    samples = []
    for imgs in frames.values():
        for img in imgs:
            arr = np.asarray(img)
            opaque = arr[arr[..., 3] >= 128][:, :3]
            if opaque.size:
                samples.append(opaque)
    if not samples:
        return frames
    sample = np.concatenate(samples, axis=0)
    if sample.shape[0] > 40000:
        rng = np.random.default_rng(0)
        sample = sample[rng.choice(sample.shape[0], 40000, replace=False)]
    cols = max(2, min(256, colors))
    strip_w = min(200, sample.shape[0])
    strip_h = max(1, math.ceil(sample.shape[0] / strip_w))
    pad = np.zeros((strip_w * strip_h - sample.shape[0], 3), dtype=np.uint8)
    canvas = np.vstack([sample.astype(np.uint8), pad]).reshape(strip_h, strip_w, 3)
    palette_img = Image.fromarray(canvas, "RGB").quantize(colors=cols, method=Image.Quantize.MEDIANCUT)

    out: dict[str, list[Image.Image]] = {}
    for anim, imgs in frames.items():
        converted = []
        for img in imgs:
            arr = np.asarray(img)
            q = Image.fromarray(arr[..., :3], "RGB").quantize(
                palette=palette_img, dither=Image.Dither.NONE
            ).convert("RGB")
            qa = np.dstack([np.asarray(q), arr[..., 3]])
            converted.append(Image.fromarray(qa, "RGBA"))
        out[anim] = converted
    return out


def idle_variation_stats(frames: list[Image.Image]) -> None:
    """把四张待机帧对齐后的差异打出来，帮助判断是否值得逐帧播放。"""
    base = np.asarray(frames[0]).astype(np.int16)
    for i, img in enumerate(frames[1:], start=1):
        arr = np.asarray(img).astype(np.int16)
        a1 = base[..., 3] > 127
        a2 = arr[..., 3] > 127
        both = a1 & a2
        if not both.any():
            continue
        diff = np.abs(base[..., :3] - arr[..., :3]).max(axis=2)[both]
        iou = (a1 & a2).sum() / max(1, (a1 | a2).sum())
        changed = float((diff > 32).mean() * 100)
        print(f"  待机帧 1 vs {i + 1}: 平均差 {diff.mean():.1f}, "
              f"明显变化像素 {changed:.1f}%, 轮廓 IoU {iou:.3f}")


# ----------------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------------
def save_assets(char: str, frames: dict[str, list[Image.Image]]) -> dict[str, int]:
    OUT.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for anim, imgs in frames.items():
        strip = Image.new("RGBA", (FRAME * len(imgs), FRAME), (0, 0, 0, 0))
        for i, img in enumerate(imgs):
            img.save(OUT / f"{char}_{anim}_{i + 1}.png")
            strip.alpha_composite(img, (i * FRAME, 0))
        strip.save(OUT / f"{char}_{anim}_strip.png")
        counts[anim] = len(imgs)
    return counts


def save_preview(char: str, frames: dict[str, list[Image.Image]]) -> None:
    DEBUG.mkdir(exist_ok=True)
    anims = ("idle", "walk", "jump", "attack")
    scale = 2
    cell = FRAME * scale
    label_w = 74
    pad = 18
    sheet = Image.new("RGBA", (label_w + 4 * cell + pad, pad * 2 + 4 * cell), (30, 27, 42, 255))
    draw = ImageDraw.Draw(sheet)
    for r, anim in enumerate(anims):
        yy = pad + r * cell
        draw.text((8, yy + cell // 2 - 4), anim, fill=(232, 222, 204, 255))
        draw.line([(label_w, yy + BASELINE * scale),
                   (label_w + 4 * cell - 1, yy + BASELINE * scale)],
                  fill=(91, 122, 94, 255))
        for c in range(4):
            if c >= len(frames.get(anim, [])):
                continue
            big = frames[anim][c].resize((cell, cell), Image.Resampling.NEAREST)
            sheet.alpha_composite(big, (label_w + c * cell, yy))
    path = DEBUG / f"preview_{char}.png"
    sheet.convert("RGB").save(path)
    print(f"  预览图: {path}")


def save_hug_preview(name: str, frames: list[Image.Image]) -> None:
    DEBUG.mkdir(exist_ok=True)
    scale = 2
    cell = FRAME * scale
    label_w = 74
    pad = 18
    sheet = Image.new("RGBA", (label_w + len(frames) * cell + pad, pad * 2 + cell),
                      (30, 27, 42, 255))
    draw = ImageDraw.Draw(sheet)
    draw.text((8, pad + cell // 2 - 4), "hug", fill=(232, 222, 204, 255))
    draw.line([(label_w, pad + BASELINE * scale),
               (label_w + len(frames) * cell - 1, pad + BASELINE * scale)],
              fill=(91, 122, 94, 255))
    for i, img in enumerate(frames):
        big = img.resize((cell, cell), Image.Resampling.NEAREST)
        sheet.alpha_composite(big, (label_w + i * cell, pad))
    path = DEBUG / f"preview_{name}_hug.png"
    sheet.convert("RGB").save(path)
    print(f"  预览图: {path}")


def write_manifest(all_counts: dict[str, dict[str, int]],
                   all_couples: dict[str, dict[str, int]] | None = None) -> None:
    heights = {char: TARGET_HEIGHTS.get(char, DEFAULT_HEIGHT) for char in all_counts}
    manifest = {
        "meta": {"frame": FRAME, "baseline": BASELINE, "zoom": ZOOM,
                 "heights": heights},
        "chars": all_counts,
    }
    if all_couples:
        manifest["couples"] = all_couples
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "manifest.js").write_text(
        "// 由 tools/process.py 自动生成，请勿手改\n"
        "window.SPRITE_MANIFEST = "
        + json.dumps(manifest, ensure_ascii=False, indent=2)
        + ";\n",
        encoding="utf-8",
    )


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="AI 精灵表 -> 网页动画素材")
    parser.add_argument("--colors", type=int, default=32,
                        help="共享调色板颜色数，0 表示不量化（默认 32）")
    parser.add_argument("--idle", choices=("all", "bob"), default="all",
                        help="待机用原图 4 帧（all）还是 1 帧 + 1px 呼吸（bob）")
    parser.add_argument("--no-preview", action="store_true", help="不生成调试预览图")
    args = parser.parse_args()

    sheets = find_sheets()
    all_counts: dict[str, dict[str, int]] = {}
    for char, paths in sheets.items():
        print(f"\n[{char}]")
        target_height = TARGET_HEIGHTS.get(char, DEFAULT_HEIGHT)
        idle_sheet = analyze_sheet(paths["idle"], "idle")
        action_sheet = analyze_sheet(paths["actions"], "actions")

        idle_frames = render_frames(idle_sheet, target_height)["idle"]
        action_frames = render_frames(action_sheet, target_height)

        if args.idle == "bob":
            base = idle_frames[0]
            bob = Image.new("RGBA", (FRAME, FRAME), (0, 0, 0, 0))
            bob.alpha_composite(base, (0, -1))
            idle_frames = [base, bob]
        else:
            idle_variation_stats(idle_frames)

        frames: dict[str, list[Image.Image]] = {"idle": idle_frames}
        frames.update(action_frames)
        frames = apply_shared_palette(frames, args.colors)

        counts = save_assets(char, frames)
        all_counts[char] = counts
        print(f"  → idle {counts['idle']} / walk {counts['walk']} / "
              f"jump {counts['jump']} / attack {counts['attack']} 帧")
        if not args.no_preview:
            save_preview(char, frames)

    couples: dict[str, dict[str, int]] = {}
    for name, path in find_hug_sheets().items():
        print(f"\n[{name} 拥抱]")
        hug_sheet = analyze_hug_sheet(path)
        hug_frames = {"hug": render_hug_frames(hug_sheet, HUG_TARGET_HEIGHT)}
        hug_frames = apply_shared_palette(hug_frames, args.colors)
        counts = save_assets(name, hug_frames)
        couples[name] = counts
        print(f"  → hug {counts['hug']} 帧")
        if not args.no_preview:
            save_hug_preview(name, hug_frames["hug"])

    write_manifest(all_counts, couples)
    print(f"\n素材输出: {OUT}")
    print("manifest:", json.dumps({"chars": all_counts, "couples": couples},
                                  ensure_ascii=False))
    print("预览: 双击 web/index.html")


if __name__ == "__main__":
    main()
