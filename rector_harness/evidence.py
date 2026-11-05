from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from .model import HarnessState


MAX_OUTPUT = 40_000


def _run(args: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
    try:
        completed = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, f"command not found: {args[0]}"
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return 124, f"timed out after {timeout}s\n{output}"
    output = (completed.stdout or "")
    if completed.stderr:
        output += f"\n[stderr]\n{completed.stderr}"
    return completed.returncode, output


def collect_evidence(root: Path, state: HarnessState) -> dict[str, str]:
    evidence: dict[str, str] = {
        "objective": state.objective,
        "acceptance_criteria": "\n".join(f"- {item}" for item in state.acceptance_criteria) or "(none recorded)",
        "engineer_task": state.current_task,
    }
    for name, args in {
        "git_status": ["git", "status", "--short", "--branch"],
        "git_diff_stat": ["git", "diff", "--stat"],
        "git_diff": ["git", "diff", "--no-ext-diff", "--unified=3"],
        "untracked_files": ["git", "ls-files", "--others", "--exclude-standard"],
    }.items():
        code, output = _run(args, root)
        evidence[name] = f"exit_code={code}\n{output[-MAX_OUTPUT:]}" if output else f"exit_code={code}\n(empty)"

    if state.test_command:
        args = shlex.split(state.test_command)
        code, output = _run(args, root, timeout=300)
        evidence["tests"] = f"command={state.test_command}\nexit_code={code}\n{output[-MAX_OUTPUT:]}"
    else:
        evidence["tests"] = "No test command configured; reviewer must treat test status as unverified."
    return evidence


def format_evidence(evidence: dict[str, str]) -> str:
    return "\n\n".join(f"## {key}\n{value}" for key, value in evidence.items())
