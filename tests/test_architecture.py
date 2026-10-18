import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "learn_agent"


def imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_core_does_not_import_adapters():
    for name in ["contracts.py", "runner.py", "context.py"]:
        assert not any(
            dependency.startswith(("openai", "learn_agent.providers", "learn_agent.store"))
            for dependency in imports(ROOT / name)
        )
    assert not any(name.startswith("learn_agent.") for name in imports(ROOT / "contracts.py"))


def test_entry_points_use_shared_application_service():
    for name in ["cli.py", "web.py"]:
        dependencies = imports(ROOT / name)
        assert "learn_agent.application" in dependencies
        assert "learn_agent.runner" not in dependencies
        assert "learn_agent.providers.openai" not in dependencies
    assert not any(
        name.startswith(("fastapi", "starlette")) for name in imports(ROOT / "application.py")
    )
