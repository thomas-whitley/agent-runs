"""The task type registry. One row per type, in code, naming its tools, its
default provider, and its token budget. A run's type picks its row here;
nothing about that row comes from MODEL or from the request.

Nothing is added to this table until every row in the README's claims table
for this phase is green, per docs/mercury.md.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskType:
    name: str
    tools: tuple[str, ...]
    provider: str | None
    budget_tokens: int
    public: bool = False


_CHAT_TOOLS = (
    "list_runs",
    "read_run_events",
    "portfolio_status",
    "create_task",
)

_TYPES = (
    TaskType(name="pytest", tools=(), provider="gemini", budget_tokens=50_000),
    TaskType(name="chat", tools=_CHAT_TOOLS, provider="gemini", budget_tokens=20_000),
    TaskType(name="repo_chore", tools=(), provider="gemini", budget_tokens=50_000),
    TaskType(name="site_check", tools=(), provider=None, budget_tokens=0),
    TaskType(name="digest", tools=(), provider="gemini", budget_tokens=20_000),
)

TASK_TYPES: dict[str, TaskType] = {task_type.name: task_type for task_type in _TYPES}

# The kinds a site_check can be. The scheduler Job creates and closes its
# own kinds in the cloud: uptime is plain HTTP against a site, ci_watch reads
# a repo's workflow runs from GitHub, and dependency_audit reads a repo's lock
# files. Neither worker claims them. lighthouse and broken_links need a
# browser, so only the self hosted checks worker runs them, through the claim
# endpoints.
SCHEDULER_CHECK_KINDS = ("uptime", "ci_watch", "dependency_audit")
SELF_HOSTED_CHECK_KINDS = ("lighthouse", "broken_links")
CHECK_KINDS = SCHEDULER_CHECK_KINDS + SELF_HOSTED_CHECK_KINDS
DEFAULT_CHECK_KIND = "uptime"
# The kinds the cloud takes over when no self hosted worker claims them in the
# window. PageSpeed Insights runs Lighthouse but cannot crawl, and the crawl
# lives only in checks/, so broken_links waits for the self hosted worker.
CLOUD_FALLBACK_CHECK_KINDS = ("lighthouse",)
