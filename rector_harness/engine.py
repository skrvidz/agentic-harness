from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

from .evidence import collect_evidence
from .events import EventBus, HarnessEvent
from .model import AgentResult, HarnessState, Review
from .prompts import engineer_prompt, reviewer_prompt
from .runner import AgentRunner, _parse_claude
from .runtime import ensure_harness_dir
from .storage import SQLiteStore
from .transcript import Transcript, utc_now


DecisionProvider = Callable[[str], Awaitable[str]]


class Harness:
    """Turn-based orchestrator usable from headless CLI or a Textual worker."""

    def __init__(self, root: Path, state: HarnessState, engineer: AgentRunner, reviewer: AgentRunner,
                 output: Callable[[str], None] = print, bus: EventBus | None = None,
                 store: SQLiteStore | None = None, decision_provider: DecisionProvider | None = None,
                 runtime_dir: Path | None = None) -> None:
        self.root, self.state, self.engineer, self.reviewer, self.output = root, state, engineer, reviewer, output
        self.harness_dir = runtime_dir or ensure_harness_dir(root)
        self.transcript = Transcript(self.harness_dir / "transcript.jsonl")
        self.bus = bus or EventBus()
        self.store = store or SQLiteStore(self.harness_dir / "harness.db")
        self.decision_provider = decision_provider
        self.stop_requested = asyncio.Event()
        self.state.run_id = self.state.run_id or str(uuid4())
        hydrate_final_summary(self.root, self.state)
        self._recover_legacy_pending_review()
        self._save()

    def run(self, max_cycles: int | None = None, non_interactive: bool = False) -> int:
        return asyncio.run(self.run_async(max_cycles, non_interactive))

    async def run_async(self, max_cycles: int | None = None, non_interactive: bool = False) -> int:
        limit = max_cycles or self.state.max_cycles
        self.state.status = "working"
        self._save()
        # A pending reviewer turn must run even when the preceding engineer used
        # the last configured cycle.
        while self.state.cycle < limit or self.state.next_actor == "reviewer":
            if self.stop_requested.is_set():
                await self._pause_async("Paused before starting the next actor turn.")
                return 2
            if self.state.next_actor == "human":
                await self._pause_async(self.state.open_questions[0] if self.state.open_questions else "Human decision required.")
                return 2
            if self.state.next_actor == "reviewer" and self.state.pending_engineer_summary:
                outcome = await self._run_reviewer_turn(non_interactive)
            else:
                outcome = await self._run_engineer_turn()
            if outcome is not None:
                return outcome
        await self._pause_async(f"Maximum cycles reached ({limit}); human review required.")
        return 2

    async def _run_engineer_turn(self) -> int | None:
        self.state.cycle += 1
        self.state.turn += 1
        self.state.next_actor = "engineer"
        self._save()
        await self._emit("turn_started", message=f"Cycle {self.state.cycle} started")
        self._say(f"\n╭─ TURN {self.state.turn} · CYCLE {self.state.cycle} ─────────────────────╮")
        await self._emit("agent_started", role="engineer", message="Engineer turn started")
        result = await self.engineer.run_async(
            engineer_prompt(self.state, self._feedback()), self.state.engineer_session_id,
            self._stream_callback("engineer"))
        self._record_agent("engineer", result)
        self.state.engineer_session_id = result.session_id or self.state.engineer_session_id
        self._say_agent(result)
        if result.error:
            self.state.next_actor = "engineer"
            self._save()
            await self._pause_async(f"Engineer failed: {result.error}")
            return 2
        await self._emit("agent_finished", role="engineer", message=result.text[-1000:],
                         data={"returncode": result.returncode})
        self.state.pending_engineer_summary = result.text
        self.state.pending_engineer_turn = self.state.turn
        self.state.next_actor = "reviewer"
        self.state.turn += 1
        self._save()
        if self.stop_requested.is_set():
            await self._pause_async("Paused after the engineer turn; review was not started.")
            return 2
        return None

    async def _run_reviewer_turn(self, non_interactive: bool) -> int | None:
        evidence = collect_evidence(self.root, self.state)
        await self._emit("review_started", role="reviewer", message="Read-only review started")
        result = await self.reviewer.run_async(
            reviewer_prompt(self.state, self.state.pending_engineer_summary, evidence), self.state.reviewer_session_id,
            self._stream_callback("reviewer"))
        self._record_agent("reviewer", result, evidence=evidence)
        self.state.reviewer_session_id = result.session_id or self.state.reviewer_session_id
        self._say_agent(result)
        if result.error:
            self.state.next_actor = "reviewer"
            self._save()
            await self._pause_async(f"Reviewer failed: {result.error}. Inspect the raw artifact before resuming.")
            return 2
        review = parse_review(result.text)
        self._record_review(review)
        review_message = f"Verdict: {review.verdict}"
        if review.summary:
            review_message += f" · {review.summary}"
        await self._emit("review_completed", role="reviewer", message=review_message,
                         data={"verdict": review.verdict, "severity": review.severity,
                               "summary": review.summary})
        return await self._apply_review(review, non_interactive)

    async def _apply_review(self, review: Review, non_interactive: bool) -> int | None:
        self.state.last_verdict = review.verdict
        if review.verdict == "DONE":
            self.state.final_summary = self._final_summary(review)
            self.state.status, self.state.phase = "completed", "done"
            self.state.current_task = ""
            self.state.pending_engineer_summary = ""
            self.state.pending_engineer_turn = None
            self.state.next_actor = "done"
            self.state.open_questions = []
            (self.harness_dir / "final_summary.md").write_text(
                f"# Final reviewer summary\n\n{self.state.final_summary}\n",
                encoding="utf-8",
            )
            self._save()
            await self._emit("completed", message="Objective completed according to reviewer evidence",
                             data={"summary": self.state.final_summary})
            self._say("✓ DONE · objective completed according to reviewer evidence")
            return 0
        if review.verdict == "APPROVED":
            self.state.current_task = review.next_task or "Choose and implement the next smallest increment toward the objective."
            self.state.phase = "implementation"
            self._finish_review_turn()
            return None
        if review.verdict == "REVISE":
            self.state.current_task = self._feedback_text(review)
            self.state.phase = "revision"
            self._finish_review_turn()
            return None

        question = review.human_question or self._feedback_text(review) or "What decision should the agents use to continue?"
        self.state.pending_engineer_summary = ""
        self.state.pending_engineer_turn = None
        if non_interactive:
            self.state.next_actor = "human"
            self._save()
            await self._pause_async(question)
            return 2
        self._say(f"\n⚠ HUMAN DECISION REQUIRED\n{question}")
        if self.decision_provider:
            answer = (await self.decision_provider(question)).strip()
        else:
            answer = (await asyncio.to_thread(input, "Decision (type your answer, or 'q' to pause): ")).strip()
        if answer.lower() == "q":
            await self._pause_async("Paused by human.")
            return 2
        self.state.open_questions = []
        self.state.current_task = f"Human decision: {answer}\nContinue the implementation and verify it against the objective."
        self.state.status = "working"
        self.state.next_actor = "engineer"
        self._save()
        self.store.record_decision(self.state, question, answer)
        await self._emit("human_decision", message=answer, data={"question": question, "answer": answer})
        return None

    def _finish_review_turn(self) -> None:
        self.state.pending_engineer_summary = ""
        self.state.pending_engineer_turn = None
        self.state.next_actor = "engineer"
        self._save()

    def _recover_legacy_pending_review(self) -> None:
        """Upgrade a V1 paused reviewer failure without discarding its engineer work."""
        if self.state.next_actor != "engineer" or self.state.pending_engineer_summary or self.state.status != "paused":
            return
        transcript_path = self.harness_dir / "transcript.jsonl"
        if not transcript_path.exists():
            return
        try:
            events = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except json.JSONDecodeError:
            return
        if not events or events[-1].get("kind") != "paused" or "Reviewer failed" not in events[-1].get("message", events[-1].get("reason", "")):
            return
        engineer_event = next((event for event in reversed(events) if event.get("kind") == "agent" and event.get("role") == "engineer" and event.get("returncode") == 0), None)
        if not engineer_event:
            return
        summary = engineer_event.get("summary", "")
        if not summary:
            artifact = self.harness_dir / "artifacts" / f"turn-{int(engineer_event.get('turn', 0)):04d}-engineer.txt"
            if artifact.exists():
                summary, _ = _parse_claude(artifact.read_text(encoding="utf-8"))
        if summary:
            self.state.pending_engineer_summary = summary
            self.state.pending_engineer_turn = int(engineer_event.get("turn", 0))
            self.state.next_actor = "reviewer"

    def _feedback(self) -> str:
        path = self.harness_dir / "reviewer_feedback.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def _feedback_text(self, review: Review) -> str:
        lines = [f"Reviewer verdict: {review.verdict} ({review.severity})."]
        if review.summary:
            lines.append(f"Summary: {review.summary}")
        for finding in review.findings:
            lines.append(f"- {finding.get('id', 'finding')}: {finding.get('issue', '')}")
            if finding.get("evidence"):
                lines.append(f"  Evidence: {finding['evidence']}")
            if finding.get("required_change"):
                lines.append(f"  Required change: {finding['required_change']}")
        return "\n".join(lines)

    def _record_agent(self, role: str, result: AgentResult, **extra) -> None:
        artifact = self.harness_dir / "artifacts" / f"turn-{self.state.turn:04d}-{role}.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(result.raw or result.text, encoding="utf-8")
        relative_artifact = str(artifact.relative_to(self.root))
        self.transcript.event("agent", role=role, name=result.agent, turn=self.state.turn,
                              session_id=result.session_id, returncode=result.returncode,
                              duration_seconds=round(result.duration_seconds, 3), command=result.command,
                              summary=result.text, artifact=relative_artifact, **extra)
        self.store.record_agent(self.state, role, result, relative_artifact)

    def _record_review(self, review: Review) -> None:
        (self.harness_dir / "reviewer_feedback.md").write_text(self._feedback_text(review) + "\n", encoding="utf-8")
        self.transcript.event("review", verdict=review.verdict, severity=review.severity,
                              summary=review.summary, findings=review.findings, confidence=review.confidence)
        self.store.record_review(self.state, review)

    def _final_summary(self, review: Review) -> str:
        """Persist a short human-facing conclusion even for older reviewers."""
        summary = review.summary.strip()
        if summary:
            return summary[:240]
        if review.findings:
            issue = str(review.findings[0].get("issue", "")).strip()
            if issue:
                return issue[:240]
        return f"Reviewer closed the objective with verdict {review.verdict}."

    async def _pause_async(self, reason: str) -> None:
        self.state.status = "paused"
        self.state.open_questions = [reason]
        self._save()
        await self._emit("paused", message=reason)
        self._say(f"\n⏸ PAUSED · {reason}")

    def request_stop(self) -> None:
        self.stop_requested.set()

    def _save(self) -> None:
        self.state.updated_at = utc_now()
        self.state.save(self.harness_dir / "state.json")
        self.store.save_state(self.state)

    async def _emit(self, kind: str, role: str | None = None, message: str = "", data: dict | None = None) -> None:
        event = HarnessEvent(kind=kind, run_id=self.state.run_id, turn=self.state.turn, role=role,
                             message=message, data=data or {})
        self.transcript.event(kind, run_id=self.state.run_id, turn=self.state.turn, role=role,
                              message=message, data=event.data)
        self.store.record_event(event)
        await self.bus.publish(event)

    def _stream_callback(self, role: str):
        async def callback(stream: str, text: str) -> None:
            await self._emit("agent_output", role=role, message=text.rstrip(), data={"stream": stream})
        return callback

    def _say(self, message: str) -> None:
        self.output(message)

    def _say_agent(self, result: AgentResult) -> None:
        self._say(f"{result.agent}\n{result.text.strip()[-6000:]}\n")


