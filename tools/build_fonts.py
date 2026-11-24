"""生成网页用的字体子集。

思源宋体（Noto Serif SC）的可变字体有 25 MB，不能整个发给手机。这里把它切成：
- 一个“界面字”文件：页面模板和代码里出现过的字，加上 ASCII，所有页面都会用到；
- 若干个“常用字”分片：GB2312 里其余的汉字和符号，每片约 200 字。

浏览器按 @font-face 的 unicode-range 只下载页面上实际出现的字所在的分片（比如同学的名字）。
Fira Code 只保留 ASCII，用来显示学号和邀请码。

用法（字体源文件取自 github.com/google/fonts 的 ofl/notoserifsc 与 ofl/firacode）：

    uv run --group fonts python tools/build_fonts.py NotoSerifSC[wght].ttf FiraCode[wght].ttf
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "mus_booking"
OUT = PACKAGE / "web" / "static" / "fonts"
CSS = PACKAGE / "web" / "static" / "fonts.css"
CHUNK = 200
WEIGHTS = (400, 700)


def ui_characters() -> set[int]:
    chars = set(range(0x20, 0x7F))
    for path in PACKAGE.rglob("*"):
        if path.suffix in {".html", ".py", ".js", ".css"} and "fonts" not in path.parts:
            chars |= {ord(c) for c in path.read_text(encoding="utf-8")}
    chars |= {ord(c) for c in "“”‘’…—–·、，。：；！？（）《》「」【】"}
    return {c for c in chars if c >= 0x20}


def gb2312_characters() -> set[int]:
    found = set()
    for code in range(0x80, 0x10000):
        char = chr(code)
        try:
            char.encode("gb2312")
        except UnicodeEncodeError:
            continue
        found.add(code)
    return found


def ranges(codes: list[int]) -> str:
    parts = []
    start = previous = codes[0]
    for code in [*codes[1:], None]:
        if code is not None and code == previous + 1:
            previous = code
            continue
        parts.append(f"U+{start:X}" if start == previous else f"U+{start:X}-{previous:X}")
        if code is not None:
            start = previous = code
    return ", ".join(parts)


def limited(source: Path, low: int, high: int) -> bytes:
    font = TTFont(source)
    instancer.instantiateVariableFont(font, {"wght": (low, high)}, inplace=True)
    buffer = io.BytesIO()
    font.save(buffer)
    return buffer.getvalue()


def write_subset(data: bytes, codes: set[int], target: Path) -> int:
    font = TTFont(io.BytesIO(data))
    options = subset.Options()
    options.flavor = "woff2"
    options.layout_features = ["*"]
    options.hinting = False
    options.desubroutinize = True
    options.name_IDs = ["*"]
    options.notdef_outline = True
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=sorted(codes))
    subsetter.subset(font)
    font.flavor = "woff2"
    font.save(target)
    return target.stat().st_size


def main(noto: str, fira: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.woff2"):
        old.unlink()

    serif = limited(Path(noto), *WEIGHTS)
    cmap = set(TTFont(io.BytesIO(serif)).getBestCmap())
    ui = sorted(ui_characters() & cmap)
    rest = sorted((gb2312_characters() & cmap) - set(ui))
    faces = []

    size = write_subset(serif, set(ui), OUT / "serif-ui.woff2")
    faces.append(("Noto Serif SC", "serif-ui.woff2", ui, WEIGHTS))
    print(f"serif-ui.woff2  {len(ui)} 字  {size / 1024:.0f} KB")
    total = size
    for index in range(0, len(rest), CHUNK):
        chunk = rest[index : index + CHUNK]
        name = f"serif-{index // CHUNK + 1:02d}.woff2"
        size = write_subset(serif, set(chunk), OUT / name)
        total += size
        faces.append(("Noto Serif SC", name, chunk, WEIGHTS))
    print(f"常用字分片 {len(rest) // CHUNK + 1} 个，合计 {total / 1024 / 1024:.1f} MB")

    mono = limited(Path(fira), 400, 600)
    ascii_codes = list(range(0x20, 0x7F))
    size = write_subset(mono, set(ascii_codes), OUT / "mono.woff2")
    faces.append(("Fira Code", "mono.woff2", ascii_codes, (400, 600)))
    print(f"mono.woff2  {size / 1024:.0f} KB")

    lines = ["/* 由 tools/build_fonts.py 生成，不要手改。字体授权见 fonts/ 下的 OFL 文件。 */"]
    for family, name, codes, (low, high) in faces:
        lines.append(
            "@font-face {"
            f' font-family: "{family}"; src: url("fonts/{name}") format("woff2");'
            f" font-weight: {low} {high}; font-style: normal; font-display: swap;"
            f" unicode-range: {ranges(codes)}; }}"
        )
    CSS.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"写入 {CSS.relative_to(ROOT)}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
