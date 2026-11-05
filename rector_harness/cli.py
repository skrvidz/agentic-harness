from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from rich.console import Console

from .app import HarnessApp
from .engine import Harness, hydrate_final_summary
from .evidence import collect_evidence
from .model import HarnessState
from .planner import PlanHarness, write_planner_schema
from .runner import ClaudeRunner, CodexEngineerRunner, CodexPlannerRunner, CodexRunner, DryRunEngineer, DryRunPlanner, DryRunReviewer
from .runtime import ensure_harness_dir
from .storage import SQLiteStore
from .transcript import Transcript


console = Console()
app = typer.Typer(no_args_is_help=False, help="Local terminal-only Claude/Codex harness")

REVIEWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "severity", "summary", "findings", "human_input_required", "human_question", "next_task", "confidence"],
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVED", "REVISE", "NEEDS_HUMAN", "DONE"]},
        "severity": {"type": "string", "enum": ["none", "minor", "major", "blocking"]},
        "summary": {"type": "string", "maxLength": 240},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "issue", "evidence", "required_change"],
                "properties": {
                    "id": {"type": "string"},
                    "issue": {"type": "string"},
                    "evidence": {"type": "string"},
                    "required_change": {"type": "string"},
                },
            },
        },
        "human_input_required": {"type": "boolean"},
        "human_question": {"type": "string"},
        "next_task": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def _write_reviewer_schema(harness_dir: Path) -> None:
    """Keep schemas created by older harness versions compatible with Codex."""
    harness_dir.mkdir(parents=True, exist_ok=True)
    (harness_dir / "reviewer_schema.json").write_text(
        json.dumps(REVIEWER_SCHEMA, indent=2) + "\n", encoding="utf-8"
    )


def _root(ctx: typer.Context) -> Path:
    return Path(ctx.obj).resolve()


@app.callback(invoke_without_command=True)
def callback(ctx: typer.Context, root: Annotated[Path, typer.Option("--root", help="Project working tree")]=Path.cwd()) -> None:
    ctx.ensure_object(dict)
    ctx.obj = root
    if ctx.invoked_subcommand is None:
        from .workspace import WorkspaceApp
        WorkspaceApp().run()


@app.command()
def init(
    ctx: typer.Context,
    objective: Annotated[str, typer.Option(help="Human objective")],
    acceptance: Annotated[list[str] | None, typer.Option("--acceptance", help="Acceptance criterion; repeatable")] = None,
    task: Annotated[str, typer.Option(help="First engineer task")] = "",
    test_command: Annotated[str | None, typer.Option(help="Test command, tokenized without a shell")] = None,
    max_cycles: Annotated[int, typer.Option(min=1, help="Maximum engineer/reviewer cycles")] = 10,
    mode: Annotated[str, typer.Option(help="Orchestration mode: single or plan")] = "single",
    parallelism: Annotated[int, typer.Option(min=1, help="Maximum parallel plan tickets")] = 1,
) -> None:
    root = _root(ctx)
    harness_dir = ensure_harness_dir(root)
    state = HarnessState(objective=objective, acceptance_criteria=acceptance or [], current_task=task,
                         max_cycles=max_cycles, test_command=test_command, run_id=str(uuid4()),
                         mode=mode, parallelism=parallelism)
    state.save(harness_dir / "state.json")
    (harness_dir / "objective.md").write_text(
        f"# Objective\n\n{objective}\n\n## Acceptance criteria\n" +
        "\n".join(f"- {item}" for item in state.acceptance_criteria) + "\n", encoding="utf-8")
    _write_reviewer_schema(harness_dir)
    write_planner_schema(harness_dir)
    store = SQLiteStore(harness_dir / "harness.db")
    store.save_state(state)
    store.close()
    console.print(f"[green]Initialized[/green] {harness_dir}")


def _load(ctx: typer.Context) -> tuple[Path, HarnessState]:
    root = _root(ctx)
    harness_dir = ensure_harness_dir(root)
    path = harness_dir / "state.json"
    if not path.exists():
        raise typer.BadParameter(f"not initialized: {path}")
    _write_reviewer_schema(harness_dir)
    state = HarnessState.load(path)
    hydrate_final_summary(root, state)
    return root, state


