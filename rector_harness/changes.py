from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


HARNESS_RUNTIME_PREFIX = ".harness/"


@dataclass(frozen=True)
class FileChange:
    status: str
    path: str
    additions: int | None = 0
    deletions: int | None = 0
    binary: bool = False
    old_path: str | None = None

    @property
    def label(self) -> str:
        return {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}.get(self.status, self.status)

    @property
    def stats(self) -> str:
        if self.binary:
            return "binary"
        return f"+{self.additions or 0} -{self.deletions or 0}"


def _git(args: list[str], root: Path, timeout: int = 10) -> tuple[int, str]:
    try:
        result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, ""
    return result.returncode, result.stdout or ""


def _parse_status(output: str) -> list[tuple[str, str, str | None]]:
    entries: list[tuple[str, str, str | None]] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:]
        status = "?" if code == "??" else (code[0] if code[0] != " " else code[1])
        old_path = None
        if status == "R" and " -> " in path:
            old_path, path = path.split(" -> ", 1)
        entries.append((status, path, old_path))
    return entries


def _parse_numstat(output: str) -> dict[str, tuple[int | None, int | None, bool]]:
    stats: dict[str, tuple[int | None, int | None, bool]] = {}
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        binary = added == "-" or removed == "-"
        try:
            additions = int(added)
            deletions = int(removed)
        except ValueError:
            additions = deletions = None
        stats[path] = (additions, deletions, binary)
    return stats


def _untracked_stats(root: Path, relative_path: str) -> tuple[int | None, int | None, bool]:
    path = root / relative_path
    try:
        data = path.read_bytes()
    except OSError:
        return None, None, False
    if b"\0" in data:
        return None, None, True
    return len(data.splitlines()), 0, False


def collect_changes(root: Path) -> list[FileChange]:
    """Return the working-tree delta from HEAD, including untracked files."""
    _, status_output = _git(["status", "--short", "--untracked-files=all"], root)
    _, numstat_output = _git(["diff", "--no-ext-diff", "--numstat", "HEAD", "--"], root)
    stats = _parse_numstat(numstat_output)
    changes: list[FileChange] = []
    for status, path, old_path in _parse_status(status_output):
        # Runtime state belongs to the harness, not to the implementation
        # review. It remains available in transcript.jsonl and SQLite.
        if path == ".harness" or path.startswith(HARNESS_RUNTIME_PREFIX):
            continue
        if status == "?":
            additions, deletions, binary = _untracked_stats(root, path)
            status = "A"
        else:
            additions, deletions, binary = stats.get(path, (0, 0, False))
        changes.append(FileChange(status, path, additions, deletions, binary, old_path))
    return changes


def change_totals(changes: list[FileChange]) -> tuple[int, int, int, int, int]:
    added = sum(change.status == "A" for change in changes)
    modified = sum(change.status == "M" for change in changes)
    deleted = sum(change.status == "D" for change in changes)
    renamed = sum(change.status == "R" for change in changes)
    lines = sum((change.additions or 0) + (change.deletions or 0) for change in changes)
    return added, modified, deleted, renamed, lines


def change_summary(changes: list[FileChange]) -> str:
    if not changes:
        return "Changes: clean working tree"
    added, modified, deleted, renamed, lines = change_totals(changes)
    parts = [f"{len(changes)} files", f"{lines} lines"]
    if added:
        parts.append(f"{added} added")
    if modified:
        parts.append(f"{modified} modified")
    if deleted:
        parts.append(f"{deleted} deleted")
    if renamed:
        parts.append(f"{renamed} renamed")
    return "Changes: " + " · ".join(parts)


def file_detail(root: Path, change: FileChange, limit: int = 120_000) -> str:
    """Get a reviewable patch, falling back to content for an untracked file."""
    _, output = _git(["diff", "--no-ext-diff", "--unified=80", "HEAD", "--", change.path], root, timeout=20)
    if output.strip():
        return output[-limit:]
    path = root / change.path
    try:
        data = path.read_bytes()
    except OSError as exc:
        return f"Unable to read {change.path}: {exc}"
    if b"\0" in data:
        return f"{change.path}\n\n(binary file, {len(data)} bytes)"
    text = data.decode("utf-8", errors="replace")
    return f"--- /dev/null\n+++ b/{change.path}\n@@ untracked file @@\n{text}"[-limit:]
