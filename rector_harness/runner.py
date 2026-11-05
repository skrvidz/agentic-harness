from __future__ import annotations

import asyncio
import inspect
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from .model import AgentResult


# Codex emits JSONL events whose command output can contain a large diff or
# source file. asyncio's default StreamReader line limit is only 64 KiB.
SUBPROCESS_STREAM_LIMIT = 16 * 1024 * 1024


class AgentRunner:
    name: str

    def run(self, prompt: str, session_id: str | None = None) -> AgentResult:
        raise NotImplementedError

    async def run_async(self, prompt: str, session_id: str | None = None,
                        on_output: Callable[[str, str], Awaitable[None] | None] | None = None) -> AgentResult:
        return await asyncio.to_thread(self.run, prompt, session_id)


class ClaudeRunner(AgentRunner):
    name = "CLAUDE · ENGINEER"

    def __init__(self, root: Path, model: str = "opus", max_turns: int = 12, timeout: int = 1800,
                 executable: str = "claude") -> None:
        self.root, self.model, self.max_turns, self.timeout, self.executable = root, model, max_turns, timeout, executable

    def _command(self, prompt: str, session_id: str | None = None) -> list[str]:
        command = [self.executable, "-p", "--output-format", "stream-json", "--include-partial-messages",
                   "--verbose", "--model", self.model, "--permission-mode", "acceptEdits",
                   "--max-turns", str(self.max_turns)]
        if session_id:
            command.extend(["--resume", session_id])
        command.append(prompt)
        return command

    def run(self, prompt: str, session_id: str | None = None) -> AgentResult:
        command = self._command(prompt, session_id)
        return _subprocess_result(self.name, command, self.root, self.timeout, _parse_claude)

    async def run_async(self, prompt: str, session_id: str | None = None,
                        on_output: Callable[[str, str], Awaitable[None] | None] | None = None) -> AgentResult:
        return await _async_subprocess_result(self.name, self._command(prompt, session_id), self.root, self.timeout,
                                              _parse_claude, on_output)


class CodexRunner(AgentRunner):
    name = "CODEX · REVIEWER"

    def __init__(self, root: Path, model: str = "gpt-5.6-sol", timeout: int = 1800,
                 executable: str = "codex", schema_path: Path | None = None) -> None:
        self.root, self.model, self.timeout, self.executable = root, model, timeout, executable
        self.schema_path = schema_path or (root / ".harness" / "reviewer_schema.json")

    def _command(self, prompt: str, session_id: str | None = None) -> list[str]:
        schema_path = self.schema_path
        if session_id:
            # A resumed thread inherits the policy used when it was created. Do not
            # add the bypass flag here: the reviewer must remain read-only.
            command = [self.executable, "exec", "resume", session_id, "--json", "--model", self.model, prompt]
        else:
            command = [self.executable, "exec", "--json", "--model", self.model, "--sandbox", "read-only",
                       "--skip-git-repo-check", prompt]
        if schema_path.exists():
            prompt_value = command.pop()
            command.extend(["--output-schema", str(schema_path), prompt_value])
        return command

    def run(self, prompt: str, session_id: str | None = None) -> AgentResult:
        return _subprocess_result(self.name, self._command(prompt, session_id), self.root, self.timeout, _parse_codex)

    async def run_async(self, prompt: str, session_id: str | None = None,
                        on_output: Callable[[str, str], Awaitable[None] | None] | None = None) -> AgentResult:
        return await _async_subprocess_result(self.name, self._command(prompt, session_id), self.root, self.timeout,
                                              _parse_codex, on_output)


class CodexEngineerRunner(CodexRunner):
    """Codex in the write-capable engineer role.

    Codex and Claude have different CLI protocols. Selecting ``codex`` as the
    engineer must not send Claude's ``-p``/``--output-format`` flags to it.
    """

    name = "CODEX · ENGINEER"

    def _command(self, prompt: str, session_id: str | None = None) -> list[str]:
        if session_id:
            return [self.executable, "exec", "resume", session_id, "--json", "--model", self.model, prompt]
        return [self.executable, "exec", "--json", "--model", self.model, "--sandbox", "workspace-write",
                "--skip-git-repo-check", prompt]


