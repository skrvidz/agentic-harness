from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .engine import Harness, parse_review
from .events import EventBus, HarnessEvent
from .evidence import collect_evidence
from .model import ExecutionPlan, HarnessState, PlanTicket
from .prompts import planner_final_prompt, planner_prompt
from .runner import AgentRunner
from .runtime import ensure_harness_dir
from .storage import SQLiteStore
from .transcript import Transcript


PLANNER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "tickets"],
    "properties": {
        "summary": {"type": "string"},
        "tickets": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "task", "acceptance_criteria", "depends_on"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"},
                    "title": {"type": "string"},
                    "task": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "source": {"type": "string"},
                },
            },
        },
    },
}


def write_planner_schema(harness_dir: Path) -> Path:
    harness_dir.mkdir(parents=True, exist_ok=True)
    path = harness_dir / "planner_schema.json"
    path.write_text(json.dumps(PLANNER_SCHEMA, indent=2) + "\n", encoding="utf-8")
    return path


def _json_candidates(text: str) -> list[str]:
    candidates = [text.strip()]
    if "```" in text:
        candidates.extend(
            part.strip() for part in text.split("```") if part.strip() and not part.strip().startswith(("json", "JSON"))
        )
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            _, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        candidates.append(text[index:index + end])
    return candidates


def parse_plan(text: str, objective: str, acceptance_criteria: list[str]) -> ExecutionPlan:
    payload: dict[str, Any] | None = None
    for candidate in _json_candidates(text):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("tickets"), list):
            payload = value
            break
    if payload is None:
        raise ValueError("Planner did not return a parseable ticket plan")

    tickets: list[PlanTicket] = []
    ids: set[str] = set()
    for item in payload["tickets"]:
        if not isinstance(item, dict):
            raise ValueError("Planner returned a non-object ticket")
        ticket_id = str(item.get("id", "")).strip()
        title = str(item.get("title", "")).strip()
        task = str(item.get("task", "")).strip()
        if not ticket_id or ticket_id in ids or not title or not task:
            raise ValueError("Planner returned a ticket with a missing or duplicate id/title/task")
        ids.add(ticket_id)
        criteria = [str(value).strip() for value in item.get("acceptance_criteria", []) if str(value).strip()]
        tickets.append(PlanTicket(
            id=ticket_id,
            title=title,
            task=task,
            acceptance_criteria=criteria or acceptance_criteria,
            depends_on=[str(value).strip() for value in item.get("depends_on", []) if str(value).strip()],
            source=str(item.get("source", "")).strip(),
        ))
    if not tickets:
        raise ValueError("Planner returned an empty ticket plan")
    for ticket in tickets:
        unknown = set(ticket.depends_on) - ids
        if unknown:
            raise ValueError(f"Ticket {ticket.id} depends on unknown ticket(s): {', '.join(sorted(unknown))}")

    graph = {ticket.id: ticket.depends_on for ticket in tickets}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(ticket_id: str) -> None:
        if ticket_id in visiting:
            raise ValueError("Planner returned a cyclic ticket dependency")
        if ticket_id in visited:
            return
        visiting.add(ticket_id)
        for dependency in graph[ticket_id]:
            visit(dependency)
        visiting.remove(ticket_id)
        visited.add(ticket_id)

    for ticket_id in graph:
        visit(ticket_id)
    return ExecutionPlan(
        plan_id=str(uuid4()),
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        status="ready",
        tickets=tickets,
    )


