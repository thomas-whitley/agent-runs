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
    TaskType(name="pytest", tools=(), provider="gemini", budget_tokens=50_000, public=True),
    TaskType(name="chat", tools=_CHAT_TOOLS, provider="gemini", budget_tokens=20_000),
    TaskType(name="repo_chore", tools=(), provider="gemini", budget_tokens=50_000),
    TaskType(name="site_check", tools=(), provider=None, budget_tokens=0),
    TaskType(name="digest", tools=(), provider="gemini", budget_tokens=20_000),
)

TASK_TYPES: dict[str, TaskType] = {task_type.name: task_type for task_type in _TYPES}

# The kinds a site_check can be. uptime is plain HTTP, run and closed by the
# scheduler Job in the cloud. The other two need a browser, so only the self
# hosted checks worker runs them, through the claim endpoints.
CHECK_KINDS = ("uptime", "lighthouse", "broken_links")
DEFAULT_CHECK_KIND = "uptime"
SELF_HOSTED_CHECK_KINDS = ("lighthouse", "broken_links")
