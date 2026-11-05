from __future__ import annotations

from pathlib import Path
import re
import shlex
from typing import Any

from rich.text import Text

from .events import HarnessEvent
from .model import HarnessState


ROLE_STYLES = {
    "engineer": "cyan",
    "reviewer": "magenta",
    "system": "yellow",
    "tool": "green",
    "test": "green",
    "human": "bright_yellow",
    "planner": "blue",
}


def _compact(value: Any, limit: int = 180) -> str:
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _shell_script(value: str) -> str:
    """Remove the transport shell from a command shown in the activity feed."""
    try:
        parts = shlex.split(value)
    except ValueError:
        parts = []
    if len(parts) >= 3 and Path(parts[0]).name in {"sh", "bash", "zsh"} and parts[1] in {"-c", "-lc"}:
        return parts[2]
    return value


def _tool_summary(value: str) -> tuple[str, str]:
    """Turn verbose CLI payloads into a glanceable operational description."""
    script = _shell_script(value.strip())
    steps = [part.strip() for part in re.split(r"\n|\s*(?:&&|;)\s*", script) if part.strip()]
    first = steps[0] if steps else script
    try:
        tokens = shlex.split(first)
    except ValueError:
        tokens = first.split()
    executable = Path(tokens[0]).name if tokens else "tool"

    action = "RUN"
    if executable in {"sed", "cat", "head", "tail", "nl"}:
        action = "READ"
    elif executable in {"rg", "grep", "find"}:
        action = "SEARCH"
    elif executable in {"ls", "tree"}:
        action = "LIST"
    elif executable == "test":
        action = "CHECK"
    elif executable in {"pytest", "rspec"} or "pytest" in tokens or "unittest" in tokens:
        action = "TEST"
    elif executable in {"python", "python3"}:
        action = "PYTHON"
    elif executable in {"git", "gh"}:
        action = executable.upper()

    path_pattern = re.compile(r"(?:\.?\.?/)?[\w.@+-]+(?:/[\w.@+\-]+)+(?:\.[\w-]+)?|[\w.@+-]+\.[A-Za-z0-9]+")
    paths: list[str] = []
    for match in path_pattern.findall(script):
        name = Path(match).name
        if name and name not in paths:
            paths.append(name)
        if len(paths) == 3:
            break

    if len(steps) > 1:
        action = "BATCH"
        subject = f"{len(steps)} steps"
    else:
        subject = executable
    if paths:
        subject += " · " + ", ".join(paths[:2])
        if len(paths) > 2:
            subject += f" +{len(paths) - 2}"
    elif "<<" in first:
        subject += " · inline script"
    elif len(tokens) > 1 and len(steps) == 1:
        subject = " ".join(tokens[:3])
    return action, _compact(subject, 120)


def _tool_line(timestamp: str, role: str, value: str) -> Text:
    action, summary = _tool_summary(value)
    text = Text()
    text.append(timestamp, style="dim")
    text.append("  ")
    text.append(f"{role[:3].upper() or 'AGT':<4}", style=f"bold {ROLE_STYLES.get(role, 'white')}")
    text.append("  ")
    text.append(f"{action:<8}", style="bold green")
    text.append("  ")
    text.append(summary, style="dim")
    return text


def render_activity_event(event: HarnessEvent) -> Text | None:
    """Render a live event using the same vocabulary as persisted history."""
    record = event.model_dump()
    data = record.pop("data", {})
    if isinstance(data, dict):
        record.update(data)
    return render_activity_record(record)


def _line(timestamp: str, label: str, message: str = "", role: str = "system") -> Text:
    text = Text()
    text.append(timestamp, style="dim")
    text.append("  ")
    text.append(f"{label:<14}", style=f"bold {ROLE_STYLES.get(role, 'white')}")
    if message:
        text.append("  ")
        text.append(message)
    return text