class CodexPlannerRunner(CodexRunner):
    """Read-only Codex role used for initial decomposition and final evaluation."""

    name = "CODEX · PLANNER"

    def __init__(self, root: Path, model: str = "gpt-5.6-sol", timeout: int = 1800,
                 executable: str = "codex") -> None:
        super().__init__(root, model, timeout, executable, root / ".harness" / "planner_schema.json")


class DryRunEngineer(AgentRunner):
    name = "CLAUDE · ENGINEER (DRY RUN)"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, prompt: str, session_id: str | None = None) -> AgentResult:
        self.calls += 1
        return AgentResult(agent=self.name, text="Dry run engineer completed the requested increment.\nTests: simulated pass.", session_id=session_id or f"dry-engineer-{self.calls}")


class DryRunReviewer(AgentRunner):
    name = "CODEX · REVIEWER (DRY RUN)"

    def __init__(self, verdict: str = "DONE") -> None:
        self.verdict, self.calls = verdict, 0

    def run(self, prompt: str, session_id: str | None = None) -> AgentResult:
        self.calls += 1
        payload = {"verdict": self.verdict, "severity": "none", "summary": "Dry-run review completed.", "findings": [], "human_input_required": False,
                   "next_task": "", "confidence": 1.0}
        return AgentResult(agent=self.name, text=json.dumps(payload), session_id=session_id or f"dry-reviewer-{self.calls}")


class DryRunPlanner(AgentRunner):
    name = "CODEX · PLANNER (DRY RUN)"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, prompt: str, session_id: str | None = None) -> AgentResult:
        self.calls += 1
        if "TICKET RESULTS:" in prompt:
            text = json.dumps({
                "verdict": "DONE",
                "summary": "Dry-run planner verified all planned tickets.",
                "findings": [],
                "human_input_required": False,
                "human_question": "",
            })
        else:
            text = json.dumps({
                "summary": "Dry-run dependency-aware plan.",
                "tickets": [{
                    "id": "T1",
                    "title": "Implement objective",
                    "task": "Implement and test the requested objective.",
                    "acceptance_criteria": ["Configured tests pass"],
                    "depends_on": [],
                }],
            })
        return AgentResult(agent=self.name, text=text, session_id=session_id or f"dry-planner-{self.calls}")


def _subprocess_result(agent: str, command: list[str], cwd: Path, timeout: int, parser) -> AgentResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)
        raw = (completed.stdout or "")
        if completed.stderr:
            raw += f"\n[stderr]\n{completed.stderr}"
        text, session_id = parser(raw)
        error = None if completed.returncode == 0 else f"process exited with code {completed.returncode}"
        return AgentResult(agent=agent, text=text, raw=raw, session_id=session_id, returncode=completed.returncode,
                           duration_seconds=time.monotonic() - started, command=command, error=error)
    except FileNotFoundError:
        return AgentResult(agent=agent, text="", returncode=127, duration_seconds=time.monotonic() - started,
                           command=command, error=f"command not found: {command[0]}")
    except subprocess.TimeoutExpired as exc:
        raw = _as_text(exc.stdout) + _as_text(exc.stderr)
        return AgentResult(agent=agent, text=raw, raw=raw, returncode=124, duration_seconds=time.monotonic() - started,
                           command=command, error=f"timed out after {timeout}s")


