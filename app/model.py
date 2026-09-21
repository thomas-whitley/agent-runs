"""The model the loop calls, and the stub that stands in for it.

CI and the tests use the stub, so a push costs nothing and needs no key.
"""

import re
from dataclasses import dataclass, field
from typing import Protocol

_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class ModelReply:
    text: str
    tokens: int


class Model(Protocol):
    def complete(self, system: str, prompt: str) -> ModelReply: ...


def extract_code(text: str) -> str:
    """The code inside the first fenced block, or the whole reply if there is none."""
    match = _FENCE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


@dataclass
class StubModel:
    """Deterministic and offline. The last reply repeats once the script runs out."""

    replies: list[str]
    tokens_per_reply: int = 100
    prompts: list[str] = field(default_factory=list)
    _calls: int = 0

    def complete(self, system: str, prompt: str) -> ModelReply:
        self.prompts.append(prompt)
        index = min(self._calls, len(self.replies) - 1)
        self._calls += 1
        return ModelReply(text=self.replies[index], tokens=self.tokens_per_reply)


class AnthropicModel:
    """Claude through the Anthropic SDK. Imported lazily so the stub path needs no SDK."""

    def __init__(self, model: str, api_key: str, max_tokens: int = 2048) -> None:
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def complete(self, system: str, prompt: str) -> ModelReply:
        message = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        tokens = message.usage.input_tokens + message.usage.output_tokens
        return ModelReply(text=text, tokens=tokens)
