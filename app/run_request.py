"""The body of POST /runs, validated against the task registry. A chat run
creates its run through the same model, so both paths refuse the same input."""

from typing import Any

from pydantic import BaseModel, field_validator, model_validator

from app.tasks import CHECK_KINDS, DEFAULT_CHECK_KIND, TASK_TYPES


class RunRequest(BaseModel):
    type: str
    inputs: dict[str, Any]

    @field_validator("type")
    @classmethod
    def type_must_be_registered(cls, value: str) -> str:
        if value not in TASK_TYPES:
            raise ValueError(f"unknown task type {value!r}")
        return value

    @field_validator("inputs")
    @classmethod
    def inputs_must_carry_a_task(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Every registered type stores its input on the same task column for
        # now; per type input shapes are future work, not this step's.
        task = value.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("inputs.task must not be blank")
        return value

    @model_validator(mode="after")
    def only_a_check_has_a_kind(self) -> "RunRequest":
        kind = self.inputs.get("kind")
        if kind is None:
            return self
        if self.type != "site_check":
            raise ValueError("inputs.kind is only for site_check")
        if kind not in CHECK_KINDS:
            raise ValueError(f"unknown check kind {kind!r}")
        return self

    @property
    def check_kind(self) -> str | None:
        if self.type != "site_check":
            return None
        return self.inputs.get("kind", DEFAULT_CHECK_KIND)
