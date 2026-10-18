"""The only route from model proposals to local file effects."""

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from learn_agent.contracts import ToolCall, ToolResult, ToolSpec
from learn_agent.policy import Denied, Workspace


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListArgs(Arguments):
    path: str = Field(description="Relative directory path. Use . for the root.")


class ReadArgs(Arguments):
    path: str
    offset: int = Field(ge=0, description="Character offset, starting at zero.")
    limit: int = Field(ge=1, le=8000, description="Maximum characters to return.")


class CreateArgs(Arguments):
    path: str
    content: str = Field(max_length=16_000)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate JSON key")
        obj[key] = value
    return obj


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


class ToolExecutor:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._tools: dict[str, tuple[type[Arguments], str, Callable[[Any], dict[str, Any]]]] = {
            "list_files": (
                ListArgs,
                "List up to 200 visible entries in one workspace directory.",
                self._list,
            ),
            "read_file": (
                ReadArgs,
                "Read a UTF-8 regular file up to 256 KiB. Returns a character page and hash.",
                self._read,
            ),
            "create_file": (
                CreateArgs,
                "Create a new UTF-8 file. No overwrite or parent creation. Write grant required.",
                self._create,
            ),
        }

    @property
    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name, desc, args.model_json_schema())
            for name, (args, desc, _) in self._tools.items()
        ]

    def execute(self, call: ToolCall) -> ToolResult:
        if call.name not in self._tools:
            return ToolResult(ok=False, code="unknown_tool")
        schema, _, handler = self._tools[call.name]
        try:
            if len(call.arguments.encode("utf-8")) > 100_000:
                return ToolResult(ok=False, code="arguments_too_large")
            raw = json.loads(
                call.arguments, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
            args = schema.model_validate(raw)
        except (ValueError, ValidationError, RecursionError):
            return ToolResult(ok=False, code="invalid_arguments")
        try:
            return ToolResult(ok=True, code="ok", data=handler(args))
        except Denied as exc:
            return ToolResult(ok=False, code=str(exc))
        except FileExistsError:
            return ToolResult(ok=False, code="already_exists")
        except FileNotFoundError:
            return ToolResult(ok=False, code="not_found")
        except UnicodeError:
            return ToolResult(ok=False, code="not_utf8")
        except OSError:
            return ToolResult(ok=False, code="io_error")

    def _list(self, args: ListArgs) -> dict[str, Any]:
        directory = self.workspace.path(args.path)
        entries: list[str] = []
        # Bound enumeration as well as the response. No recursive traversal.
        with os.scandir(directory) as iterator:
            for count, entry in enumerate(iterator):
                if count >= 1000:
                    return {"entries": sorted(entries)[:200], "truncated": True}
                if not self.workspace.blocked(entry.name) and not entry.is_symlink():
                    entries.append(entry.name + ("/" if entry.is_dir() else ""))
        return {"entries": sorted(entries)[:200], "truncated": len(entries) > 200}

    def _read(self, args: ReadArgs) -> dict[str, Any]:
        path = self.workspace.path(args.path)
        if not path.is_file():
            if not path.exists():
                raise FileNotFoundError
            raise Denied("not_regular_file")
        with path.open("rb") as file:
            raw = file.read(262_145)
        if len(raw) > 262_144:
            raise Denied("file_too_large")
        text = raw.decode("utf-8")
        if "\x00" in text:
            raise Denied("not_text")
        end = min(len(text), args.offset + args.limit)
        return {
            "path": args.path,
            "text": text[args.offset : end],
            "next_offset": end if end < len(text) else None,
            "total_chars": len(text),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def _create(self, args: CreateArgs) -> dict[str, Any]:
        path: Path = self.workspace.path(args.path, write=True)
        raw = args.content.encode("utf-8")
        # Exclusive create prevents overwrites. A crash can still leave a partial file.
        with path.open("xb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        return {"path": args.path, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
