from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Callable

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static

from .changes import change_summary, collect_changes
from .engine import Harness
from .events import HarnessEvent
from .rendering import render_activity_event, render_activity_record, render_overview


class DecisionScreen(ModalScreen[str | None]):
    CSS = """
    DecisionScreen { align: center middle; }
    #dialog { width: 80; max-width: 90%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    #question { margin-bottom: 1; color: $text; }
    #answer { margin-bottom: 1; }
    #buttons { height: auto; align-horizontal: right; }
    Button { margin-left: 1; }
    """

    def __init__(self, question: str, title: str = "HUMAN DECISION REQUIRED") -> None:
        super().__init__()
        self.question, self.title = question, title

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title)
            yield Static(self.question, id="question")
            yield Input(placeholder="Type decision or context", id="answer")
            with Horizontal(id="buttons"):
                yield Button("Submit", variant="primary", id="submit")
                yield Button("Pause", id="pause")

    def on_mount(self) -> None:
        self.query_one("#answer", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value or "q")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.dismiss(self.query_one("#answer", Input).value or "q")
        else:
            self.dismiss("q")


class HarnessEventMessage(Message):
    def __init__(self, event: HarnessEvent) -> None:
        super().__init__()
        self.event = event


class HarnessApp(App[int]):
    """Reactive Textual view over the event bus; it owns no orchestration state."""

    TITLE = "Agent Harness"
    CSS = """
    Screen { background: $background; }
    #agents { height: 1fr; min-height: 12; }
    .agent-panel { width: 1fr; border: round $primary; padding: 1; margin: 0 1; overflow-y: auto; }
    .agent-panel:focus { border: round $accent; }
    .agent-title { text-style: bold; color: $accent; }
    #activity-label { height: 1; color: $text-muted; text-style: bold; }
    #activity { height: 16; border: round $secondary; margin: 1; }
    #objective { height: 12; border: round $secondary; margin: 0 1; padding: 1; }
    #status { height: 1; dock: bottom; color: $text-muted; }
    """
    BINDINGS = [
        ("p", "pause", "Pause"),
        ("d", "decision", "Decision"),
        ("i", "inject", "Inject"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, harness: Harness, max_cycles: int | None = None, non_interactive: bool = False) -> None:
        super().__init__()
        self.harness = harness
        self.max_cycles = max_cycles
        self.non_interactive = non_interactive
        self._decision_future: asyncio.Future[str] | None = None
        self._stream_buffers: dict[str, str] = {}
        self._stream_times: dict[str, str] = {}
        self.harness.bus.subscribe(self._receive_event)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="agents"):
            with Vertical(classes="agent-panel", id="engineer-panel"):
                yield Static("CLAUDE · ENGINEER", classes="agent-title")
                yield Static("○ waiting", id="engineer-status")
                yield Static("", id="engineer-output")
            with Vertical(classes="agent-panel", id="reviewer-panel"):
                yield Static("CODEX · REVIEWER", classes="agent-title")
                yield Static("○ waiting", id="reviewer-status")
                yield Static("", id="reviewer-output")
            with Vertical(classes="agent-panel", id="planner-panel"):
                yield Static("CODEX · PLANNER", classes="agent-title")
                yield Static("○ waiting", id="planner-status")
                yield Static("", id="planner-output")
        yield Label("ACTIVITY · live events", id="activity-label")
        yield RichLog(id="activity", highlight=False, markup=False, wrap=True, max_lines=2000)
        yield Static(self._objective_text(), id="objective")
        yield Static("[p] pause  [d] decision  [i] inject context  [q] quit", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        self.run_worker(self._drive(), name="harness-run", exclusive=True)

    async def _drive(self) -> None:
        if self.harness.state.mode == "plan":
            result = await self.harness.run_async(self.harness.state.parallelism, self.non_interactive)
        else:
            result = await self.harness.run_async(self.max_cycles, self.non_interactive)
        if result == 0:
            self.notify("Objective completed", severity="information")
        elif self.harness.state.status == "paused":
            self.notify("Harness paused", severity="warning")

    async def _receive_event(self, event: HarnessEvent) -> None:
        self.post_message(HarnessEventMessage(event))

    def on_harness_event_message(self, message: HarnessEventMessage) -> None:
        event = message.event
        log = self.query_one("#activity", RichLog)
        if event.kind == "agent_output" and event.role in {"engineer", "reviewer"}:
            self._handle_stream_output(event, log)
            self._update_agent_panel(event)
            return
        if event.role in {"engineer", "reviewer"}:
            self._flush_stream(event.role, log, event.at)
        rendered = render_activity_event(event)
        if rendered:
            log.write(rendered)
        self._update_agent_panel(event)
        if event.kind == "completed":
            self.query_one("#status", Static).update("DONE · objective completed")
        elif event.kind == "paused":
            self.query_one("#status", Static).update(f"PAUSED · {event.message}")

    def _handle_stream_output(self, event: HarnessEvent, log: RichLog) -> None:
        role = event.role
        text = event.message
        if text.startswith("["):
            self._flush_stream(role, log, event.at)
            rendered = render_activity_event(event)
            if rendered:
                log.write(rendered)
            return
        if role not in self._stream_buffers:
            self._stream_times[role] = event.at
        self._stream_buffers[role] = self._stream_buffers.get(role, "") + text
        if len(self._stream_buffers[role]) >= 100 or text.rstrip().endswith((".", "!", "?", ":", ";")):
            self._flush_stream(role, log, event.at)

    def _flush_stream(self, role: str, log: RichLog, timestamp: str) -> None:
        text = self._stream_buffers.pop(role, "").strip()
        started_at = self._stream_times.pop(role, timestamp)
        if text:
            rendered = render_activity_record({
                "at": started_at, "kind": "agent_output", "role": role, "message": text
            })
            if rendered:
                log.write(rendered)

    def _update_agent_panel(self, event: HarnessEvent) -> None:
        if event.role not in {"engineer", "reviewer", "planner"}:
            return
        status = self.query_one(f"#{event.role}-status", Static)
        output = self.query_one(f"#{event.role}-output", Static)
        if event.kind in {"agent_started", "review_started", "planner_started", "planner_final_started"}:
            status.update("● working")
        elif event.kind in {"agent_finished", "review_completed", "plan_created", "ticket_completed", "planner_final_completed"}:
            status.update("✓ finished")
        elif event.kind == "agent_output" and event.message:
            current = self._stream_buffers.get(event.role, "")
            if event.message.startswith("["):
                current = event.message
            output.update(current[-3000:])

    def action_pause(self) -> None:
        self.harness.request_stop()
        self.notify("Pause requested; current process will finish safely.", severity="warning")

    def action_quit(self) -> None:
        self.harness.request_stop()
        self.exit(2)

    def action_decision(self) -> None:
        if self._decision_future is None:
            question = self.harness.state.open_questions[0] if self.harness.state.open_questions else "No pending decision. Add context for the next engineer turn:"
            self.push_screen(DecisionScreen(question), self._manual_decision_result)

    def action_inject(self) -> None:
        self.push_screen(DecisionScreen("Context to inject into the next engineer turn:", "INJECT CONTEXT"), self._inject_result)

    def _manual_decision_result(self, answer: str | None) -> None:
        if answer and answer != "q":
            self.harness.state.current_task = f"Human context: {answer}\nContinue the implementation and verify it."
            self.harness.state.save(self.harness.harness_dir / "state.json")
            self.harness.store.save_state(self.harness.state)
            self.notify("Decision/context saved")

    def _inject_result(self, answer: str | None) -> None:
        if answer and answer != "q":
            self.harness.state.current_task = f"Injected human context: {answer}\nContinue the implementation and verify it."
            self.harness.state.save(self.harness.harness_dir / "state.json")
            self.harness.store.save_state(self.harness.state)
            self.notify("Context saved")

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

    def _objective_text(self) -> str:
        return render_overview(
            self.harness.root,
            self.harness.state,
            change_summary(collect_changes(self.harness.root)),
        )


def _local_time(timestamp: str) -> str:
    try:
        return datetime.fromisoformat(timestamp).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return timestamp[11:19]
