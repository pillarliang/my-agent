"""The loop owns control flow. Adapters own I/O and protocol details."""

from collections.abc import Callable
from time import monotonic
from typing import Any

from learn_agent.context import build_context
from learn_agent.contracts import (
    ContextOverflow,
    Limits,
    Message,
    Model,
    ModelError,
    RunResult,
    RunState,
    Status,
    Store,
    ToolEvidence,
    Verification,
    Verifier,
)
from learn_agent.tools import ToolExecutor


class Runner:
    def __init__(
        self,
        model: Model,
        tools: ToolExecutor,
        store: Store,
        *,
        model_key: str,
        limits: Limits | None = None,
        verifier: Verifier | None = None,
        clock: Callable[[], float] = monotonic,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.model, self.tools, self.store = model, tools, store
        self.model_key, self.limits = model_key, limits or Limits()
        self.verifier, self.clock, self.on_event = verifier, clock, on_event

    def run(self, question: str, *, session_id: str) -> RunResult:
        if not question.strip() or not session_id.strip():
            raise ValueError("Question and session ID must not be empty.")
        run_id = self.store.start(session_id, str(self.tools.workspace.root), self.model_key)
        state = RunState(current=[Message(role="user", text=question)])
        started = self.clock()
        status: Status = "step_limit"
        answer, steps, input_tokens, output_tokens = "", 0, 0, 0
        usage_complete = True
        verification = Verification()
        tool_evidence: list[ToolEvidence] = []

        def emit(name: str, **data: Any) -> None:
            self.store.event(run_id, name, data)
            if self.on_event:
                self.on_event(name, {"run_id": run_id, **data})

        def remaining() -> float:
            return self.limits.max_seconds - (self.clock() - started)

        try:
            self.store.save_messages(run_id, state.current)
            emit("run.started", session_id=session_id)
            history = self.store.history(session_id)
            for step in range(1, self.limits.max_steps + 1):
                if remaining() <= 0:
                    status = "time_limit"
                    break
                context = build_context(
                    history, state.current, self.tools.specs, self.limits.max_input_bytes
                )
                steps = step
                emit(
                    "model.started",
                    step=step,
                    input_bytes=context.input_bytes,
                    omitted_turns=context.omitted_turns,
                )
                reply = self.model.respond(
                    context,
                    timeout=min(self.limits.model_timeout, remaining()),
                    max_output_tokens=self.limits.max_output_tokens,
                )
                input_tokens += reply.input_tokens or 0
                output_tokens += reply.output_tokens or 0
                usage_complete &= reply.input_tokens is not None and reply.output_tokens is not None
                emit(
                    "model.finished",
                    step=step,
                    calls=len(reply.calls),
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                )
                # Validate the whole batch's identities BEFORE any effect.
                ids = [call.id for call in reply.calls]
                if (
                    len(ids) > self.limits.max_calls_per_step
                    or len(set(ids)) != len(ids)
                    or any(not id_ or id_ in state.seen_calls for id_ in ids)
                    or (not reply.calls and not reply.text.strip())
                ):
                    status = "protocol_error"
                    break
                state.seen_calls.update(ids)
                state.current.append(
                    Message(
                        role="assistant",
                        text=reply.text,
                        calls=reply.calls,
                        provider_items=reply.provider_items,
                    )
                )
                self.store.save_messages(run_id, state.current)
                if remaining() <= 0:
                    status = "time_limit"
                    break
                if not reply.calls:
                    answer = reply.text
                    if self.verifier:
                        verification = self.verifier.check(answer)
                    status = (
                        "verification_failed" if verification.status == "failed" else "completed"
                    )
                    break
                for call in reply.calls:
                    if remaining() <= 0:
                        status = "time_limit"
                        break
                    emit("tool.started", step=step, call_id=call.id, tool=call.name)
                    result = self.tools.execute(call)
                    tool_evidence.append(
                        ToolEvidence(
                            call_id=call.id,
                            tool=call.name,
                            ok=result.ok,
                            code=result.code,
                            path=result.data.get("path"),
                            sha256=result.data.get("sha256"),
                        )
                    )
                    state.current.append(Message(role="tool", call_id=call.id, result=result))
                    self.store.save_messages(run_id, state.current)
                    emit(
                        "tool.finished",
                        step=step,
                        call_id=call.id,
                        tool=call.name,
                        ok=result.ok,
                        code=result.code,
                    )
                if status == "time_limit":
                    break
        except ContextOverflow:
            status = "context_limit"
        except ModelError as exc:
            status = "model_error"
            usage_complete = False
            emit("model.error", code=str(exc))
        except KeyboardInterrupt:
            status = "cancelled"
            usage_complete = False
        except Exception as exc:
            status = "internal_error"
            # Store an error class. Never log the exception body or credentials.
            emit("run.error", error_type=type(exc).__name__)
        run_result = RunResult(
            run_id=run_id,
            session_id=session_id,
            status=status,
            answer=answer,
            steps=steps,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_complete=usage_complete,
            verification=verification,
            tool_evidence=tool_evidence,
        )
        self.store.finish(run_result, state.current)
        return run_result
