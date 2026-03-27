"""Shared GitHub URL parsing utilities."""

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

_ACTIONS_RUN_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/runs/(?P<run_id>\d+)"
    r"(?:/job/(?P<job_id>\d+))?"
)
_PR_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")
_ISSUE_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)")


@dataclass
class GitHubURL:
    owner: str
    repo: str
    type: str  # "run", "pr", "issue"
    number: str
    job_id: Optional[str] = None

    @property
    def repo_full(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_github_url(url: str) -> Optional[GitHubURL]:
    """Parse a GitHub URL into structured components. Returns None if unrecognized."""
    parsed = urlparse(url)
    path = parsed.path

    m = _ACTIONS_RUN_RE.match(path)
    if m:
        return GitHubURL(
            owner=m.group("owner"),
            repo=m.group("repo"),
            type="run",
            number=m.group("run_id"),
            job_id=m.group("job_id"),
        )

    m = _PR_RE.match(path)
    if m:
        return GitHubURL(
            owner=m.group("owner"),
            repo=m.group("repo"),
            type="pr",
            number=m.group("number"),
        )

    m = _ISSUE_RE.match(path)
    if m:
        return GitHubURL(
            owner=m.group("owner"),
            repo=m.group("repo"),
            type="issue",
            number=m.group("number"),
        )

    return None