@app.command()
def run(
    ctx: typer.Context,
    tui: Annotated[bool, typer.Option("--tui/--headless", help="Run Textual UI or plain terminal output")] = True,
    dry_run: Annotated[bool, typer.Option(help="Do not launch agent processes")] = False,
    review_verdict: Annotated[str, typer.Option(help="Dry-run reviewer verdict")] = "DONE",
    max_cycles: Annotated[int | None, typer.Option(min=1)] = None,
    non_interactive: Annotated[bool, typer.Option(help="Pause instead of prompting at human gates")] = False,
    engineer_model: Annotated[str | None, typer.Option(help="Engineer CLI model alias (overrides saved config)")] = None,
    reviewer_model: Annotated[str | None, typer.Option(help="Reviewer CLI model (overrides saved config)")] = None,
    engineer_command: Annotated[str | None, typer.Option(help="Engineer executable (overrides saved config)")] = None,
    reviewer_command: Annotated[str | None, typer.Option(help="Reviewer executable (overrides saved config)")] = None,
    max_engineer_turns: Annotated[int | None, typer.Option(min=1, help="Maximum engineer turns (overrides saved config)")] = None,
    plan: Annotated[bool, typer.Option("--plan", help="Run planner-driven dependency graph mode")] = False,
    parallelism: Annotated[int | None, typer.Option(min=1, help="Maximum plan tickets running in parallel")] = None,
    planner_model: Annotated[str | None, typer.Option(help="Planner CLI model (overrides saved config)")] = None,
    planner_command: Annotated[str | None, typer.Option(help="Planner executable (overrides saved config)")] = None,
) -> None:
    root, state = _load(ctx)
    if state.status == "completed":
        console.print("Objective already completed. Use reset only to start over.")
        raise typer.Exit(0)
    planning_mode = plan or state.mode == "plan"
    if planning_mode:
        state.mode = "plan"
        state.planner_model = planner_model or state.planner_model
        state.planner_command = planner_command or state.planner_command
        state.parallelism = parallelism or state.parallelism
        state.save(root / ".harness" / "state.json")
        store = SQLiteStore(root / ".harness" / "harness.db")
        store.save_state(state)
        store.close()
        write_planner_schema(root / ".harness")
        planner = DryRunPlanner() if dry_run else CodexPlannerRunner(root, state.planner_model, executable=state.planner_command)

        def engineer_factory(ticket_root, ticket):
            if dry_run:
                return DryRunEngineer()
            selected_command = state.engineer_command
            if Path(selected_command).name == "codex":
                return CodexEngineerRunner(ticket_root, state.engineer_model, executable=selected_command)
            return ClaudeRunner(ticket_root, state.engineer_model, state.max_engineer_turns, executable=selected_command)

        def reviewer_factory(ticket_root, ticket):
            return DryRunReviewer("DONE") if dry_run else CodexRunner(
                ticket_root, state.reviewer_model, executable=state.reviewer_command
            )

        plan_harness = PlanHarness(root, state, planner, engineer_factory, reviewer_factory)
        if tui and sys.stdout.isatty():
            textual_app = HarnessApp(plan_harness, non_interactive=non_interactive)
            textual_app.run()
            raise typer.Exit(0)
        raise typer.Exit(plan_harness.run(parallelism, non_interactive))
    if dry_run:
        engineer, reviewer = DryRunEngineer(), DryRunReviewer(review_verdict.upper())
    else:
        selected_engineer_command = engineer_command or state.engineer_command
        selected_engineer_model = engineer_model or state.engineer_model
        if Path(selected_engineer_command).name == "codex":
            engineer = CodexEngineerRunner(root, selected_engineer_model, executable=selected_engineer_command)
        else:
            engineer = ClaudeRunner(
                root,
                selected_engineer_model,
                max_engineer_turns or state.max_engineer_turns,
                executable=selected_engineer_command,
            )
        reviewer = CodexRunner(
            root,
            reviewer_model or state.reviewer_model,
            executable=reviewer_command or state.reviewer_command,
        )
    if tui and sys.stdout.isatty():
        app_holder: dict[str, HarnessApp] = {}

        async def decision_provider(question: str) -> str:
            return await app_holder["app"].ask_decision(question)

        harness = Harness(root, state, engineer, reviewer, decision_provider=decision_provider)
        textual_app = HarnessApp(harness, max_cycles=max_cycles, non_interactive=non_interactive)
        app_holder["app"] = textual_app
        textual_app.run()
    else:
        harness = Harness(root, state, engineer, reviewer)
        raise typer.Exit(harness.run(max_cycles, non_interactive))


@app.command()
def resume(ctx: typer.Context, headless: Annotated[bool, typer.Option(help="Resume without Textual")] = False) -> None:
    """Resume a paused run."""
    run(ctx, tui=not headless)


@app.command()
def status(ctx: typer.Context) -> None:
    root, state = _load(ctx)
    console.print_json(json.dumps(state.model_dump()))
    db = root / ".harness" / "harness.db"
    if db.exists():
        store = SQLiteStore(db)
        console.print(f"Events stored: {len(store.recent_events())}")
        store.close()


@app.command(name="inspect")
def inspect_command(ctx: typer.Context) -> None:
    """Alias for status."""
    status(ctx)


@app.command()
def transcript(ctx: typer.Context, tail: Annotated[int, typer.Option(min=1)] = 100) -> None:
    root, _ = _load(ctx)
    path = root / ".harness" / "transcript.jsonl"
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines[-tail:]:
        console.print(line, markup=False)


@app.command()
def diff(ctx: typer.Context) -> None:
    root, state = _load(ctx)
    console.print(collect_evidence(root, state)["git_diff"], markup=False)


@app.command()
def decision(ctx: typer.Context, answer: Annotated[list[str], typer.Argument(help="Decision/context")]) -> None:
    root, state = _load(ctx)
    value = " ".join(answer)
    state.current_task = f"Human decision: {value}\nContinue implementation and run tests."
    state.open_questions = []
    state.status = "working"
    state.next_actor = "engineer"
    state.pending_engineer_summary = ""
    state.pending_engineer_turn = None
    state.save(root / ".harness" / "state.json")
    store = SQLiteStore(root / ".harness" / "harness.db")
    store.save_state(state)
    store.record_decision(state, "Manual CLI decision", value)
    store.close()
    Transcript(root / ".harness" / "transcript.jsonl").event("human_decision", answer=value)
    console.print("Decision saved. Run `agent-harness run` to resume.")


@app.command()
def stop(ctx: typer.Context) -> None:
    root, state = _load(ctx)
    state.status = "paused"
    state.open_questions = ["Stopped by human."]
    state.save(root / ".harness" / "state.json")
    store = SQLiteStore(root / ".harness" / "harness.db")
    store.save_state(state)
    store.close()
    console.print("Run marked paused.")


@app.command()
def reset(ctx: typer.Context) -> None:
    root, _ = _load(ctx)
    harness_dir = root / ".harness"
    if typer.confirm(f"Delete runtime files under {harness_dir}?", default=False):
        import shutil
        shutil.rmtree(harness_dir)
        console.print("Runtime state removed")


def main() -> None:
    app()
