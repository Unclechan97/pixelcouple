"""把网页版打包成一个完全自包含的 HTML 文件。

图片全部转成 data URI，manifest 内联；生成的文件可以直接发给别人，
对方双击用浏览器打开即可，不需要服务器、Python 或网络。

用法：
    python tools/make_singlefile.py
    python tools/make_singlefile.py --out dist/发给她.html
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
ASSETS = WEB / "assets"


def load_manifest() -> tuple[str, dict]:
    text = (ASSETS / "manifest.js").read_text(encoding="utf-8")
    data = json.loads(text[text.index("{") : text.rindex("}") + 1])
    return text.strip(), data


def collect_strips(manifest: dict) -> list[str]:
    names: list[str] = []
    for char, anims in manifest["chars"].items():
        for anim in anims:
            names.append(f"{char}_{anim}_strip.png")
    hug = manifest.get("couples", {}).get("couple", {}).get("hug", 0)
    if hug:
        names.append("couple_hug_strip.png")
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default=str(ROOT / "dist" / "PixelCouple.html"),
        help="输出 HTML 路径",
    )
    args = parser.parse_args()

    html = (WEB / "index.html").read_text(encoding="utf-8")
    manifest_text, manifest = load_manifest()
    names = collect_strips(manifest)

    missing = [name for name in names if not (ASSETS / name).exists()]
    if missing:
        raise SystemExit("缺少素材：" + ", ".join(missing) + "，请先运行 python tools/process.py")

    assets = {
        name: "data:image/png;base64,"
        + base64.b64encode((ASSETS / name).read_bytes()).decode("ascii")
        for name in names
    }
    mapping = json.dumps(assets, ensure_ascii=True, separators=(",", ":"))
    inline = "\n".join(
        (
            "<script>",
            manifest_text,
            "const PIXEL_ASSETS = " + mapping + ";",
            'const assetUrl = (name) => `url("${PIXEL_ASSETS[name]}")`;',
            "</script>",
        )
    )

    old_script = '<script src="assets/manifest.js"></script>'
    old_frame = 'this.frameEl.style.backgroundImage = `url("assets/${this.id}_${name}_strip.png")`;'
    old_hug = "hugSprite.style.backgroundImage = 'url(\"assets/couple_hug_strip.png\")';"
    for old, label in (
        (old_script, "manifest 引用"),
        (old_frame, "角色贴图引用"),
        (old_hug, "拥抱贴图引用"),
    ):
        if html.count(old) != 1:
            raise SystemExit(f"index.html 结构和预期不一致，找不到唯一的{label}")

    html = html.replace(old_script, inline)
    html = html.replace(
        old_frame,
        "this.frameEl.style.backgroundImage = "
        "assetUrl(`${this.id}_${name}_strip.png`);",
    )
    html = html.replace(
        old_hug,
        'hugSprite.style.backgroundImage = assetUrl("couple_hug_strip.png");',
    )
    html = html.replace(
        "<!DOCTYPE html>",
        "<!DOCTYPE html>\n<!-- 单文件版本：由 tools/make_singlefile.py 生成，图片已内嵌。 -->",
        1,
    )

    if "assets/" in html:
        raise SystemExit("输出里仍然有 assets/ 引用，打包失败")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"已生成 {out}")
    print(f"{len(assets)} 张图，文件大小 {out.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
