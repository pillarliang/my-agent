# Learn Agent: run and inspect the system

## Choose an entry point

The Web UI and CLI use the same application service and agent core. Start either one directly; a CLI run is not a prerequisite for the Web UI. This guide uses normal technical prose and does not require ASD-STE100 compliance.

Every execution entry point uses a real model. The default backend is DashScope with `qwen3.8-flash`; configuration or API failures produce an error. Model SDK dependencies are installed with the base project.

## Start the Web UI

Run these commands in the project directory:

```sh
cd /Users/drew/Documents/dev/learn-agent
uv sync --locked --extra web
uv run --extra web learn-agent serve --provider dashscope --allow-write
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Ask the agent to read `notes.txt` and explain its learning goal, then inspect the tool progress and result. The model label should show `DashScope · qwen3.8-flash`. Keep the terminal open while the server runs.

The backend reads the API key, endpoint and model from the local `.env`; an existing shell variable takes precedence over the file. A new installation needs its own values based on `.env.example`. When changing the model or endpoint, restart the service and create a new session because previous sessions retain their original bindings.

The `--allow-write` flag permits new files inside the workspace. Remove it for read-only tasks. The default workspace is `examples/workspace`, and the default database is `.learn-agent/state.sqlite3`. Remote model calls incur API usage; the model request count and output allowance are bounded by the run settings.

## Use the CLI instead

From the project directory, submit one request without starting a Web service:

```sh
uv run learn-agent run "Read notes.txt and explain its learning goal" \
  --provider dashscope --session flash-lesson-1 --trace
```

The CLI first prints a system conclusion, actual tool records and check scope, then labels the model text as an explanation. An unchecked result is explicitly unverified; a passing check proves only its stated conditions. `--trace` writes progress to stderr. Reuse the same Session ID for a follow-up request with the same model, endpoint and workspace. Both entry points can query the same database.

## Read, create and verify a report

Use a fresh workspace to keep an existing report from affecting the task:

```sh
report_workspace=$(mktemp -d)
cp examples/workspace/notes.txt "$report_workspace/notes.txt"
uv run learn-agent run "Read notes.txt, then create report.md with the source acceptance marker" \
  --workspace "$report_workspace" --allow-write \
  --verify-file report.md --contains LEARN-42 --trace
```

The model chooses its tools using actual observations. On success, expect `status=completed verification=passed` and inspect the new report. The number and order of steps may vary, so use the event records to trace the actual path. A contains check proves the current file condition; use tool results and the fresh workspace to assess evidence of this run's effects.

## Inspect a Run

Replace `RUN_ID` with the ID printed by the CLI, then inspect the ordered events:

```sh
uv run learn-agent inspect RUN_ID
```

Find a `tool.started` event and its corresponding `tool.finished`, compare their call IDs, and read the result code. Normal events contain metadata; the saved transcript also contains user messages, tool arguments and results.

## Stop or recover after a failure

For a CLI run, press `Ctrl+C`, read the final status and inspect any files it could have created. Cancellation does not remove files, and a forced stop can prevent the final database write. For the Web UI, closing a browser only disconnects observation; stopping the server normally waits for accepted tasks to finish.

The MVP does not resume interrupted runs automatically. If a forced stop leaves a run marked as running, first confirm the original process has stopped, inspect the events and check the workspace. Then release the old run:

```sh
uv run learn-agent abandon RUN_ID
```

The command refuses if the recorded process ID still exists. Once the actual file effects are known, submit a new request and describe those effects as needed; abandoning a run does not replay or reverse its tools. See the [runtime boundaries](04-boundaries.md) and [DashScope configuration](12-dashscope.md) for details.

## Run checks and real integration tests

`uv run pytest -m "not live"` checks local components without requesting a model. `uv run pytest` also includes real model integration tests and incurs API usage. Tests for an unconfigured backend are skipped with a reason. `uv run python scripts/evaluate.py` evaluates the three tasks with the default real model; no substitute responses are used.
