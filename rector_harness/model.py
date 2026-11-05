from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


VERDICTS = {"APPROVED", "REVISE", "NEEDS_HUMAN", "DONE"}
STATUSES = {"idle", "working", "paused", "completed", "blocked"}
TICKET_STATUSES = {"pending", "ready", "running", "completed", "blocked", "failed"}


class PlanTicket(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    title: str
    task: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    status: str = "pending"
    engineer_session_id: str | None = None
    reviewer_session_id: str | None = None
    last_verdict: str | None = None
    summary: str = ""
    error: str = ""
    source: str = ""


class ExecutionPlan(BaseModel):
    schema_version: int = 1
    plan_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    objective: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    status: str = "draft"
    planner_session_id: str | None = None
    tickets: list[PlanTicket] = Field(default_factory=list)
    final_verdict: str | None = None
    final_summary: str = ""
    updated_at: str | None = None

    @classmethod
    def load(cls, path: Path) -> "ExecutionPlan":
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")


class HarnessState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = 2
    run_id: str | None = None
    objective: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    phase: str = "implementation"
    next_actor: str = "engineer"
    pending_engineer_summary: str = ""
    pending_engineer_turn: int | None = None
    turn: int = 0
    cycle: int = 0
    status: str = "idle"
    engineer: str = "claude-opus"
    reviewer: str = "gpt-5.6-sol"
    engineer_command: str = "claude"
    engineer_model: str = "opus"
    reviewer_command: str = "codex"
    reviewer_model: str = "gpt-5.6-sol"
    max_engineer_turns: int = 30
    engineer_session_id: str | None = None
    reviewer_session_id: str | None = None
    current_task: str = ""
    open_questions: list[str] = Field(default_factory=list)
    last_verdict: str | None = None
    final_summary: str = ""
    max_cycles: int = 10
    test_command: str | None = None
    updated_at: str | None = None
    mode: str = "single"
    planner_command: str = "codex"
    planner_model: str = "gpt-5.6-sol"
    planner_session_id: str | None = None
    plan_path: str = ".harness/plan.json"
    parallelism: int = 1
    plan_status: str = "none"

    @classmethod
    def load(cls, path: Path) -> "HarnessState":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()


class AgentResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    agent: str
    text: str
    raw: str = ""
    session_id: str | None = None
    returncode: int = 0
    duration_seconds: float = 0.0
    command: list[str] = Field(default_factory=list)
    error: str | None = None


class Review(BaseModel):
    verdict: str
    severity: str = "none"
    summary: str = ""
    findings: list[dict[str, Any]] = Field(default_factory=list)
    human_input_required: bool = False
    human_question: str = ""
    next_task: str = ""
    confidence: float | None = None
    raw_text: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any], raw_text: str = "") -> "Review":
        verdict = str(payload.get("verdict", "NEEDS_HUMAN")).upper()
        if verdict not in VERDICTS:
            verdict = "NEEDS_HUMAN"
        confidence = payload.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        findings = payload.get("findings", [])
        if not isinstance(findings, list):
            findings = [{"issue": str(findings)}]
        return cls(
            verdict=verdict,
            severity=str(payload.get("severity", "none")),
            summary=str(payload.get("summary", "")).strip(),
            findings=findings,
            human_input_required=bool(payload.get("human_input_required", verdict == "NEEDS_HUMAN")),
            human_question=str(payload.get("human_question", "")),
            next_task=str(payload.get("next_task", "")),
            confidence=confidence,
            raw_text=raw_text,
        )
