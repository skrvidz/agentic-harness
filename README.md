# Agentic Harness

An experimental, local harness for an engineer agent and an independent reviewer. It makes the
turn sequence, evidence, human decisions and restart state visible without adding an agent service
or external task manager.

The default flow uses two locally installed terminal agents:

```text
Claude Code (write/test) -> Codex (read-only review) -> Claude Code
                                      |
                              human decision when needed
```

The harness does not make API requests itself and does not use SDKs, MCP, or a multi-agent framework. It
starts the existing `claude` and `codex` executables as local subprocesses in the selected project
directory; those terminal clients retain responsibility for their own authenticated transport.
It orchestrates non-interactive terminal turns (`claude -p` and `codex exec`); it does not attach to an
already-running interactive terminal process.

## Quick start

Install the local package once:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Open the workspace from anywhere:

```bash
.venv/bin/agent-harness
```

The workspace is the main interface. It keeps a local registry at
`~/.agent-harness/workspace.json`, lets you add existing projects or create a new run configuration,
and opens a guided five-step editor (goal, flow, agents, execution, review) instead of one
large form. The final review summarizes the complete configuration before saving. It lets you edit
the objective, acceptance criteria, current task, test command, cycle limits, engineer/reviewer
executable and model before pressing Run. The executable fields are plain local
program names or paths; the harness still uses `asyncio` subprocesses without a shell, API requests,
SDKs or MCP.

From the target project (the directory where Claude will edit), the CLI remains available:

```bash
.venv/bin/python -m rector_harness init \
  --objective "Implement reliable ingestion" \
  --acceptance "all tests pass" \
  --acceptance "retry behavior is idempotent" \
  --test-command "pytest -q"

# Safe protocol smoke test; does not launch either agent.
.venv/bin/python -m rector_harness run --headless --dry-run

# Real orchestration with the Textual TUI.
.venv/bin/python -m rector_harness run

# Real orchestration without the TUI (CI or a redirected terminal).
.venv/bin/python -m rector_harness run --headless --non-interactive

```

`run` executes the selected engineer with edit permissions and starts new Codex reviewer sessions
with a read-only sandbox policy. Claude is
launched with `--output-format stream-json --include-partial-messages`, so its text and tool activity
are visible while the process runs. The harness passes evidence, not the full engineer conversation,
to the reviewer. The reviewer receives
`.harness/reviewer_schema.json` through Codex's local CLI structured-output option. Every raw result
is stored in `.harness/artifacts/`. Events are published to the TUI and stored in both
`.harness/transcript.jsonl` and `.harness/harness.db`.

The workspace TUI uses `n` to create a run, `a` to add an existing project, `e`/`s` to edit and save,
`r` to run the selected project, `u` to add a follow-up task after a completed run, `w` to ask the
reviewer a standalone read-only question, `c` to explicitly continue after a cycle-limit stop, `p`
to request a safe pause, `d` to add a human decision, `i` to inject context, `f` to browse file changes and
their full diff, `g` to inspect the GitHub remote/branch/PR, `t` to change and persist the theme,
`k` to inspect the planner ticket DAG, `v` to browse complete parsed agent responses (`r` inside that view toggles raw JSONL), `h` to reload the
persisted activity history, `x` to remove a project from the workspace index, and
`q` to quit. Selecting a run hydrates the activity panel from its `transcript.jsonl`; live events are
appended automatically after the history. It is a view over the persisted state/event bus; it is not
the source of truth.

For dependency-aware orchestration, use the workspace TUI and set `Orchestration mode` to
`Planner DAG · tickets` in the run editor. The planner creates `.harness/plan.json`; tickets whose
dependencies are complete are scheduled automatically. Clean Git projects use one isolated
worktree per parallel ticket and merge completed ticket branches back into the selected project.
Use the `Parallel plan tickets` field to control the maximum number of concurrent tickets.
When all tickets complete, the same planner session evaluates the integrated result.

Plans are generated from the local objective, acceptance criteria and repository evidence. Tickets
may carry a generic source reference, but no task manager or account is required.

The complete-response view detects local image references in agent responses. In Kitty, referenced PNG/JPEG/GIF/WebP
figures are previewed inline using the Kitty graphics protocol; the image selector appears when a response mentions
more than one local figure.

When the reviewer returns `DONE`, it must provide a concise `summary` (maximum 240 characters). The
harness stores it in `state.json`, `.harness/final_summary.md`, the `review` event and the final
`completed` event. The selected run displays that conclusion above the activity log.

Press `f` to see the working-tree snapshot relative to `HEAD`. It includes tracked staged/unstaged
changes and untracked files, classifies them as added/modified/deleted/renamed, shows line totals,
and lets you select a file to inspect its patch or content. Press `g` for the local branch and
GitHub context; `o` in that dialog opens the pull request or branch in the macOS browser. The GitHub
view is read-only and does not create commits, pushes or pull requests.

## Commands

```text
agent-harness                         # open the workspace TUI
agent-harness init --objective TEXT [--acceptance TEXT] [--test-command COMMAND]
agent-harness run [--tui|--headless] [--dry-run] [--max-cycles N] [--non-interactive]
agent-harness resume [--headless]
agent-harness inspect
agent-harness status
agent-harness transcript [--tail N]
agent-harness diff
agent-harness decision ANSWER...
agent-harness stop
agent-harness reset
```

When a reviewer returns `NEEDS_HUMAN`, interactive mode asks for a decision. In automation use
`--non-interactive`; the state becomes `paused`, and `decision ...` stores the answer before the next
`run`.

The persisted `next_actor` makes restart points explicit. If an engineer has already finished and the
reviewer process fails, the next `run` retries the reviewer directly; V1 paused reviewer failures are
migrated automatically from the transcript. The workspace uses the same state, so selecting Run on a
paused project resumes the correct actor.

The Codex model defaults to `gpt-5.6-sol` and Claude defaults to the `opus` alias. Override them with
`--reviewer-model` and `--engineer-model` if the installed terminal clients expose different aliases.

SQLite stores runs, turns, events, agent messages, reviews and human decisions. The JSON files remain
human-readable snapshots and raw artifacts for recovery and inspection.

## Safety boundaries

- The selected engineer CLI is given edit-capable permissions.
- Codex is launched read-only for new reviewer sessions. Resumed sessions retain their original CLI policy.
- Prompts and evidence are passed as subprocess arguments; the harness contains no API client or network transport.
- Test commands are tokenized with `shlex` and run without a shell.
- The harness stops on agent process failure, malformed reviewer JSON, human decisions, or cycle limit.
- `reset` removes harness runtime files only after an interactive confirmation.

## Publishing and local data

This repository contains the harness source and tests. Each target project stores its run state,
transcripts, raw agent output and SQLite database under its own ignored `.harness/` directory. Agent
output and test logs may contain sensitive project data; inspect them before sharing a target project.
The harness runs local agent executables and the configured test command with the user's permissions,
so select target projects and commands you trust. Parallel tickets require isolated Git worktrees;
the run pauses if isolation cannot be established.
The harness refuses a symlinked `.harness` directory and restricts that directory to its owner.
Image previews use Pillow 13 or newer. See [SECURITY.md](SECURITY.md) for the trust model and reporting.

Run the test suite before publishing changes:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Licensed under the MIT License. See [LICENSE](LICENSE).
