from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatRequest(StrictModel):
    client_platform: Literal["web", "mobile", "android", "ios"] = "web"
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None

    @field_validator("message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message must not be blank.")
        return value.strip()


class Artifact(StrictModel):
    report_id: UUID
    filename: str
    format: Literal["pdf", "xlsx"]
    download_url: str


class ChartSeries(StrictModel):
    name: str = Field(min_length=1, max_length=250)
    values: list[float | None] = Field(min_length=1, max_length=50)

    @field_validator("values")
    @classmethod
    def finite_values(cls, values):
        import math

        if any(
            value is not None and (not math.isfinite(value) or value < 0)
            for value in values
        ):
            raise ValueError("Invalid chart value")
        return values


class ReportChart(StrictModel):
    subtitle: str = Field(default="", max_length=500)
    type: Literal["pie", "doughnut", "bar", "line", "table"]
    title: str = Field(min_length=1, max_length=250)
    unit: Literal["employee-days", "employees", "hours", "percent", "records", "days"]
    labels: list[str] = Field(min_length=1, max_length=50)
    series: list[ChartSeries] = Field(min_length=1, max_length=20)
    note: str = Field(max_length=1000)

    @model_validator(mode="after")
    def dimensions(self):
        if any(len(label) > 250 for label in self.labels) or any(
            len(series.values) != len(self.labels) for series in self.series
        ):
            raise ValueError("Invalid chart dimensions")
        if self.type in ("pie", "doughnut") and len(self.series) != 1:
            raise ValueError("Pie charts require one series")
        return self


class FollowupSuggestion(StrictModel):
    label: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=500)


class ChatData(StrictModel):
    jobs: list[dict] = Field(default_factory=list, max_length=1)
    version: int = 1
    status: Literal["answered", "clarify", "denied", "unsupported"] = "answered"
    tables: list[dict] = Field(default_factory=list, max_length=3)
    suggestions: list[FollowupSuggestion] = Field(default_factory=list, max_length=3)
    charts: list[ReportChart] = Field(default_factory=list, max_length=3)
    quota: dict | None = None
    conversation_id: UUID
    reply: str
    mode: Literal["demo", "openai"]
    data_source: Literal["fictional_sample", "effortrak_backend"] = "fictional_sample"
    tools_used: list[str]
    artifacts: list[Artifact]


class ChatResponse(StrictModel):
    success: bool = True
    data: ChatData