def render_activity_record(record: dict[str, Any]) -> Text | None:
    nested_data = record.get("data")
    if isinstance(nested_data, dict):
        record = {**record, **nested_data}
    kind = str(record.get("kind", "event"))
    timestamp = str(record.get("at", ""))[11:19] or "--:--:--"
    message = str(record.get("message") or "").strip()
    role = str(record.get("role") or "").lower()
    if kind == "agent_output":
        if not message:
            return None
        if message.startswith("[tool]"):
            return _tool_line(timestamp, role, message.removeprefix("[tool] ").strip())
        # Assistant text is intentionally omitted from the activity stream.
        # It is available in full through the response viewer (v), while the
        # activity panel remains a readable operational timeline.
        return None
    if kind in {"planner_started", "planner_final_started"}:
        return _line(timestamp, "PLANNER", message, "planner")
    if kind == "plan_created":
        ticket_ids = record.get("ticket_ids") or []
        count = len(ticket_ids) if isinstance(ticket_ids, list) else ticket_ids
        return _line(timestamp, "PLANNER", f"PLAN CREATED  {count} tickets", "planner")
    if kind == "ticket_started":
        return _line(timestamp, "PLAN", message, "planner")
    if kind == "ticket_completed":
        return _line(timestamp, "PLAN", message, "planner")
    if kind == "planner_final_completed":
        verdict = str(record.get("verdict") or "").strip()
        return _line(timestamp, "PLANNER", f"FINAL {verdict}  {_compact(message, 220)}".strip(), "planner")
    if kind in {"agent", "agent_finished"}:
        returncode = record.get("returncode")
        state = "FAILED" if returncode not in (None, 0) else "FINISHED"
        if kind == "agent_finished":
            return None
        suffix = "response available · v to open"
        if state == "FAILED":
            suffix = _compact(record.get("error") or "process failed")
        return _line(timestamp, role.upper() or "AGENT", f"{state}  {suffix}", role or "system")
    if kind == "review":
        verdict = str(record.get("verdict") or "—")
        severity = str(record.get("severity") or "none")
        summary = str(record.get("summary") or "").strip()
        text = _line(timestamp, "REVIEW", f"{verdict}  severity={severity}", "reviewer")
        if summary:
            text.append("\n              ")
            text.append(summary)
        return text
    if kind in {"agent_started", "review_started"}:
        label = "REVIEWER" if role == "reviewer" else "ENGINEER" if role == "engineer" else "SYSTEM"
        return _line(timestamp, label, message, role or "system")
    if kind == "review_completed":
        # The preceding `review` record contains the structured verdict and
        # summary. Avoid printing the same decision twice.
        return None
    if kind == "reviewer_question_started":
        return _line(timestamp, "REVIEW Q", message, "reviewer")
    if kind == "reviewer_question_completed":
        return _line(timestamp, "REVIEW Q", message, "reviewer")
    if kind == "human_decision":
        return _line(timestamp, "HUMAN", str(record.get("answer") or message), "human")
    if kind == "follow_up":
        return _line(timestamp, "HUMAN", f"Follow-up: {message}", "human")
    if kind == "completed":
        summary = str((record.get("data") or {}).get("summary") or "")
        label = "PLANNER" if role == "planner" else "SYSTEM"
        text = _line(timestamp, label, "RUN COMPLETED", "planner" if role == "planner" else "system")
        if summary:
            text.append("\n              ")
            text.append(summary)
        return text
    if kind == "paused":
        return _line(timestamp, "SYSTEM", f"PAUSED  {message}", "system")
    if kind == "turn_started":
        return _line(timestamp, "SYSTEM", message, "system")
    if message:
        return _line(timestamp, "SYSTEM", message, "system")
    return _line(timestamp, "SYSTEM", kind, "system")


def render_overview(root: Path, state: HarnessState, changes: str) -> Text:
    objective = _compact(state.objective, 190)
    task = _compact(state.current_task, 190)
    text = Text()
    text.append(f"{root.name}  ", style="bold bright_white")
    text.append(f"{root}\n", style="dim")
    text.append("RUN OVERVIEW\n", style="bold yellow")
    text.append("STATUS ", style="bold")
    text.append(state.status.upper(), style="bold green" if state.status == "completed" else "bold cyan")
    text.append(f"    MODE {state.mode.upper()}    PHASE {state.phase.upper()}    NEXT {state.next_actor.upper()}    CYCLE {state.cycle}/{state.max_cycles}\n")
    if state.mode == "plan":
        text.append("PLANNER   ", style="bold blue")
        text.append(f"{state.planner_command} / {state.planner_model}    PARALLEL {state.parallelism}\n")
    text.append("ENGINEER ", style="bold cyan")
    text.append(f"{state.engineer_command} / {state.engineer_model}    ")
    text.append("REVIEWER ", style="bold magenta")
    text.append(f"{state.reviewer_command} / {state.reviewer_model}\n")
    text.append("OBJECTIVE ", style="bold yellow")
    text.append(f"{objective or '—'}\n")
    text.append("TASK      ", style="bold yellow")
    text.append(f"{task or '—'}\n")
    text.append("ACCEPTANCE  ", style="bold yellow")
    text.append(f"{len(state.acceptance_criteria)} criteria    TESTS {state.test_command or 'not configured'}\n")
    text.append(f"{changes}\n", style="bold green")
    if state.final_summary:
        text.append("FINAL REVIEW SUMMARY\n", style="bold magenta")
        text.append(_compact(state.final_summary, 240))
    return text
