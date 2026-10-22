"""分页教程的结构契约：跨页锚点、资源和 SVG 引用必须实际存在。"""

import pytest

from scripts import check_docs


@pytest.fixture
def tutorial(tmp_path, monkeypatch):
    monkeypatch.setattr(check_docs, "ROOT", tmp_path)
    docs = tmp_path / "docs"
    (docs / "stages").mkdir(parents=True)
    (docs / "assets/diagrams").mkdir(parents=True)
    (docs / "assets/style.css").write_text("body { color: black; }")
    (docs / "tutorial.html").write_text(
        '<link rel="stylesheet" href="assets/style.css">'
        '<a data-chapter="stage-0" href="stages/stage-0.html#stage-0">阶段 0</a>'
    )
    (docs / "stages/stage-0.html").write_text('<article id="stage-0">正文</article>')
    return docs


def test_cross_page_chapter_and_local_shared_assets_are_allowed(tutorial):
    errors = []
    pages = check_docs.check_tutorial(errors)
    assert len(pages) == 2
    assert errors == []


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ('<a href="stages/stage-0.html#missing">链接</a>', "missing anchor"),
        ('<img src="assets/missing.svg">', "missing file"),
        ('<script src="https://example.com/app.js"></script>', "external asset"),
        ('<div id="same"></div><div id="same"></div>', "duplicate id"),
    ],
)
def test_broken_cross_page_contract_is_reported(tutorial, markup, expected):
    with (tutorial / "tutorial.html").open("a") as file:
        file.write(markup)
    errors = []
    check_docs.check_tutorial(errors)
    assert any(expected in error for error in errors)


def test_standalone_svg_must_include_its_arrow_definition(tutorial):
    (tutorial / "assets/diagrams/broken.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<path d="M0 0 H10" marker-end="url(#missing)"/></svg>'
    )
    errors = []
    check_docs.check_tutorial(errors)
    assert any("missing SVG definition" in error for error in errors)
