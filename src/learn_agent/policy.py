"""Local path checks. These checks are not an operating-system sandbox."""

from pathlib import Path

BLOCKED = {".git", ".venv", ".learn-agent", ".ssh", ".aws", ".gnupg"}


class Denied(Exception):
    pass


class Workspace:
    def __init__(self, root: Path, *, allow_write: bool = False) -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be a directory.")
        self.allow_write = allow_write

    @staticmethod
    def blocked(name: str) -> bool:
        return name in BLOCKED or name == ".env" or name.startswith(".env.")

    def path(self, relative: str, *, write: bool = False) -> Path:
        if write and not self.allow_write:
            raise Denied("write_not_granted")
        path = Path(relative)
        if not relative or path.is_absolute() or ".." in path.parts:
            raise Denied("invalid_path")
        target = self.root
        for part in path.parts:
            if self.blocked(part):
                raise Denied("protected_path")
            target /= part
            if target.is_symlink():
                raise Denied("symlink_denied")
        if not target.resolve().is_relative_to(self.root):
            raise Denied("outside_workspace")
        return target
