"""An independent, deterministic task check. No model judge is needed here."""

from learn_agent.contracts import CheckResult, Verification
from learn_agent.policy import Workspace


class FileContainsVerifier:
    def __init__(self, workspace: Workspace, path: str, contains: str) -> None:
        self.workspace, self.path, self.contains = workspace, path, contains

    def check(self, answer: str) -> Verification:
        # Reuse bounded tool reads. A directory, symlink, or large file fails the check.
        import json

        from learn_agent.contracts import ToolCall
        from learn_agent.tools import ToolExecutor

        result = ToolExecutor(self.workspace).execute(
            ToolCall(
                id="verification",
                name="read_file",
                arguments=json.dumps({"path": self.path, "offset": 0, "limit": 8000}),
            )
        )
        passed = result.ok and self.contains in result.data.get("text", "")
        return Verification(
            status="passed" if passed else "failed",
            detail="Required text is present in the first 8000 characters."
            if passed
            else "Required text was not found, or the file could not be read.",
            checks=[
                CheckResult(
                    name="file_contains",
                    status="passed" if passed else "failed",
                    target=self.path,
                    detail=f"检查文件前 8000 个字符是否包含 {self.contains!r}。",
                    evidence={
                        "read_code": result.code,
                        "expected_text": self.contains,
                        "checked_chars": len(result.data.get("text", "")),
                        "sha256": result.data.get("sha256"),
                    },
                )
            ],
        )
