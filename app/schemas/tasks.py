from typing import Any, Literal

from pydantic import BaseModel, Field


TaskStatus = Literal["pending", "success", "error"]


class AgentEnvelope(BaseModel):
    book_id: str
    chapter_id: str
    task_id: str
    trace_id: str
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    status: TaskStatus = "pending"
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
