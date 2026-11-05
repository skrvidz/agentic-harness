from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse


@dataclass(frozen=True)
class GitHubInfo:
    remote: str = ""
    repository_url: str = ""
    branch: str = ""
    branch_url: str = ""
    pull_request_url: str = ""
    pull_request_title: str = ""
    pull_request_state: str = ""
    error: str = ""


def _run(args: list[str], root: Path, timeout: int = 10) -> tuple[int, str, str]:
    try:
        result = subprocess.run(args, cwd=root, text=True, capture_output=True, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _https_remote(remote: str) -> str:
    value = remote.strip()
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.split(":", 1)[1]
    elif value.startswith("ssh://git@github.com/"):
        value = "https://github.com/" + value.split("github.com/", 1)[1]
    elif value.startswith("http://"):
        value = "https://" + value[len("http://"):]
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password:
        return ""
    return value.removesuffix(".git").rstrip("/")


def inspect_github(root: Path) -> GitHubInfo:
    _, remote, remote_error = _run(["git", "config", "--get", "remote.origin.url"], root)
    _, branch, branch_error = _run(["git", "branch", "--show-current"], root)
    repository_url = _https_remote(remote)
    branch_url = f"{repository_url}/tree/{quote(branch, safe='')}" if repository_url and branch else ""
    pr_url = pr_title = pr_state = ""
    if repository_url:
        code, output, _ = _run(["gh", "pr", "view", "--json", "url,title,state"], root)
        if code == 0:
            try:
                payload = json.loads(output)
                pr_url = str(payload.get("url", ""))
                pr_title = str(payload.get("title", ""))
                pr_state = str(payload.get("state", ""))
            except json.JSONDecodeError:
                pass
    return GitHubInfo(
        remote=remote,
        repository_url=repository_url,
        branch=branch,
        branch_url=branch_url,
        pull_request_url=pr_url,
        pull_request_title=pr_title,
        pull_request_state=pr_state,
        error=remote_error or branch_error,
    )


def open_url(url: str) -> bool:
    if urlparse(url).hostname != "github.com" or urlparse(url).scheme != "https":
        return False
    try:
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True