def parse_review(text: str) -> Review:
    candidates = [text.strip()]
    if "```" in text:
        candidates.extend(part.strip() for part in text.split("```") if part.strip() and not part.strip().startswith("json"))
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                return Review.from_payload(payload, text)
        except json.JSONDecodeError:
            continue
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
            if isinstance(payload, dict) and "verdict" in payload:
                return Review.from_payload(payload, text)
        except json.JSONDecodeError:
            pass
    return Review(verdict="NEEDS_HUMAN", human_input_required=True,
                  human_question="Reviewer did not return parseable JSON. Inspect the raw artifact and decide how to continue.", raw_text=text)


def hydrate_final_summary(root: Path, state: HarnessState) -> bool:
    """Migrate completed pre-summary runs without rerunning their agents."""
    if state.status != "completed" or state.final_summary:
        return False
    harness_dir = root / ".harness"
    summary = ""
    transcript = harness_dir / "transcript.jsonl"
    try:
        events = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        events = []
    for event in reversed(events):
        if event.get("kind") == "review" and str(event.get("summary", "")).strip():
            summary = str(event["summary"]).strip()
            break
    if not summary:
        for event in reversed(events):
            if event.get("kind") != "agent" or event.get("role") != "reviewer":
                continue
            parsed = parse_review(str(event.get("summary", "")))
            if parsed.summary:
                summary = parsed.summary
                break
            if parsed.findings:
                issue = str(parsed.findings[0].get("issue", "")).strip()
                if issue:
                    summary = issue
                    break
    if not summary:
        summary = "Run completed before concise reviewer summaries were persisted."
    state.final_summary = summary[:240]
    state.save(harness_dir / "state.json")
    (harness_dir / "final_summary.md").write_text(
        f"# Final reviewer summary\n\n{state.final_summary}\n", encoding="utf-8"
    )
    return True
