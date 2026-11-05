from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Footer, Header, Input, Label, ListItem, ListView, RichLog, Select, Static, TextArea

try:
    from textual_image.widget import Image as TerminalImage
except ImportError:  # pragma: no cover - installed with the package dependencies
    TerminalImage = None

from .app import DecisionScreen, HarnessEventMessage
from .changes import FileChange, change_summary, collect_changes, file_detail
from .engine import Harness, hydrate_final_summary, parse_review
from .events import HarnessEvent
from .evidence import collect_evidence
from .github import GitHubInfo, inspect_github, open_url
from .model import ExecutionPlan, HarnessState
from .planner import PlanHarness, write_planner_schema
from .prompts import reviewer_question_prompt
from .rendering import render_activity_event, render_activity_record, render_overview
from .runner import ClaudeRunner, CodexEngineerRunner, CodexPlannerRunner, CodexRunner, _parse_claude, _parse_codex
from .runtime import ensure_harness_dir
from .storage import SQLiteStore
from .transcript import Transcript


class WorkspaceRegistry:
    """Small human-readable index of projects that have harness runs."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path.home() / ".agent-harness" / "workspace.json")
        self.paths: list[str] = []
        self.theme = "textual-dark"
        self.load()

    def load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            values = payload.get("projects", []) if isinstance(payload, dict) else []
            self.paths = sorted({str(Path(value).expanduser().resolve()) for value in values if isinstance(value, str)})
            configured_theme = payload.get("theme") if isinstance(payload, dict) else None
            if isinstance(configured_theme, str) and configured_theme:
                self.theme = configured_theme
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.paths = []
            self.theme = "textual-dark"

    def add(self, root: Path) -> None:
        value = str(root.expanduser().resolve())
        self.paths = sorted(set(self.paths) | {value})
        self.save()

    def remove(self, root: Path) -> None:
        value = str(root.expanduser().resolve())
        self.paths = [path for path in self.paths if path != value]
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"theme": self.theme, "projects": self.paths}, indent=2) + "\n", encoding="utf-8")


def _preset_for(command: str, model: str, role: str) -> str:
    value = f"{Path(command).name}|{model}"
    known = {
        "engineer": {"claude|opus", "claude|sonnet", "codex|gpt-5.6-luna"},
        "reviewer": {"codex|gpt-5.6-sol", "codex|gpt-5.6-luna"},
    }
    return value if value in known.get(role, set()) else "custom"


class RunEditorScreen(ModalScreen[dict[str, str] | None]):
    STEP_NAMES = ("GOAL", "SOURCES", "AGENTS", "EXECUTION", "REVIEW")

    CSS = """
    RunEditorScreen { align: center middle; }
    #editor { width: 110; max-width: 96%; height: 94%; border: double $accent; background: $surface; padding: 1 2; }
    #editor-title { color: $accent; text-style: bold; }
    #stepper { height: 3; border: round $primary; padding: 1; margin: 1 0; color: $text; }
    #wizard-steps { height: 1fr; }
    .wizard-step { padding: 0 1; }
    .section-title { color: $accent; text-style: bold; margin-top: 1; }
    .section-help { color: $text-muted; margin-bottom: 1; }
    .field-label { margin-top: 1; color: $text-muted; text-style: bold; }
    .help { color: $text-muted; margin-bottom: 1; }
    #review-summary { height: 1fr; border: round $secondary; padding: 1 2; color: $text; }
    #editor-validation { height: 2; color: $warning; }
    #editor-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    #editor-buttons Button { margin-left: 1; }
    TextArea { height: 5; }
    """

    def __init__(self, values: dict[str, str] | None = None, title: str = "CONFIGURE RUN") -> None:
        super().__init__()
        self.values = values or {}
        self.title = title
        self.step = 0

    def _value(self, key: str) -> str:
        return self.values.get(key, "")

    @staticmethod
    def _preset_options(role: str) -> list[tuple[str, str]]:
        if role == "engineer":
            return [
                ("Claude Code · Opus 5", "claude|opus"),
                ("Claude Code · Sonnet", "claude|sonnet"),
                ("Codex · GPT-5.6 Luna", "codex|gpt-5.6-luna"),
                ("Custom / wrapper", "custom"),
            ]
        return [
            ("Codex · GPT-5.6 Sol", "codex|gpt-5.6-sol"),
            ("Codex · GPT-5.6 Luna", "codex|gpt-5.6-luna"),
            ("Custom / wrapper", "custom"),
        ]

    def compose(self) -> ComposeResult:
        with Vertical(id="editor"):
            yield Label(self.title, id="editor-title")
            yield Static("Configure the run in five focused steps. Nothing is executed until you save.", classes="help")
            yield Static("", id="stepper")
            with ContentSwitcher(initial="step-goal", id="wizard-steps"):
                with VerticalScroll(id="step-goal", classes="wizard-step"):
                    yield Static("1 · GOAL", classes="section-title")
                    yield Static("Define what success means and where the work lives.", classes="section-help")
                    yield Label("Project root", classes="field-label")
                    yield Input(value=self._value("root"), placeholder="/path/to/project", id="root")
                    yield Label("Objective", classes="field-label")
                    yield Input(value=self._value("objective"), placeholder="What should be achieved?", id="objective")
                    yield Label("Acceptance criteria · one per line", classes="field-label")
                    yield TextArea(text=self._value("acceptance"), id="acceptance")
                    yield Label("Current task / additional context", classes="field-label")
                    yield TextArea(text=self._value("task"), id="task")

                with VerticalScroll(id="step-sources", classes="wizard-step"):
                    yield Static("2 · SOURCES & FLOW", classes="section-title")
                    yield Static("Choose a simple pair or let a planner split the objective into dependent tickets.", classes="section-help")
                    yield Label("Orchestration mode", classes="field-label")
                    yield Select([("Single · Engineer → Reviewer", "single"), ("Planner DAG · tickets", "plan")],
                                 value=self._value("mode") or "single", id="mode")

                with VerticalScroll(id="step-agents", classes="wizard-step"):
                    yield Static("3 · AGENTS", classes="section-title")
                    yield Static("Pick a preset or edit the executable and model freely. These are local CLIs.", classes="section-help")
                    yield Static("ENGINEER · writes code, runs tests", classes="section-title")
                    yield Label("Preset", classes="field-label")
                    yield Select(self._preset_options("engineer"), value=self._value("engineer_preset") or Select.NULL,
                                 allow_blank=True, id="engineer_preset")
                    yield Label("Executable / provider CLI", classes="field-label")
                    yield Input(value=self._value("engineer_command") or "claude", placeholder="claude", id="engineer_command")
                    yield Label("Model", classes="field-label")
                    yield Input(value=self._value("engineer_model") or "opus", placeholder="opus", id="engineer_model")
                    yield Static("REVIEWER · read-only critic and gatekeeper", classes="section-title")
                    yield Label("Preset", classes="field-label")
                    yield Select(self._preset_options("reviewer"), value=self._value("reviewer_preset") or Select.NULL,
                                 allow_blank=True, id="reviewer_preset")
                    yield Label("Executable / provider CLI", classes="field-label")
                    yield Input(value=self._value("reviewer_command") or "codex", placeholder="codex", id="reviewer_command")
                    yield Label("Model", classes="field-label")
                    yield Input(value=self._value("reviewer_model") or "gpt-5.6-sol", placeholder="gpt-5.6-sol", id="reviewer_model")
                    yield Static("PLANNER · scopes the DAG and evaluates the finish", classes="section-title")
                    yield Label("Executable / provider CLI", classes="field-label")
                    yield Input(value=self._value("planner_command") or "codex", placeholder="codex", id="planner_command")
                    yield Label("Model", classes="field-label")
                    yield Input(value=self._value("planner_model") or "gpt-5.6-sol", placeholder="gpt-5.6-sol", id="planner_model")

                with VerticalScroll(id="step-execution", classes="wizard-step"):
                    yield Static("4 · EXECUTION", classes="section-title")
                    yield Static("Set the guardrails for tests, parallel tickets and subprocess turns.", classes="section-help")
                    yield Label("Parallel tickets", classes="field-label")
                    yield Input(value=self._value("parallelism") or "1", id="parallelism")
                    yield Label("Test command · arguments separated by spaces", classes="field-label")
                    yield Input(value=self._value("test_command"), placeholder="pytest -q", id="test_command")
                    yield Label("Maximum cycles", classes="field-label")
                    yield Input(value=self._value("max_cycles") or "10", id="max_cycles")
                    yield Label("Maximum engineer turns per subprocess", classes="field-label")
                    yield Input(value=self._value("max_engineer_turns") or "30", id="max_engineer_turns")
                    yield Static("For planner mode, clean Git repositories use isolated worktrees so ready tickets can run in parallel.", classes="section-help")

                with Vertical(id="step-review", classes="wizard-step"):
                    yield Static("5 · REVIEW & SAVE", classes="section-title")
                    yield Static("Check the run at a glance before creating or updating it.", classes="section-help")
                    yield Static("", id="review-summary")
            yield Static("", id="editor-validation")
            with Horizontal(id="editor-buttons"):
                yield Button("Back", id="back")
                yield Button("Next", variant="primary", id="next")
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._show_step(0)

    def _collect(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for key in ("root", "objective", "engineer_command", "engineer_model", "reviewer_command",
                    "reviewer_model", "planner_command", "planner_model", "parallelism", "test_command",
                    "max_cycles", "max_engineer_turns"):
            result[key] = self.query_one(f"#{key}", Input).value.strip()
        result["acceptance"] = self.query_one("#acceptance", TextArea).text.strip()
        result["task"] = self.query_one("#task", TextArea).text.strip()
        result["engineer_preset"] = str(self.query_one("#engineer_preset", Select).value)
        result["reviewer_preset"] = str(self.query_one("#reviewer_preset", Select).value)
        result["mode"] = str(self.query_one("#mode", Select).value)
        return result

    def _show_step(self, index: int) -> None:
        self.step = max(0, min(index, len(self.STEP_NAMES) - 1))
        current = f"step-{self.STEP_NAMES[self.step].lower()}"
        self.query_one("#wizard-steps", ContentSwitcher).current = current
        parts = []
        for index, name in enumerate(self.STEP_NAMES):
            marker = "●" if index == self.step else ("✓" if index < self.step else "○")
            parts.append(f"{marker} {name}")
        self.query_one("#stepper", Static).update("  →  ".join(parts))
        self.query_one("#back", Button).styles.display = "none" if self.step == 0 else "block"
        self.query_one("#next", Button).styles.display = "none" if self.step == len(self.STEP_NAMES) - 1 else "block"
        self.query_one("#save", Button).label = "Create run" if self.title.startswith("NEW") else "Save run"
        if self.step == len(self.STEP_NAMES) - 1:
            self._update_review_summary()

    def _set_validation(self, message: str = "") -> None:
        self.query_one("#editor-validation", Static).update(message)

    def _validate_step(self, index: int) -> str | None:
        if index == 0:
            root = self.query_one("#root", Input).value.strip()
            if not root:
                return "Project root is required."
            if not Path(root).expanduser().is_dir():
                return "Project root must be an existing directory."
            if not self.query_one("#objective", Input).value.strip():
                return "Objective is required."
        elif index == 1:
            if str(self.query_one("#mode", Select).value) not in {"single", "plan"}:
                return "Choose an orchestration mode."
        elif index == 2:
            fields = (("engineer_command", "Engineer executable"), ("engineer_model", "Engineer model"),
                      ("reviewer_command", "Reviewer executable"), ("reviewer_model", "Reviewer model"))
            if str(self.query_one("#mode", Select).value) == "plan":
                fields += (("planner_command", "Planner executable"), ("planner_model", "Planner model"))
            for field, label in fields:
                if not self.query_one(f"#{field}", Input).value.strip():
                    return f"{label} is required."
        elif index == 3:
            for field, label in (("parallelism", "Parallel tickets"), ("max_cycles", "Maximum cycles"),
                                  ("max_engineer_turns", "Maximum engineer turns")):
                value = self.query_one(f"#{field}", Input).value.strip()
                try:
                    if int(value) < 1:
                        raise ValueError
                except ValueError:
                    return f"{label} must be a positive integer."
        return None

    def _update_review_summary(self) -> None:
        values = self._collect()
        mode = "Planner DAG" if values["mode"] == "plan" else "Single Engineer → Reviewer"
        summary = (
            "READY TO SAVE\n\n"
            f"GOAL\n  {values['objective'] or '(no objective)'}\n\n"
            f"FLOW\n  {mode}\n"
            f"  Engineer  {values['engineer_command']} / {values['engineer_model']}\n"
            f"  Reviewer  {values['reviewer_command']} / {values['reviewer_model']}\n"
            f"  Planner   {values['planner_command']} / {values['planner_model']}\n\n"
            f"PROJECT\n  Root    {values['root']}\n\n"
            f"EXECUTION\n  Tests: {values['test_command'] or '(not configured)'}\n"
            f"  Parallelism: {values['parallelism']} · Cycles: {values['max_cycles']} · Turns: {values['max_engineer_turns']}"
        )
        self.query_one("#review-summary", Static).update(summary)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "mode":
            if self.step == len(self.STEP_NAMES) - 1:
                self._update_review_summary()
            return
        if event.value in (Select.NULL, Select.BLANK, None, "custom"):
            return
        try:
            command, model = str(event.value).split("|", 1)
        except ValueError:
            return
        if event.select.id == "engineer_preset":
            self.query_one("#engineer_command", Input).value = command
            self.query_one("#engineer_model", Input).value = model
        elif event.select.id == "reviewer_preset":
            self.query_one("#reviewer_command", Input).value = command
            self.query_one("#reviewer_model", Input).value = model

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self._set_validation()
            self._show_step(self.step - 1)
        elif event.button.id == "next":
            error = self._validate_step(self.step)
            if error:
                self._set_validation(f"⚠ {error}")
                self.notify(error, severity="warning")
                return
            self._set_validation()
            self._show_step(self.step + 1)
        elif event.button.id == "save":
            for index in range(len(self.STEP_NAMES) - 1):
                error = self._validate_step(index)
                if error:
                    self._show_step(index)
                    self._set_validation(f"⚠ {error}")
                    self.notify(error, severity="warning")
                    return
            self.dismiss(self._collect())
        elif event.button.id == "cancel":
            self.dismiss(None)


class ChangesScreen(ModalScreen[None]):
    """Browsable working-tree delta for the selected run."""

    CSS = """
    ChangesScreen { align: center middle; }
    #changes-dialog { width: 98%; height: 92%; border: thick $accent; background: $surface; padding: 1 2; }
    #changes-header { height: auto; margin-bottom: 1; }
    #changes-body { height: 1fr; }
    #change-list { width: 38%; border: round $secondary; }
    #change-preview { width: 1fr; margin-left: 1; }
    #change-header { height: 3; color: $accent; }
    #change-detail { height: 1fr; border: round $secondary; }
    #changes-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    #changes-buttons Button { margin-left: 1; }
    """

    BINDINGS = [
        ("o", "open_github", "Open GitHub"),
        ("escape", "close", "Close"),
    ]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.changes: list[FileChange] = []
        self.selected_change: FileChange | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="changes-dialog"):
            yield Label("WORKING TREE CHANGES", id="changes-header")
            with Horizontal(id="changes-body"):
                yield ListView(id="change-list")
                with Vertical(id="change-preview"):
                    yield Static("Select a file", id="change-header")
                    yield TextArea("", read_only=True, id="change-detail")
            with Horizontal(id="changes-buttons"):
                yield Button("Open GitHub", id="open-github")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.changes = collect_changes(self.root)
        view = self.query_one("#change-list", ListView)
        if not self.changes:
            view.append(ListItem(Label("Working tree clean")))
            self.query_one("#change-header", Static).update("No uncommitted changes")
            return
        for change in self.changes:
            old = f"  ← {change.old_path}" if change.old_path else ""
            view.append(ListItem(Label(f"{change.status}  {change.path}{old}\n    {change.stats}")))
        view.index = 0
        self._select_change(0)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "change-list" and event.list_view.index is not None:
            self._select_change(event.list_view.index)

    def _select_change(self, index: int) -> None:
        if not 0 <= index < len(self.changes):
            return
        self.selected_change = self.changes[index]
        change = self.selected_change
        self.query_one("#change-header", Static).update(f"{change.status}  {change.path}  ·  {change.label}  ·  {change.stats}")
        self.query_one("#change-detail", TextArea).text = file_detail(self.root, change)

    def action_open_github(self) -> None:
        self.push_screen(GitHubScreen(self.root))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open-github":
            self.action_open_github()
        else:
            self.dismiss(None)


class GitHubScreen(ModalScreen[None]):
    """Local Git/GitHub context; opening a browser is the only external action."""

    CSS = """
    GitHubScreen { align: center middle; }
    #github-dialog { width: 86; max-width: 94%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    #github-info { height: auto; margin: 1 0; }
    #github-buttons { height: auto; align-horizontal: right; }
    #github-buttons Button { margin-left: 1; }
    """

    BINDINGS = [
        ("o", "open_github", "Open"),
        ("escape", "close", "Close"),
    ]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.info = GitHubInfo()

    def compose(self) -> ComposeResult:
        with Vertical(id="github-dialog"):
            yield Label("GIT / GITHUB")
            yield Static("Inspecting repository…", id="github-info")
            with Horizontal(id="github-buttons"):
                yield Button("Open", variant="primary", id="open")
                yield Button("Close", id="close")

    async def on_mount(self) -> None:
        self.run_worker(self._load_info(), exclusive=True)

    async def _load_info(self) -> None:
        # `gh pr view` may consult the network; keep the workspace responsive
        # while GitHub context is being resolved.
        self.info = await asyncio.to_thread(inspect_github, self.root)
        lines = [f"Project: {self.root}", f"Branch: {self.info.branch or '—'}", f"Remote: {self.info.remote or '—'}"]
        if self.info.pull_request_url:
            lines.append(f"Pull request: {self.info.pull_request_title or '—'} [{self.info.pull_request_state}]\n{self.info.pull_request_url}")
        elif self.info.branch_url:
            lines.append(f"Branch URL:\n{self.info.branch_url}")
        else:
            lines.append("No GitHub origin detected. Configure remote.origin.url first.")
        self.query_one("#github-info", Static).update("\n".join(lines))

    def action_open_github(self) -> None:
        url = self.info.pull_request_url or self.info.branch_url or self.info.repository_url
        if open_url(url):
            self.notify("Opened GitHub in the browser", severity="information")
        else:
            self.notify("No GitHub URL available", severity="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open":
            self.action_open_github()
        else:
            self.dismiss(None)


class FollowUpScreen(ModalScreen[str | None]):
    """Start another task without deleting the completed run history."""

    CSS = """
    FollowUpScreen { align: center middle; }
    #follow-up-dialog { width: 86; max-width: 94%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    #follow-up-help { color: $text-muted; margin: 1 0; }
    #follow-up-task { height: 7; }
    #follow-up-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    #follow-up-buttons Button { margin-left: 1; }
    """

    def __init__(self, objective: str) -> None:
        super().__init__()
        self.objective = objective

    def compose(self) -> ComposeResult:
        with Vertical(id="follow-up-dialog"):
            yield Label("NEW FOLLOW-UP TASK")
            yield Static(f"Objective: {self.objective}", id="follow-up-help")
            yield TextArea("", id="follow-up-task")
            with Horizontal(id="follow-up-buttons"):
                yield Button("Start task", variant="primary", id="start")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#follow-up-task", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            task = self.query_one("#follow-up-task", TextArea).text.strip()
            self.dismiss(task or None)
        else:
            self.dismiss(None)


class ReviewerQuestionScreen(ModalScreen[str | None]):
    """Ask Codex a read-only question without entering the implementation loop."""

    CSS = """
    ReviewerQuestionScreen { align: center middle; }
    #reviewer-question-dialog { width: 92; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    #reviewer-question-help { color: $text-muted; margin: 1 0; }
    #reviewer-question { height: 7; }
    #reviewer-question-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    #reviewer-question-buttons Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="reviewer-question-dialog"):
            yield Label("ASK REVIEWER · READ ONLY")
            yield Static("The answer will not change the task, next actor or run status.", id="reviewer-question-help")
            yield TextArea("", id="reviewer-question")
            with Horizontal(id="reviewer-question-buttons"):
                yield Button("Ask", variant="primary", id="ask")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#reviewer-question", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ask":
            question = self.query_one("#reviewer-question", TextArea).text.strip()
            self.dismiss(question or None)
        else:
            self.dismiss(None)


