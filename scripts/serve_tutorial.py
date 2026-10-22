"""独立的教程静态服务，仅使用标准库，不导入或启动 Agent。"""

import argparse
import io
from functools import partial
from html import escape
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from os import PathLike
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def public_file(path: Path) -> bool:
    """发布教程、Python 源码与示例文本；真实配置和会话数据库不对外提供。"""
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT) or resolved == ROOT:
        return False
    relative = resolved.relative_to(ROOT)
    if relative == Path(".env.example"):
        return True
    if any(part.startswith(".") for part in relative.parts):
        return False
    return (
        relative.parts[0] == "docs"
        or relative == Path("README.md")
        or (
            relative.parts[0] in {"learn_agent", "examples", "tests", "scripts"}
            and relative.suffix == ".py"
        )
        or (relative.parent == Path("examples/workspace") and relative.suffix == ".txt")
    )


class TutorialHandler(SimpleHTTPRequestHandler):
    """复用标准静态响应，同时限制发布范围和目录入口。"""

    def send_head(self) -> io.BytesIO | BinaryIO | None:
        if urlsplit(self.path).path in {"/", "/docs", "/docs/"}:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/docs/tutorial.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        path = Path(self.translate_path(self.path)).resolve()
        if not public_file(path) and path != ROOT / "examples/workspace":
            self.send_error(HTTPStatus.NOT_FOUND)
            return None
        return super().send_head()

    def list_directory(self, path: str | PathLike[str]) -> io.BytesIO | None:
        # 教程指向工作区目录时，只展示可发布的示例；其他目录不生成文件列表。
        directory = Path(path)
        if directory != ROOT / "examples/workspace":
            self.send_error(HTTPStatus.NOT_FOUND)
            return None
        links = "".join(
            f'<li><a href="{quote(file.name)}">{escape(file.name)}</a></li>'
            for file in sorted(directory.iterdir())
            if file.is_file() and public_file(file)
        )
        body = (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            "<title>教程示例资料</title><h1>教程示例资料</h1>"
            f"<ul>{links}</ul></html>"
        ).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def guess_type(self, path: str | PathLike[str]) -> str:
        if Path(path).suffix in {".py", ".md", ".txt", ".example"}:
            return "text/plain; charset=utf-8"
        return super().guess_type(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=8080, help="监听端口，默认 8080")
    args = parser.parse_args()
    handler = partial(TutorialHandler, directory=str(ROOT))
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
        print(f"教程服务：http://{args.host}:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n教程服务已停止。")


if __name__ == "__main__":
    main()