def _git(args: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, ""
    return result.returncode, (result.stdout or "") + (f"\n{result.stderr}" if result.stderr else "")


class TicketWorkspace:
    """Create isolated worktrees for clean Git projects; preserve dirty roots safely."""

    def __init__(self, root: Path, plan_id: str, ticket_id: str) -> None:
        self.root = root
        self.branch = f"harness/{plan_id[:8]}/{ticket_id}"
        self.path = root.parent / f".{root.name}-harness-{plan_id[:8]}-{ticket_id}"
        self.isolated = False

    def create(self, require_isolation: bool = False) -> Path:
        code, status = _git(["status", "--porcelain"], self.root)
        if code != 0 or status.strip():
            if require_isolation:
                raise RuntimeError("Parallel tickets require a clean Git working tree")
            return self.root
        if self.path.exists():
            code, output = _git(["worktree", "list", "--porcelain"], self.root)
            expected_path = f"worktree {self.path.resolve()}"
            expected_branch = f"branch refs/heads/{self.branch}"
            registered = any(
                expected_path in block.splitlines() and expected_branch in block.splitlines()
                for block in output.split("\n\n")
            )
            if code != 0 or not registered:
                raise RuntimeError(f"Existing path is not the expected ticket worktree: {self.path}")
            self.isolated = True
            return self.path
        code, _ = _git(["worktree", "add", "-b", self.branch, str(self.path), "HEAD"], self.root)
        if code == 0:
            self.isolated = True
            return self.path
        if require_isolation:
            raise RuntimeError(f"Could not create ticket worktree: {self.path}")
        return self.root

    def integrate(self) -> tuple[bool, str]:
        if not self.isolated:
            return True, ""
        code, _ = _git(["add", "-A"], self.path)
        if code != 0:
            return False, "could not stage ticket worktree"
        code, output = _git(["commit", "-m", f"harness: complete {self.branch.rsplit('/', 1)[-1]}"], self.path)
        if code != 0 and "nothing to commit" not in output.lower():
            return False, output.strip() or "could not commit ticket worktree"
        if code == 0:
            code, output = _git(["merge", "--no-edit", "--no-ff", self.branch], self.root)
            if code != 0:
                return False, output.strip() or "ticket merge conflict"
        _git(["worktree", "remove", "--force", str(self.path)], self.root)
        _git(["branch", "-D", self.branch], self.root)
        return True, ""


class PlanHarness:
    """Planner-driven DAG orchestration over isolated Engineer -> Reviewer ticket loops."""

    def __init__(
        self,
        root: Path,
        state: HarnessState,
        planner: AgentRunner,
        engineer_factory: Callable[[Path, PlanTicket], AgentRunner],
        reviewer_factory: Callable[[Path, PlanTicket], AgentRunner],
        output: Callable[[str], None] = print,
        bus: EventBus | None = None,
        decision_provider: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self.root, self.state, self.planner = root, state, planner
        self.engineer_factory, self.reviewer_factory = engineer_factory, reviewer_factory
        self.output, self.bus = output, bus or EventBus()
        self.decision_provider = decision_provider
        self.harness_dir = ensure_harness_dir(root)
        self.plan_path = (root / state.plan_path).resolve()
        if not self.plan_path.is_relative_to(self.harness_dir.resolve()):
            raise ValueError("Plan path must stay inside .harness")
        self.transcript = Transcript(self.harness_dir / "transcript.jsonl")
        self.store = SQLiteStore(self.harness_dir / "harness.db")
        self.plan: ExecutionPlan | None = None
        self.stop_requested = asyncio.Event()

    def run(self, parallelism: int | None = None, non_interactive: bool = False) -> int:
        return asyncio.run(self.run_async(parallelism, non_interactive))

    async def run_async(self, parallelism: int | None = None, non_interactive: bool = False) -> int:
        self.state.mode = "plan"
        self.state.plan_status = "working"
        self.state.status = "working"
        self.state.next_actor = "planner"
        self._save_state()
        if not await self._ensure_plan():
            return 2
        assert self.plan is not None
        width = max(1, parallelism or self.state.parallelism)
        if width > 1:
            code, status = _git(["status", "--porcelain"], self.root)
            if code != 0:
                width = 1
            elif status.strip():
                await self._pause("Parallel tickets require a clean Git working tree before worktree isolation.")
                return 2
        while True:
            if self.stop_requested.is_set():
                await self._pause("Plan paused by human.")
                return 2
            pending = [ticket for ticket in self.plan.tickets if ticket.status in {"pending", "ready", "running"}]
            if not pending:
                break
            ready = [ticket for ticket in self.plan.tickets if self._is_ready(ticket)]
            if not ready:
                await self._pause("Plan is blocked by an invalid or incomplete dependency graph.")
                return 2
            batch = ready[:width]
            for ticket in batch:
                ticket.status = "running"
            self.state.next_actor = "tickets"
            self._save_plan()
            results = await asyncio.gather(*(self._run_ticket(ticket, non_interactive, width > 1) for ticket in batch))
            if any(not ok for ok in results):
                await self._pause("One or more tickets failed; inspect the ticket artifacts before resuming.")
                return 2
        return await self._run_final_evaluation(non_interactive)

    async def _ensure_plan(self) -> bool:
        try:
            self.plan = ExecutionPlan.load(self.plan_path) if self.plan_path.exists() else None
        except (OSError, ValueError) as exc:
            await self._pause(f"Could not load plan: {exc}")
            return False
        if self.plan and self.plan.status in {"ready", "running", "paused"}:
            self.plan.status = "running"
            self.state.plan_status = "running"
            for ticket in self.plan.tickets:
                if ticket.status == "running":
                    ticket.status = "pending"
            self._save_plan()
            return True
        evidence = await asyncio.to_thread(collect_evidence, self.root, self.state)
        await self._emit("planner_started", role="planner", message="Creating dependency-aware ticket plan")
        result = await self.planner.run_async(planner_prompt(self.state, evidence), self.state.planner_session_id,
                                              self._stream_callback("planner"))
        self.state.planner_session_id = result.session_id or self.state.planner_session_id
        self._save_state()
        artifact = self.harness_dir / "artifacts" / "plan-planner.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(result.raw or result.text, encoding="utf-8")
        if result.error:
            await self._pause(f"Planner failed: {result.error}")
            return False
        try:
            self.plan = parse_plan(result.text, self.state.objective, self.state.acceptance_criteria)
        except ValueError as exc:
            await self._pause(str(exc))
            return False
        self.plan.planner_session_id = self.state.planner_session_id
        self.plan.status = "running"
        self.state.plan_status = "running"
        self._save_plan()
        await self._emit("plan_created", role="planner", message=f"Created {len(self.plan.tickets)} tickets",
                          data={"ticket_ids": [ticket.id for ticket in self.plan.tickets]})
        return True

    def _is_ready(self, ticket: PlanTicket) -> bool:
        if ticket.status not in {"pending", "ready", "running"}:
            return False
        completed = {item.id for item in self.plan.tickets if item.status == "completed"} if self.plan else set()
        if ticket.status == "running":
            return False
        return all(dependency in completed for dependency in ticket.depends_on)

    async def _run_ticket(self, ticket: PlanTicket, non_interactive: bool, require_isolation: bool = False) -> bool:
        assert self.plan is not None
        manager = TicketWorkspace(self.root, self.plan.plan_id, ticket.id)
        runtime_dir = self.harness_dir / "tickets" / ticket.id
        if not runtime_dir.resolve().is_relative_to(self.harness_dir.resolve() / "tickets"):
            ticket.status = "failed"
            ticket.error = "Ticket runtime path escapes the harness directory"
            self._save_plan()
            return False
        try:
            ticket_root = await asyncio.to_thread(manager.create, require_isolation)
        except RuntimeError as exc:
            ticket.status = "failed"
            ticket.error = str(exc)
            self._save_plan()
            return False
        ticket_state = self.state.model_copy(update={
            "run_id": f"{self.state.run_id}:{ticket.id}",
            "objective": self.state.objective,
            "acceptance_criteria": ticket.acceptance_criteria,
            "current_task": f"[{ticket.id}] {ticket.title}\n{ticket.task}",
            "status": "idle",
            "phase": "implementation",
            "next_actor": "engineer",
            "cycle": 0,
            "turn": 0,
            "pending_engineer_summary": "",
            "pending_engineer_turn": None,
            "open_questions": [],
            "last_verdict": None,
            "final_summary": "",
            "engineer_session_id": ticket.engineer_session_id,
            "reviewer_session_id": ticket.reviewer_session_id,
        })
        ticket_harness = Harness(
            ticket_root,
            ticket_state,
            self.engineer_factory(ticket_root, ticket),
            self.reviewer_factory(ticket_root, ticket),
            output=self.output,
            decision_provider=self.decision_provider,
            bus=EventBus(),
            runtime_dir=runtime_dir,
        )

        async def forward(event: HarnessEvent) -> None:
            data = {**event.data, "ticket_id": ticket.id}
            forwarded = event.model_copy(update={"data": data})
            self.transcript.event(event.kind, run_id=self.state.run_id, turn=event.turn, role=event.role,
                                  message=event.message, data=data)
            self.store.record_event(forwarded)
            await self.bus.publish(forwarded)

        ticket_harness.bus.subscribe(forward)
        await self._emit("ticket_started", role="planner", message=f"Ticket {ticket.id}: {ticket.title}",
                          data={"ticket_id": ticket.id, "depends_on": ticket.depends_on})
        result = await ticket_harness.run_async(non_interactive=non_interactive)
        ticket.engineer_session_id = ticket_state.engineer_session_id
        ticket.reviewer_session_id = ticket_state.reviewer_session_id
        ticket.last_verdict = ticket_state.last_verdict
        ticket.summary = ticket_state.final_summary or ticket_state.open_questions[0] if ticket_state.open_questions else ticket_state.final_summary
        if result != 0 or ticket_state.status != "completed":
            ticket.status = "failed"
            ticket.error = ticket_state.open_questions[0] if ticket_state.open_questions else "ticket did not complete"
            self._save_plan()
            return False
        integrated, error = await asyncio.to_thread(manager.integrate)
        if not integrated:
            ticket.status = "failed"
            ticket.error = error
            self._save_plan()
            return False
        ticket.status = "completed"
        self._save_plan()
        await self._emit("review", role="reviewer", message=f"Ticket {ticket.id} review",
                          data={"ticket_id": ticket.id, "verdict": ticket.last_verdict or "DONE",
                                "severity": "none", "summary": ticket.summary})
        await self._emit("ticket_completed", role="reviewer", message=f"Ticket {ticket.id} completed",
                          data={"ticket_id": ticket.id, "summary": ticket.summary})
        return True

    async def _run_final_evaluation(self, non_interactive: bool) -> int:
        assert self.plan is not None
        self.state.next_actor = "planner"
        results = "\n".join(
            f"{ticket.id} [{ticket.status}] {ticket.title}: {ticket.summary or ticket.error}"
            for ticket in self.plan.tickets
        )
        evidence = await asyncio.to_thread(collect_evidence, self.root, self.state)
        await self._emit("planner_final_started", role="planner", message="Evaluating integrated ticket plan")
        result = await self.planner.run_async(
            planner_final_prompt(self.state, self.plan.model_dump_json(indent=2), results, evidence),
            self.state.planner_session_id,
            self._stream_callback("planner"),
        )
        self.state.planner_session_id = result.session_id or self.state.planner_session_id
        if result.error:
            await self._pause(f"Final planner evaluation failed: {result.error}")
            return 2
        review = parse_review(result.text)
        self.plan.final_verdict = review.verdict
        self.plan.final_summary = review.summary or result.text.strip()[:240]
        self.plan.status = "completed" if review.verdict == "DONE" else "paused"
        self._save_plan()
        self.state.final_summary = self.plan.final_summary
        if review.verdict == "DONE":
            self.state.plan_status = "completed"
            self.state.status = "completed"
            self.state.phase = "done"
            self.state.next_actor = "done"
            self.state.open_questions = []
            self._save_state()
            await self._emit("completed", role="planner", message="Plan completed by final planner evaluation",
                              data={"summary": self.state.final_summary, "verdict": review.verdict})
            return 0
        question = review.human_question or review.summary or "Final planner evaluation requires follow-up work."
        self.state.status = "paused"
        self.state.plan_status = "paused"
        self.state.open_questions = [question]
        self._save_state()
        await self._emit("planner_final_completed", role="planner", message=question,
                          data={"verdict": review.verdict, "summary": review.summary})
        return 2

    def _stream_callback(self, role: str):
        async def callback(stream: str, text: str) -> None:
            if text:
                await self._emit("agent_output", role=role, message=text.rstrip(), data={"stream": stream})
        return callback

    async def _emit(self, kind: str, role: str | None = None, message: str = "", data: dict[str, Any] | None = None) -> None:
        event = HarnessEvent(kind=kind, run_id=self.state.run_id, turn=self.state.turn, role=role,
                             message=message, data=data or {})
        self.transcript.event(kind, run_id=event.run_id, turn=event.turn, role=role, message=message, data=event.data)
        self.store.record_event(event)
        await self.bus.publish(event)

    async def _pause(self, reason: str) -> None:
        self.state.status = "paused"
        self.state.open_questions = [reason]
        self._save_state()
        await self._emit("paused", role="planner", message=reason)

    def _save_state(self) -> None:
        self.state.save(self.harness_dir / "state.json")
        self.store.save_state(self.state)

    def _save_plan(self) -> None:
        if self.plan is not None:
            self.plan.save(self.plan_path)

    def request_stop(self) -> None:
        self.stop_requested.set()