class PlanScreen(ModalScreen[None]):
    """Inspectable ticket DAG and dependency status for planner-mode runs."""

    CSS = """
    PlanScreen { align: center middle; }
    #plan-dialog { width: 108; max-width: 96%; height: 86%; border: thick $accent; background: $surface; padding: 1 2; }
    #plan-objective { height: 3; color: $text-muted; }
    #plan-tickets { height: 1fr; border: round $secondary; }
    #plan-detail { height: 8; border: round $secondary; padding: 1; margin-top: 1; }
    #plan-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    """

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.plan: ExecutionPlan | None = None
        try:
            self.plan = ExecutionPlan.load(root / ".harness" / "plan.json")
        except (OSError, ValueError):
            pass

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-dialog"):
            yield Label("PLANNER DAG · TICKETS")
            yield Static(self.plan.objective if self.plan else "No plan has been generated yet.", id="plan-objective")
            yield ListView(id="plan-tickets")
            yield Static("Select a ticket to inspect its task and dependencies.", id="plan-detail")
            with Horizontal(id="plan-buttons"):
                yield Button("Close", variant="primary", id="close")

    def on_mount(self) -> None:
        view = self.query_one("#plan-tickets", ListView)
        if not self.plan:
            view.append(ListItem(Label("No tickets")))
            return
        for ticket in self.plan.tickets:
            dependencies = ", ".join(ticket.depends_on) or "none"
            view.append(ListItem(Label(f"{ticket.status.upper():10} {ticket.id:8} {ticket.title}\n  depends on: {dependencies}")))
        if self.plan.tickets:
            view.index = 0
            self._show_ticket(self.plan.tickets[0])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "plan-tickets" or not self.plan or event.list_view.index is None:
            return
        if 0 <= event.list_view.index < len(self.plan.tickets):
            self._show_ticket(self.plan.tickets[event.list_view.index])

    def _show_ticket(self, ticket) -> None:
        dependencies = ", ".join(ticket.depends_on) or "none"
        criteria = "; ".join(ticket.acceptance_criteria) or "none recorded"
        source = ticket.source or "local objective"
        self.query_one("#plan-detail", Static).update(
            f"{ticket.id} · {ticket.title}\nStatus: {ticket.status}    Depends on: {dependencies}\n"
            f"Source: {source}\nTask: {ticket.task}\nAcceptance: {criteria}"
        )

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class FullResponseScreen(ModalScreen[None]):
    """Browse complete agent responses stored as local run artifacts."""

    CSS = """
    FullResponseScreen { align: center middle; }
    #full-response-dialog { width: 112; max-width: 96%; height: 92%; border: thick $accent; background: $surface; padding: 1 2; }
    #response-picker { margin: 1 0; }
    #response-image-picker { margin-bottom: 1; }
    #response-image { height: auto; max-height: 40; width: 1fr; display: none; border: round $secondary; margin-bottom: 1; }
    #response-content { height: 1fr; border: round $secondary; }
    #response-path { height: 1; color: $text-muted; }
    #response-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    #response-buttons Button { margin-left: 1; }
    """

    BINDINGS = [
        ("r", "toggle_raw", "Raw JSONL"),
        ("escape", "close", "Close"),
    ]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.artifacts = sorted(
            (root / ".harness" / "artifacts").glob("*.txt"),
            key=lambda path: path.name,
            reverse=True,
        )
        self.current_path: Path | None = None
        self.show_raw = False
        self.current_images: list[Path] = []

    def compose(self) -> ComposeResult:
        options = [(self._label(path), str(path)) for path in self.artifacts]
        initial = options[0][1] if options else Select.NULL
        with Vertical(id="full-response-dialog"):
            yield Label("FULL AGENT RESPONSE")
            yield Select(options, value=initial, allow_blank=True, id="response-picker")
            yield Static("No response artifacts found.", id="response-path")
            yield Label("INLINE IMAGES · KITTY", classes="muted")
            yield Select([], value=Select.NULL, allow_blank=True, id="response-image-picker")
            if TerminalImage is not None:
                yield TerminalImage(id="response-image")
            else:
                yield Static("Image preview unavailable; install textual-image dependencies.", id="response-image")
            yield TextArea(self._display(Path(initial)) if options else "", read_only=True, id="response-content")
            with Horizontal(id="response-buttons"):
                yield Button("Close", variant="primary", id="close")

    @staticmethod
    def _label(path: Path) -> str:
        name = path.name
        if name.startswith("question-"):
            return f"AD-HOC REVIEWER · {name}"
        return name.removesuffix(".txt").replace("-", " · ").upper()

    @staticmethod
    def _read(path: Path) -> str:
        if not path:
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"Unable to read {path}: {exc}"

    def _display(self, path: Path) -> str:
        raw = self._read(path)
        if self.show_raw or raw.startswith("Unable to read"):
            return raw
        # Artifacts contain the exact JSONL emitted by the terminal CLIs. Use
        # the same parsers as the runners so the modal shows the agent's final
        # answer, not protocol/tool events.
        if '"thread.started"' in raw or '"item.started"' in raw:
            response, _ = _parse_codex(raw)
        elif '"stream_event"' in raw or '"type":"result"' in raw:
            response, _ = _parse_claude(raw)
        else:
            response = raw
        try:
            payload = json.loads(response)
        except (TypeError, json.JSONDecodeError):
            return response
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _find_images(self, text: str) -> list[Path]:
        """Find existing local image references in Markdown or plain text."""
        import re

        references = re.findall(
            r"!\[[^\]]*\]\(([^)]+)\)|(?:^|[\s\"'(`])([^\s\"'()`,;:]+\.(?:png|jpe?g|gif|webp|svg))",
            text,
            re.IGNORECASE,
        )
        images: list[Path] = []
        seen: set[Path] = set()
        for markdown_path, plain_path in references:
            value = (markdown_path or plain_path).strip().strip("<>`").rstrip(".,;:")
            if value.startswith(("http://", "https://", "data:")):
                continue
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = self.root / candidate
            candidate = candidate.resolve()
            if not candidate.is_file() and "/" not in value and "\\" not in value:
                # Reviewers often cite only the basename of a generated figure.
                # Resolve that basename within the selected project as a
                # convenience, while skipping runtime and dependency trees.
                for match in self.root.rglob(value):
                    if any(part in {".git", ".harness", ".venv", "node_modules"} for part in match.parts):
                        continue
                    if match.is_file():
                        candidate = match.resolve()
                        break
            if candidate.is_file() and candidate.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"} and candidate not in seen:
                seen.add(candidate)
                images.append(candidate)
        return images

    def on_mount(self) -> None:
        picker = self.query_one("#response-picker", Select)
        if self.artifacts:
            self._show(Path(str(picker.value)))
        self.query_one("#response-content", TextArea).focus()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.value in (Select.NULL, Select.BLANK, None):
            return
        if event.select.id == "response-picker":
            self._show(Path(str(event.value)))
        elif event.select.id == "response-image-picker":
            self._show_image(Path(str(event.value)))

    def _show(self, path: Path) -> None:
        self.current_path = path
        self.query_one("#response-path", Static).update(str(path))
        mode = "RAW JSONL" if self.show_raw else "PARSED RESPONSE"
        self.query_one("#response-path", Static).update(f"{mode}  ·  {path}")
        displayed = self._display(path)
        self.query_one("#response-content", TextArea).text = displayed
        self.current_images = [] if self.show_raw else self._find_images(displayed)
        image_picker = self.query_one("#response-image-picker", Select)
        image_picker.set_options([(image.name, str(image)) for image in self.current_images])
        if self.current_images:
            image_picker.value = str(self.current_images[0])
            self._show_image(self.current_images[0])
        else:
            image_picker.value = Select.NULL
            self._show_image(None)

    def _show_image(self, path: Path | None) -> None:
        image = self.query_one("#response-image")
        image.styles.display = "block" if path else "none"
        if path and TerminalImage is not None:
            image.image = str(path)


    def action_toggle_raw(self) -> None:
        self.show_raw = not self.show_raw
        if self.current_path:
            self._show(self.current_path)

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class WorkspaceApp(App[int]):
    """Single interactive control room for all registered local runs."""

    TITLE = "Agent Harness Workspace"
    CSS = """
    Screen { background: $background; }
    #body { height: 1fr; }
    #sidebar { width: 34; border: round $primary; padding: 1; }
    #main { width: 1fr; height: 1fr; overflow-x: hidden; overflow-y: hidden; }
    #runs { height: 1fr; border: round $secondary; }
    #run-buttons { height: auto; margin-top: 1; }
    #run-buttons Button { width: 1fr; margin-right: 1; }
    #details { height: 14; border: round $secondary; padding: 1; overflow-y: hidden; }
    #changes-summary { height: 2; border: round $secondary; padding: 0 1; color: $accent; }
    #agents { height: 7; }
    .agent-panel { width: 1fr; border: round $primary; padding: 1; margin: 0 1; }
    .agent-title { text-style: bold; color: $accent; }
    #activity-label { height: 1; color: $text-muted; text-style: bold; }
    #activity { height: 1fr; min-height: 6; border: round $secondary; margin: 1 0; }
    .muted { color: $text-muted; }
    """
    BINDINGS = [
        ("n", "new_run", "New run"),
        ("a", "add_run", "Add project"),
        ("e", "edit_run", "Edit"),
        ("u", "follow_up", "Follow-up task"),
        ("w", "ask_reviewer", "Ask reviewer"),
        ("k", "show_plan", "Plan tickets"),
        ("v", "view_response", "Full response"),
        ("s", "save_run", "Save"),
        ("r", "run_selected", "Run"),
        ("c", "continue_flow", "Continue flow"),
        ("p", "pause_run", "Pause"),
        ("d", "decision", "Decision"),
        ("i", "inject", "Inject"),
        ("f", "show_changes", "Files"),
        ("g", "github", "GitHub"),
        ("t", "cycle_theme", "Theme"),
        ("h", "reload_history", "History"),
        ("x", "remove_run", "Remove"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, initial_root: Path | None = None, registry: WorkspaceRegistry | None = None) -> None:
        super().__init__()
        self.registry = registry or WorkspaceRegistry()
        self.initial_root = initial_root
        self.selected_index = 0
        self.active_harness: Harness | None = None
        self._decision_future: asyncio.Future[str] | None = None
        self._stream_buffers: dict[str, str] = {}
        self._stream_times: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Label("RUNS")
                yield ListView(id="runs")
                with Horizontal(id="run-buttons"):
                    yield Button("New", id="new")
                    yield Button("Add", id="add")
                    yield Button("Remove", id="remove")
            with Vertical(id="main"):
                yield Static("No run selected", id="details")
                yield Static("", id="changes-summary")
                with Horizontal(id="agents"):
                    with Vertical(classes="agent-panel"):
                        yield Static("ENGINEER", classes="agent-title", id="engineer-title")
                        yield Static("○ waiting", id="engineer-status")
                    with Vertical(classes="agent-panel"):
                        yield Static("REVIEWER", classes="agent-title", id="reviewer-title")
                        yield Static("○ waiting", id="reviewer-status")
                    with Vertical(classes="agent-panel"):
                        yield Static("PLANNER", classes="agent-title", id="planner-title")
                        yield Static("○ waiting", id="planner-status")
                yield Label("ACTIVITY · live events and persisted history", id="activity-label")
                yield RichLog(id="activity", highlight=False, markup=False, wrap=True, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        if self.registry.theme in self.available_themes:
            self.theme = self.registry.theme
        else:
            self.registry.theme = self.theme
            self.registry.save()
        if self.initial_root and (self.initial_root / ".harness" / "state.json").exists():
            self.registry.add(self.initial_root)
        self._refresh_runs()

    def action_cycle_theme(self) -> None:
        theme_names = list(self.available_themes)
        if not theme_names:
            return
        current = self.theme if self.theme in theme_names else theme_names[0]
        self.theme = theme_names[(theme_names.index(current) + 1) % len(theme_names)]
        self.registry.theme = self.theme
        self.registry.save()
        self.notify(f"Theme saved: {self.theme}", severity="information")

    def _refresh_runs(self) -> None:
        view = self.query_one("#runs", ListView)
        view.clear()
        valid_paths: list[str] = []
        for path_value in self.registry.paths:
            root = Path(path_value)
            state_path = root / ".harness" / "state.json"
            if not state_path.exists():
                continue
            try:
                state = HarnessState.load(state_path)
                valid_paths.append(path_value)
                mode = "PLAN" if state.mode == "plan" else "SINGLE"
                label = f"{state.status.upper():9} {mode:6} {root.name}\n  {state.objective[:32]}"
                view.append(ListItem(Label(label)))
            except (OSError, ValueError):
                continue
        self.registry.paths = valid_paths
        self.registry.save()
        if self.selected_index >= len(valid_paths):
            self.selected_index = max(0, len(valid_paths) - 1)
        if valid_paths:
            view.index = self.selected_index
            self._render_details()
            if self.active_harness is None:
                self._load_activity(Path(valid_paths[self.selected_index]))
        else:
            self.query_one("#details", Static).update(
                "No runs registered. Press n to create one or a to add an existing .harness project."
            )
            self.query_one("#changes-summary", Static).update("")

    def _selected_root(self) -> Path | None:
        if not self.registry.paths or not 0 <= self.selected_index < len(self.registry.paths):
            return None
        return Path(self.registry.paths[self.selected_index])

    def _load_selected(self) -> HarnessState | None:
        root = self._selected_root()
        if root is None:
            return None
        try:
            ensure_harness_dir(root)
            state = HarnessState.load(root / ".harness" / "state.json")
            hydrate_final_summary(root, state)
            return state
        except (OSError, ValueError):
            return None

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Selection events from ChangesScreen also bubble to the App. Only the
        # workspace run list is allowed to change selected_index.
        if event.list_view.id != "runs" or event.list_view.index is None:
            return
        if not 0 <= event.list_view.index < len(self.registry.paths):
            return
        self.selected_index = event.list_view.index
        self._render_details()
        if self.active_harness is None:
            self._load_activity(self._selected_root())

    def _load_activity(self, root: Path | None) -> None:
        """Hydrate the activity panel from persisted JSONL history."""
        log = self.query_one("#activity", RichLog)
        log.clear()
        history: list[dict[str, Any]] = []
        if root is None:
            self._hydrate_agent_status(history)
            return
        path = root / ".harness" / "transcript.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            self._hydrate_agent_status(history)
            return
        # Keep the TUI responsive for very long-lived runs while retaining the
        # complete source of truth in transcript.jsonl and SQLite.
        previous_tool: tuple[str, str] | None = None
        for line in lines[-2000:]:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            history.append(record)
            message = str(record.get("message") or "")
            tool_signature = None
            if record.get("kind") == "agent_output" and message.startswith("[tool]"):
                tool_signature = (str(record.get("role") or ""), message)
                if tool_signature == previous_tool:
                    continue
            previous_tool = tool_signature
            rendered = render_activity_record(record)
            if rendered:
                log.write(rendered)
        self._hydrate_agent_status(history)

    def _hydrate_agent_status(self, history: list[dict[str, Any]]) -> None:
        statuses = {"engineer": "○ waiting", "reviewer": "○ waiting", "planner": "○ waiting"}
        for record in history:
            role = record.get("role")
            if role not in statuses:
                continue
            kind = record.get("kind")
            if kind in {"agent_started", "review_started", "planner_started", "planner_final_started"}:
                statuses[role] = "● working"
            elif kind in {"agent_finished", "review_completed", "plan_created", "ticket_completed", "completed", "planner_final_completed"}:
                statuses[role] = "✓ finished"
            elif kind == "agent":
                statuses[role] = "✗ failed" if record.get("returncode") not in (None, 0) else "✓ finished"
        self.query_one("#engineer-status", Static).update(statuses["engineer"])
        self.query_one("#reviewer-status", Static).update(statuses["reviewer"])
        self.query_one("#planner-status", Static).update(statuses["planner"])

    def _render_details(self) -> None:
        root = self._selected_root()
        state = self._load_selected()
        if root is None or state is None:
            return
        changes = change_summary(collect_changes(root))
        self.query_one("#details", Static).update(render_overview(root, state, changes))
        self.query_one("#changes-summary", Static).update(f"{changes}  ·  f: file details  ·  g: GitHub")
        self.query_one("#engineer-title", Static).update(
            f"ENGINEER · {state.engineer_command} / {state.engineer_model}"
        )
        self.query_one("#reviewer-title", Static).update(
            f"REVIEWER · {state.reviewer_command} / {state.reviewer_model}"
        )
        self.query_one("#planner-title", Static).update(
            f"PLANNER · {state.planner_command} / {state.planner_model}"
        )

    def action_show_changes(self) -> None:
        root = self._selected_root()
        if root is None:
            self.notify("Select a run first", severity="warning")
            return
        self.push_screen(ChangesScreen(root))

    def action_github(self) -> None:
        root = self._selected_root()
        if root is None:
            self.notify("Select a run first", severity="warning")
            return
        self.push_screen(GitHubScreen(root))

    def _editor_values(self, state: HarnessState | None = None, root: Path | None = None) -> dict[str, str]:
        state = state or HarnessState()
        return {
            "root": str(root or ""),
            "objective": state.objective,
            "acceptance": "\n".join(state.acceptance_criteria),
            "task": state.current_task,
            "engineer_command": state.engineer_command,
            "engineer_model": state.engineer_model,
            "engineer_preset": _preset_for(state.engineer_command, state.engineer_model, "engineer"),
            "reviewer_command": state.reviewer_command,
            "reviewer_model": state.reviewer_model,
            "reviewer_preset": _preset_for(state.reviewer_command, state.reviewer_model, "reviewer"),
            "mode": state.mode,
            "planner_command": state.planner_command,
            "planner_model": state.planner_model,
            "parallelism": str(state.parallelism),
            "test_command": state.test_command or "",
            "max_cycles": str(state.max_cycles),
            "max_engineer_turns": str(state.max_engineer_turns),
        }

    def action_new_run(self) -> None:
        self.push_screen(RunEditorScreen(title="NEW RUN"), self._save_editor_result)

    def action_add_run(self) -> None:
        self.push_screen(RunEditorScreen(title="ADD EXISTING PROJECT"), self._save_editor_result)

    def action_edit_run(self) -> None:
        root = self._selected_root()
        state = self._load_selected()
        if root and state:
            self.push_screen(RunEditorScreen(self._editor_values(state, root), "EDIT RUN"), self._save_editor_result)

    def action_save_run(self) -> None:
        self.action_edit_run()

    def _save_editor_result(self, values: dict[str, str] | None) -> None:
        if not values:
            return
        root = Path(values["root"]).expanduser().resolve()
        if not root.is_dir():
            self.notify(f"Project root does not exist: {root}", severity="error")
            return
        try:
            cycles = max(1, int(values["max_cycles"] or "10"))
            max_turns = max(1, int(values["max_engineer_turns"] or "30"))
            parallelism = max(1, int(values.get("parallelism", "1") or "1"))
        except ValueError:
            self.notify("Cycles, turns and parallelism must be positive integers", severity="error")
            return
        harness_dir = ensure_harness_dir(root)
        state_path = harness_dir / "state.json"
        try:
            state = HarnessState.load(state_path) if state_path.exists() else HarnessState(run_id=str(uuid4()))
            was_completed = state.status == "completed"
            state.run_id = state.run_id or str(uuid4())
            state.objective = values["objective"]
            state.acceptance_criteria = [line.strip() for line in values["acceptance"].splitlines() if line.strip()]
            state.current_task = values["task"]
            state.engineer_command = values["engineer_command"] or "claude"
            state.engineer_model = values["engineer_model"] or "opus"
            state.reviewer_command = values["reviewer_command"] or "codex"
            state.reviewer_model = values["reviewer_model"] or "gpt-5.6-sol"
            state.mode = values.get("mode") or "single"
            state.planner_command = values.get("planner_command") or "codex"
            state.planner_model = values.get("planner_model") or "gpt-5.6-sol"
            state.parallelism = parallelism
            state.engineer = f"{state.engineer_command}:{state.engineer_model}"
            state.reviewer = f"{state.reviewer_command}:{state.reviewer_model}"
            state.test_command = values["test_command"] or None
            state.max_cycles = cycles
            state.max_engineer_turns = max_turns
            harness_dir.mkdir(parents=True, exist_ok=True)
            state.save(state_path)
            (harness_dir / "objective.md").write_text(
                f"# Objective\n\n{state.objective}\n\n## Acceptance criteria\n" +
                "\n".join(f"- {item}" for item in state.acceptance_criteria) + "\n", encoding="utf-8"
            )
            from .cli import _write_reviewer_schema
            _write_reviewer_schema(harness_dir)
            write_planner_schema(harness_dir)
            store = SQLiteStore(harness_dir / "harness.db")
            store.save_state(state)
            store.close()
            self.registry.add(root)
            self.selected_index = self.registry.paths.index(str(root))
            if was_completed and values["task"].strip():
                self._prepare_follow_up(root, state, values["task"])
            self._refresh_runs()
            self.notify(f"Saved run: {root.name}", severity="information")
        except (OSError, ValueError) as exc:
            self.notify(f"Could not save run: {exc}", severity="error")

    def action_remove_run(self) -> None:
        root = self._selected_root()
        if root is None or self.active_harness is not None:
            return
        self.registry.remove(root)
        self.selected_index = max(0, self.selected_index - 1)
        self._refresh_runs()

    def action_refresh(self) -> None:
        self.registry.load()
        self._refresh_runs()

    def action_reload_history(self) -> None:
        self._load_activity(self._selected_root())
        self.notify("Persisted run history reloaded", severity="information")

    def action_run_selected(self) -> None:
        if self.active_harness is not None:
            self.notify("A run is already active", severity="warning")
            return
        root = self._selected_root()
        state = self._load_selected()
        if root is None or state is None:
            self.notify("Select or create a run first", severity="warning")
            return
        if state.status == "completed":
            self.notify("Run is completed; press u or edit/save a new task for a follow-up", severity="warning")
            return
        if state.cycle >= state.max_cycles and state.next_actor != "reviewer":
            self.notify("Maximum cycles reached; press c to continue the next flow step", severity="warning")
            return
        self._discard_incompatible_sessions(root, state)
        state.save(root / ".harness" / "state.json")
        store = SQLiteStore(root / ".harness" / "harness.db")
        store.save_state(state)
        store.close()
        if state.mode == "plan":
            write_planner_schema(root / ".harness")
            planner = CodexPlannerRunner(root, state.planner_model, executable=state.planner_command)

            def engineer_factory(ticket_root, ticket):
                if Path(state.engineer_command).name == "codex":
                    return CodexEngineerRunner(ticket_root, state.engineer_model, executable=state.engineer_command)
                return ClaudeRunner(ticket_root, state.engineer_model, state.max_engineer_turns, executable=state.engineer_command)

            def reviewer_factory(ticket_root, ticket):
                return CodexRunner(ticket_root, state.reviewer_model, executable=state.reviewer_command)

            self.active_harness = PlanHarness(
                root, state, planner, engineer_factory, reviewer_factory, decision_provider=self.ask_decision
            )
        else:
            if Path(state.engineer_command).name == "codex":
                engineer = CodexEngineerRunner(root, state.engineer_model, executable=state.engineer_command)
            else:
                engineer = ClaudeRunner(root, state.engineer_model, state.max_engineer_turns, executable=state.engineer_command)
            reviewer = CodexRunner(root, state.reviewer_model, executable=state.reviewer_command)

            async def decision_provider(question: str) -> str:
                return await self.ask_decision(question)

            self.active_harness = Harness(root, state, engineer, reviewer, decision_provider=decision_provider)
        # WorkspaceApp creates Harness dynamically, so it must attach the live
        # listener here. Without this, events are persisted but never reach the
        # activity panel or agent status widgets.
        self.active_harness.bus.subscribe(self._receive_event)
        self._load_activity(root)
        live = render_activity_record({"kind": "turn_started", "message": "Live continuation started"})
        if live:
            self.query_one("#activity", RichLog).write(live)
        self.notify(f"Running {root.name}", severity="information")
        self.run_worker(self._drive(), name="workspace-run", exclusive=True)

    def action_continue_flow(self) -> None:
        """Explicitly authorize one more cycle after a configured stop."""
        state = self._load_selected()
        root = self._selected_root()
        if state is None or root is None:
            self.notify("Select a run first", severity="warning")
            return
        if self.active_harness is not None:
            self.notify("A run is already active", severity="warning")
            return
        if state.status == "completed":
            self.notify("Run is already completed", severity="warning")
            return
        if state.next_actor == "reviewer":
            self.action_run_selected()
            return
        if state.cycle >= state.max_cycles:
            state.max_cycles = state.cycle + 1
        state.run_id = state.run_id or str(uuid4())
        state.status = "working"
        state.open_questions = []
        state.save(root / ".harness" / "state.json")
        store = SQLiteStore(root / ".harness" / "harness.db")
        store.save_state(state)
        store.close()
        self.notify(f"Continuing {state.phase}: {state.next_actor}", severity="information")
        self.action_run_selected()

    def action_follow_up(self) -> None:
        root = self._selected_root()
        state = self._load_selected()
        if root is None or state is None:
            self.notify("Select a run first", severity="warning")
            return
        if self.active_harness is not None:
            self.notify("A run is already active", severity="warning")
            return
        if state.status != "completed":
            self.notify("Follow-up is available after a completed run", severity="information")
            return
        self.push_screen(FollowUpScreen(state.objective), lambda task: self._follow_up_result(task, root))

    def action_ask_reviewer(self) -> None:
        root = self._selected_root()
        state = self._load_selected()
        if root is None or state is None:
            self.notify("Select a run first", severity="warning")
            return
        if self.active_harness is not None:
            self.notify("Pause the active run before asking an ad-hoc reviewer question", severity="warning")
            return
        self.push_screen(ReviewerQuestionScreen(), lambda question: self._reviewer_question_result(question, root))

    def action_view_response(self) -> None:
        root = self._selected_root()
        if root is None:
            self.notify("Select a run first", severity="warning")
            return
        self.push_screen(FullResponseScreen(root))

    def action_show_plan(self) -> None:
        root = self._selected_root()
        if root is None:
            self.notify("Select a run first", severity="warning")
            return
        self.push_screen(PlanScreen(root))

    def _reviewer_question_result(self, question: str | None, root: Path) -> None:
        if not question:
            return
        state = self._load_selected()
        if state is None:
            return
        question_line = render_activity_record({
            "kind": "reviewer_question_started", "role": "reviewer", "message": f"Question: {question}"
        })
        if question_line:
            self.query_one("#activity", RichLog).write(question_line)
        self.run_worker(self._run_reviewer_question(root, state, question), name="reviewer-question", exclusive=True)

    async def _run_reviewer_question(self, root: Path, state: HarnessState, question: str) -> None:
        transcript = Transcript(root / ".harness" / "transcript.jsonl")
        event = HarnessEvent(run_id=state.run_id, turn=state.turn, role="reviewer",
                             kind="reviewer_question_started", message=question)
        transcript.event(event.kind, run_id=event.run_id, turn=event.turn, role=event.role,
                         message=question)
        store = SQLiteStore(root / ".harness" / "harness.db")
        store.record_event(event)
        store.close()
        started = render_activity_record({
            "kind": "reviewer_question_started", "role": "reviewer", "message": "Read-only question started"
        })
        if started:
            self.query_one("#activity", RichLog).write(started)
        evidence = await asyncio.to_thread(collect_evidence, root, state)
        runner = CodexRunner(root, state.reviewer_model, executable=state.reviewer_command)

        async def stream_callback(stream: str, text: str) -> None:
            if text:
                rendered = render_activity_record({
                    "kind": "agent_output", "role": "reviewer", "message": text
                })
                if rendered:
                    self.query_one("#activity", RichLog).write(rendered)

        result = await runner.run_async(
            reviewer_question_prompt(state, question, evidence), state.reviewer_session_id, stream_callback
        )
        artifact = root / ".harness" / "artifacts" / f"question-{state.turn:04d}-{uuid4().hex[:8]}-reviewer.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(result.raw or result.text, encoding="utf-8")
        parsed = parse_review(result.text)
        answer = parsed.summary or result.text.strip()
        if result.error:
            answer = f"Reviewer question failed: {result.error}"
        answer_line = render_activity_record({
            "kind": "reviewer_question_completed", "role": "reviewer", "message": answer
        })
        if answer_line:
            self.query_one("#activity", RichLog).write(answer_line)
        for finding in parsed.findings:
            issue = str(finding.get("issue", "")).strip()
            if issue:
                finding_line = render_activity_record({
                    "kind": "review", "role": "reviewer", "verdict": "FINDING", "severity": parsed.severity,
                    "summary": issue,
                })
                if finding_line:
                    self.query_one("#activity", RichLog).write(finding_line)
        transcript.event(
            "reviewer_question_completed", run_id=state.run_id, turn=state.turn, role="reviewer",
            message=answer, question=question, artifact=str(artifact.relative_to(root)),
            returncode=result.returncode, error=result.error,
        )
        event = HarnessEvent(run_id=state.run_id, turn=state.turn, role="reviewer",
                             kind="reviewer_question_completed", message=answer,
                             data={"question": question, "artifact": str(artifact.relative_to(root)),
                                   "returncode": result.returncode, "error": result.error})
        store = SQLiteStore(root / ".harness" / "harness.db")
        store.record_event(event)
        store.close()

    def _follow_up_result(self, task: str | None, root: Path) -> None:
        if not task:
            return
        state = self._load_selected()
        if state is not None:
            self._prepare_follow_up(root, state, task)

    def _prepare_follow_up(self, root: Path, state: HarnessState, task: str) -> None:
        """Reopen orchestration while retaining transcript, artifacts and sessions."""
        state.current_task = task.strip()
        state.status = "idle"
        state.phase = "implementation"
        state.next_actor = "engineer"
        state.pending_engineer_summary = ""
        state.pending_engineer_turn = None
        state.open_questions = []
        state.last_verdict = None
        state.final_summary = ""
        state.max_cycles = max(state.max_cycles, state.cycle + 10)
        state.save(root / ".harness" / "state.json")
        (root / ".harness" / "final_summary.md").write_text(
            "# Follow-up task in progress\n\n" + state.current_task + "\n", encoding="utf-8"
        )
        store = SQLiteStore(root / ".harness" / "harness.db")
        store.save_state(state)
        store.close()
        from .transcript import Transcript
        Transcript(root / ".harness" / "transcript.jsonl").event(
            "follow_up", run_id=state.run_id, turn=state.turn, message=state.current_task,
            previous_status="completed"
        )
        self._render_details()
        self.notify("Follow-up task prepared; press r to run it", severity="information")

    def _discard_incompatible_sessions(self, root: Path, state: HarnessState) -> None:
        """Do not resume a Claude thread with Codex (or vice versa)."""
        path = root / ".harness" / "transcript.jsonl"
        try:
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            events = []
        for role, command_name, model in (
            ("engineer", state.engineer_command, state.engineer_model),
            ("reviewer", state.reviewer_command, state.reviewer_model),
        ):
            latest = next(
                (event for event in reversed(events)
                 if event.get("kind") == "agent" and event.get("role") == role
                 and event.get("returncode") == 0 and event.get("command")),
                None,
            )
            command = latest.get("command", []) if latest else []
            executable_matches = bool(command) and Path(str(command[0])).name == Path(command_name).name
            model_matches = model in [str(value) for value in command]
            if not executable_matches or not model_matches:
                if role == "engineer":
                    state.engineer_session_id = None
                else:
                    state.reviewer_session_id = None

    async def _drive(self) -> None:
        if self.active_harness is None:
            return
        harness = self.active_harness
        result = await harness.run_async()
        self.active_harness = None
        self._refresh_runs()
        if result == 0:
            self.notify("Objective completed", severity="information")
        else:
            self.notify("Run paused; inspect the activity log", severity="warning")

    def action_pause_run(self) -> None:
        if self.active_harness:
            self.active_harness.request_stop()
            self.notify("Pause requested; current subprocess will finish safely", severity="warning")

    async def _receive_event(self, event: HarnessEvent) -> None:
        self.post_message(HarnessEventMessage(event))

    def on_harness_event_message(self, message: HarnessEventMessage) -> None:
        event = message.event
        log = self.query_one("#activity", RichLog)
        if event.kind == "agent_output" and event.role in {"engineer", "reviewer"}:
            self._handle_stream_output(event, log)
            self._update_agent_status(event)
            return
        if event.role in {"engineer", "reviewer"}:
            self._flush_stream(event.role, log, event.at)
        rendered = render_activity_event(event)
        if rendered:
            log.write(rendered)
        self._update_agent_status(event)
        if event.kind in {"agent_finished", "review_completed", "paused", "completed"}:
            self._render_details()

    def _handle_stream_output(self, event: HarnessEvent, log: RichLog) -> None:
        role = event.role
        if event.message.startswith("["):
            self._flush_stream(role, log, event.at)
            rendered = render_activity_event(event)
            if rendered:
                log.write(rendered)
            return
        if role not in self._stream_buffers:
            self._stream_times[role] = event.at
        self._stream_buffers[role] = self._stream_buffers.get(role, "") + event.message
        if len(self._stream_buffers[role]) >= 100 or event.message.rstrip().endswith((".", "!", "?", ":", ";")):
            self._flush_stream(role, log, event.at)

    def _flush_stream(self, role: str, log: RichLog, timestamp: str) -> None:
        text = self._stream_buffers.pop(role, "").strip()
        started_at = self._stream_times.pop(role, timestamp)
        if text:
            rendered = render_activity_record({
                "at": started_at, "kind": "agent_output", "role": role, "message": text[-4000:]
            })
            if rendered:
                log.write(rendered)

    def _update_agent_status(self, event: HarnessEvent) -> None:
        if event.role not in {"engineer", "reviewer", "planner"}:
            return
        status = self.query_one(f"#{event.role}-status", Static)
        if event.kind in {"agent_started", "review_started", "planner_started", "planner_final_started"}:
            status.update("● working")
        elif event.kind in {"agent_finished", "review_completed", "plan_created", "ticket_completed", "planner_final_completed"}:
            status.update("✓ finished")
        elif event.kind == "paused":
            if "Engineer failed" in event.message and event.role is None:
                self.query_one("#engineer-status", Static).update("✗ failed")
            elif "Reviewer failed" in event.message and event.role is None:
                self.query_one("#reviewer-status", Static).update("✗ failed")
            else:
                status.update("⏸ paused")

    def action_decision(self) -> None:
        question = "Human decision or context for the next turn:"
        if self.active_harness and self.active_harness.state.open_questions:
            question = self.active_harness.state.open_questions[0]
        self.push_screen(DecisionScreen(question, "HUMAN DECISION"), self._context_result)

    def _context_result(self, answer: str | None) -> None:
        if answer and answer != "q":
            self._save_context(answer, "Human decision saved")

    def action_inject(self) -> None:
        self.push_screen(DecisionScreen("Context for the next engineer turn:", "INJECT CONTEXT"), self._context_result)

    def _save_context(self, value: str, message: str) -> None:
        state = self._load_selected()
        root = self._selected_root()
        if state is None or root is None:
            return
        state.current_task = f"Human context: {value.strip()}\nContinue the implementation and verify it."
        state.status = "working"
        state.next_actor = "engineer"
        state.open_questions = []
        state.pending_engineer_summary = ""
        state.pending_engineer_turn = None
        state.save(root / ".harness" / "state.json")
        store = SQLiteStore(root / ".harness" / "harness.db")
        store.save_state(state)
        store.close()
        self._render_details()
        self.notify(message, severity="information")

    async def ask_decision(self, question: str) -> str:
        loop = asyncio.get_running_loop()
        self._decision_future = loop.create_future()
        self.push_screen(DecisionScreen(question), self._decision_result)
        try:
            return await self._decision_future
        finally:
            self._decision_future = None

    def _decision_result(self, answer: str | None) -> None:
        if self._decision_future and not self._decision_future.done():
            self._decision_future.set_result(answer or "q")

    def action_quit(self) -> None:
        if self.active_harness:
            self.active_harness.request_stop()
        self.exit(2)
