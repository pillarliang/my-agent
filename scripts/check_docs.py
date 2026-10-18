"""Check local Markdown links and code fence balance without network access."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    errors = []
    documents = [
        ROOT / "README.md",
        ROOT / "START-HERE.md",
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
            target = target.split("#", 1)[0]
            if target and not (path.parent / target).exists():
                errors.append(f"{path.relative_to(ROOT)}: missing link {target}")
    for error in errors:
        print(error)
    print(f"Checked {len(documents)} documents and {diagram_count} Mermaid fences.")
    print("Link/fence checks only. This is not an STE or Mermaid syntax certification.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