async def _async_subprocess_result(agent: str, command: list[str], cwd: Path, timeout: int, parser,
                                   on_output: Callable[[str, str], Awaitable[None] | None] | None = None) -> AgentResult:
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=SUBPROCESS_STREAM_LIMIT,
        )
    except FileNotFoundError:
        return AgentResult(agent=agent, text="", returncode=127, duration_seconds=time.monotonic() - started,
                           command=command, error=f"command not found: {command[0]}")

    chunks: list[str] = []

    async def consume(stream, label: str) -> None:
        async for line in stream:
            value = _as_text(line)
            chunks.append(value if label == "stdout" else f"[stderr]\n{value}")
            if on_output:
                display = _display_line(agent, label, value)
                if display:
                    result = on_output(label, display)
                    if inspect.isawaitable(result):
                        await result

    try:
        await asyncio.wait_for(asyncio.gather(consume(process.stdout, "stdout"), consume(process.stderr, "stderr")), timeout)
        returncode = await process.wait()
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raw = "".join(chunks)
        return AgentResult(agent=agent, text=raw, raw=raw, returncode=124, duration_seconds=time.monotonic() - started,
                           command=command, error=f"timed out after {timeout}s")
    except ValueError as exc:
        # Keep a malformed/oversized stream from escaping through Textual's
        # worker. The raw partial output is still saved by the orchestrator.
        if process.returncode is None:
            process.kill()
        await process.wait()
        raw = "".join(chunks)
        return AgentResult(agent=agent, text=raw, raw=raw, returncode=1,
                           duration_seconds=time.monotonic() - started, command=command,
                           error=f"stream read failed: {exc}")
    raw = "".join(chunks)
    text, session_id = parser(raw)
    error = None if returncode == 0 else f"process exited with code {returncode}"
    return AgentResult(agent=agent, text=text, raw=raw, session_id=session_id, returncode=returncode,
                       duration_seconds=time.monotonic() - started, command=command, error=error)


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_claude(raw: str) -> tuple[str, str | None]:
    streamed: list[str] = []
    fallback: list[str] = []
    result_text = None
    session_id = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = session_id or event.get("session_id")
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            result_text = event["result"]
        delta_text, assistant_text = _claude_event_text(event)
        streamed.extend(delta_text)
        fallback.extend(assistant_text)
    if result_text:
        return result_text, session_id or _find_session_id(raw)
    if streamed or fallback:
        return "".join(streamed or fallback), session_id or _find_session_id(raw)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return str(data.get("result", data.get("text", raw))), data.get("session_id")
        return str(data), _find_session_id(raw)
    except json.JSONDecodeError:
        return raw, _find_session_id(raw)


def _claude_event_text(event: dict) -> tuple[list[str], list[str]]:
    if event.get("type") == "stream_event":
        inner = event.get("event") or {}
        delta = inner.get("delta") if isinstance(inner, dict) else None
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            return [delta["text"]], []
        return [], []
    if event.get("type") == "assistant":
        message = event.get("message") or {}
        content = message.get("content", []) if isinstance(message, dict) else []
        return [], [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
    return [], []


def _parse_codex(raw: str) -> tuple[str, str | None]:
    session_id = None
    messages: list[str] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread = event.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        session_id = session_id or event.get("thread_id") or thread_id
        item = event.get("item", event)
        if isinstance(item, dict) and item.get("type") in {"agent_message", "assistant_message"}:
            content = item.get("text", item.get("content", ""))
            if isinstance(content, str):
                messages.append(content)
        if event.get("type") == "message" and isinstance(event.get("text"), str):
            messages.append(event["text"])
    return (messages[-1] if messages else raw), session_id or _find_session_id(raw)


def _find_session_id(raw: str) -> str | None:
    match = re.search(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", raw, re.IGNORECASE)
    return match.group(0) if match else None


def _display_line(agent: str, stream: str, line: str) -> str:
    if stream == "stderr":
        return line.rstrip()
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return line.rstrip()
    if agent.startswith("CLAUDE"):
        if event.get("type") == "stream_event":
            inner = event.get("event") or {}
            delta = inner.get("delta") if isinstance(inner, dict) else None
            if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                return delta["text"]
            return ""
        if event.get("type") == "assistant":
            content = (event.get("message") or {}).get("content", [])
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    return f"[tool] {item.get('name', 'unknown')}"
            return ""
        if event.get("type") == "result":
            return "[turn complete]"
        return ""
    item = event.get("item", event)
    if isinstance(item, dict):
        if item.get("type") in {"agent_message", "assistant_message"}:
            content = item.get("text", item.get("content", ""))
            return content if isinstance(content, str) else ""
        if item.get("type") in {"command_execution", "tool_call"}:
            # Codex emits the same command for item.started and item.completed.
            # Show it immediately on start; completion is represented by the
            # subsequent agent/review lifecycle event instead of a duplicate.
            if event.get("type") == "item.completed":
                return ""
            return f"[tool] {item.get('name', item.get('command', item.get('type')))}"
    if event.get("type") in {"turn.started", "turn.completed"}:
        return event["type"]
    return ""


def available_commands() -> Iterable[str]:
    return ("claude", "codex")
