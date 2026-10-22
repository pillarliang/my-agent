"""离线检查分页教程的跨页锚点、本地资源和 SVG 引用，不生成文档。"""

from __future__ import annotations

import json
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]


class TutorialParser(HTMLParser):
    """每页独立收集标识和链接；跨页锚点由目标页面的标识集合验证。"""

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.links: list[str] = []
        self.svg_refs: list[str] = []
        self.stage_ids: list[str] = []
        self.chapter_links: list[tuple[str, str]] = []
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if identifier := values.get("id"):
            self.ids.append(identifier)
            if tag == "article" and re.fullmatch(r"stage-\d+", identifier):
                self.stage_ids.append(identifier)
        if tag in ("script", "img", "link"):
            if asset := values.get("src") or values.get("href"):
                self.assets.append(asset)
        if tag == "a" and (href := values.get("href")):
            self.links.append(href)
            if target := values.get("data-chapter"):
                self.chapter_links.append((target, href))
        if tag == "use" and (href := values.get("href")):
            self.svg_refs.append(href.removeprefix("#"))
        for value in values.values():
            self.svg_refs.extend(re.findall(r"url\(#([^)]+)\)", value or ""))


def check_link(
    source: Path, link: str, pages: dict[Path, TutorialParser], errors: list[str]
) -> None:
    """解析相对于引用者的路径，再检查目标 HTML 中的真实锚点。"""
    parts = urlsplit(link)
    if parts.scheme or parts.netloc:
        return
    target = (source.parent / unquote(parts.path)).resolve() if parts.path else source.resolve()
    if not target.exists():
        errors.append(f"{source.name}: missing file {link}")
    elif target.suffix == ".html" and parts.fragment:
        parser = pages.get(target)
        if parser is None:
            parser = TutorialParser()
            parser.feed(target.read_text(encoding="utf-8"))
            pages[target] = parser
        if unquote(parts.fragment) not in parser.ids:
            errors.append(f"{source.name}: missing anchor {link}")


def check_svg(path: Path, errors: list[str]) -> None:
    """复用图示独立保存；每个 SVG 自身必须包含其使用的箭头和标识。"""
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        errors.append(f"{path.name}: invalid SVG: {exc}")
        return
    ids = [node.attrib["id"] for node in root.iter() if "id" in node.attrib]
    for identifier, count in Counter(ids).items():
        if count > 1:
            errors.append(f"{path.name}: duplicate id {identifier}")
    for reference in re.findall(r"url\(#([^)]+)\)", path.read_text(encoding="utf-8")):
        if reference not in ids:
            errors.append(f"{path.name}: missing SVG definition {reference}")


def check_tutorial(errors: list[str]) -> dict[Path, TutorialParser]:
    """总目录、独立章节及共享资源共同组成教程，正文不再要求全部在一页。"""
    if not (ROOT / "docs/tutorial.html").exists():
        errors.append("Missing docs/tutorial.html")
    pages = {}
    for path in sorted((ROOT / "docs").rglob("*.html")):
        parser = TutorialParser()
        parser.feed(path.read_text(encoding="utf-8"))
        pages[path.resolve()] = parser
    stages = [stage for parser in pages.values() for stage in parser.stage_ids]
    if not stages or sorted(stages) != [f"stage-{index}" for index in range(len(stages))]:
        errors.append("tutorial: stages must appear once each, starting at 0 without gaps")
    for path, parser in list(pages.items()):
        for identifier, count in Counter(parser.ids).items():
            if count > 1:
                errors.append(f"{path.name}: duplicate id {identifier}")
        for reference in parser.svg_refs:
            if reference not in parser.ids:
                errors.append(f"{path.name}: missing SVG definition {reference}")
        for chapter, href in parser.chapter_links:
            if urlsplit(href).fragment != chapter:
                errors.append(f"{path.name}: sidebar chapter does not match link {href}")
        for link in parser.links:
            check_link(path, link, pages, errors)
        for asset in parser.assets:
            if asset.startswith(("data:", "#")):
                continue
            parts = urlsplit(asset)
            if parts.scheme or parts.netloc:
                errors.append(f"{path.name}: external asset dependency {asset}")
            else:
                check_link(path, asset, pages, errors)
    diagrams = sorted((ROOT / "docs/assets/diagrams").glob("*.svg"))
    for path in diagrams:
        check_svg(path, errors)
    legacy = ROOT / "docs/assets/legacy-links.js"
    if legacy.exists():
        # 只解析声明中的 JSON，不执行 JavaScript；校验旧锚点重定向也能到达目标。
        match = re.search(r"const legacyChapters=(.*);", legacy.read_text(encoding="utf-8"))
        if match is None:
            errors.append("legacy-links.js: missing redirect map")
        else:
            for target in json.loads(match.group(1)).values():
                check_link(ROOT / "docs/tutorial.html", target, pages, errors)
    print(f"Checked {len(pages)} tutorial pages and {len(diagrams)} SVG files.")
    return pages


def main() -> int:
    errors: list[str] = []
    pages = check_tutorial(errors)
    documents = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for path in documents:
        text = path.read_text(encoding="utf-8")
        if len(re.findall(r"^```.*$", text, re.MULTILINE)) % 2:
            errors.append(f"{path.name}: unbalanced code fences")
        for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
            # Markdown 的本页标题锚点由渲染器生成，此处只核对跨文件链接。
            if not target.startswith("#"):
                check_link(path, target, pages, errors)
    for error in errors:
        print(error)
    print(f"Checked {len(documents)} Markdown documents and all local cross-page links.")
    print("Structural checks only; content and browser behavior need separate review.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
