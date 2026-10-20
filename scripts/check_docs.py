"""离线检查 README 和原生 HTML 教程中的本地链接与引用。
只做结构检查，不生成教程，也不证明内容、交互或真实模型行为已验证。
"""

from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class TutorialParser(HTMLParser):
    """收集 HTML 的锚点、链接、阶段、导航目标和 SVG 引用供统一核对。"""

    def __init__(self) -> None:
        """为一次解析建立独立集合，避免多个文档之间混用锚点。"""
        super().__init__()
        self.ids: list[str] = []
        self.links: list[str] = []
        self.svg_refs: list[str] = []
        self.stage_ids: list[str] = []
        self.chapter_targets: list[str] = []
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """从标签属性记录引用关系，不执行 HTML 中的脚本或访问网络。"""
        values = dict(attrs)
        if identifier := values.get("id"):
            self.ids.append(identifier)
            if tag == "article" and re.fullmatch(r"stage-\d+", identifier):
                self.stage_ids.append(identifier)
        if target := values.get("data-chapter"):
            self.chapter_targets.append(target)
        if tag in ("script", "img", "link"):
            if asset := values.get("src") or values.get("href"):
                self.assets.append(asset)
        if tag == "a" and (href := values.get("href")):
            self.links.append(href)
        if tag == "use" and (href := values.get("href")):
            self.svg_refs.append(href.removeprefix("#"))
        for value in values.values():
            self.svg_refs.extend(re.findall(r"url\(#([^)]+)\)", value or ""))


def check_tutorial(errors: list[str]) -> None:
    """核对唯一教程文件、阶段顺序、导航、资源依赖与 SVG / 文件引用。"""
    path = ROOT / "docs/tutorial.html"
    if not path.exists():
        errors.append("Missing docs/tutorial.html")
        return
    parser = TutorialParser()
    parser.feed(path.read_text(encoding="utf-8"))
    ids = set(parser.ids)
    stages = [f"stage-{index}" for index in range(len(parser.stage_ids))]
    if not stages or parser.stage_ids != stages:
        errors.append("tutorial.html: learning stages must start at 0 and remain in order")
    for target in parser.chapter_targets:
        if target not in ids:
            errors.append(f"tutorial.html: missing sidebar chapter {target}")
    for asset in parser.assets:
        if not asset.startswith(("data:", "#")):
            errors.append(f"tutorial.html: external asset dependency {asset}")
    for extra in (ROOT / "docs").rglob("*.html"):
        if extra != path:
            errors.append(f"Duplicate tutorial HTML: {extra.relative_to(ROOT)}")
    for identifier, count in Counter(parser.ids).items():
        if count > 1:
            errors.append(f"tutorial.html: duplicate id {identifier}")
    for reference in parser.svg_refs:
        if reference not in ids:
            errors.append(f"tutorial.html: missing SVG definition {reference}")
    for link in parser.links:
        parts = urlsplit(link)
        if parts.scheme or parts.netloc:
            continue
        if not parts.path:
            if parts.fragment and parts.fragment not in ids:
                errors.append(f"tutorial.html: missing anchor {link}")
        elif not (path.parent / unquote(parts.path)).exists():
            errors.append(f"tutorial.html: missing file {link}")
    print(f"Checked tutorial HTML: {len(parser.links)} links and all SVG references.")


def main() -> int:
    """检查 Markdown 围栏及本地链接，再检查 HTML；发现问题返回非零退出码。"""
    errors = []
    documents = [
        ROOT / "README.md",
        *sorted((ROOT / "docs").rglob("*.md")),
    ]
    diagram_count = 0
    for path in documents:
        text = path.read_text(encoding="utf-8")
        fences = re.findall(r"^```.*$", text, re.MULTILINE)
        if len(fences) % 2:
            errors.append(f"{path.relative_to(ROOT)}: unbalanced code fences")
        diagram_count += text.count("```mermaid")
        for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            parts = urlsplit(target)
            linked = path.parent / unquote(parts.path)
            if parts.path and not linked.exists():
                errors.append(f"{path.relative_to(ROOT)}: missing link {target}")
            elif linked.suffix == ".html" and parts.fragment:
                parser = TutorialParser()
                parser.feed(linked.read_text(encoding="utf-8"))
                if parts.fragment not in parser.ids:
                    errors.append(f"{path.relative_to(ROOT)}: missing anchor {target}")
    check_tutorial(errors)
    for error in errors:
        print(error)
    print(f"Checked {len(documents)} documents and {diagram_count} Mermaid fences.")
    print("Link, fence and HTML reference checks only; not a content or runtime certification.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
