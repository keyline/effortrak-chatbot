from typing import Literal
from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ChatRequest(StrictModel):
    client_platform: Literal['web', 'mobile', 'android', 'ios'] = 'web'
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None

    @field_validator('message')
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('Message must not be blank.')
        return value.strip()


class AttendanceArgs(StrictModel):
    scope: Literal['self', 'team', 'company']
    employee_id: int | None = Field(default=None, ge=1)
    month: str = Field(pattern=r'^20\d{2}-(0[1-9]|1[0-2])$')


class ExportArgs(StrictModel):
    scope: Literal['self', 'team', 'company']
    employee_id: int | None = Field(default=None, ge=1)
    month: str | None = Field(default=None, pattern=r'^20\d{2}-(0[1-9]|1[0-2])$')
    format: Literal['pdf', 'xlsx']
    report_type: Literal['today', 'last_7_days', 'monthly_details', 'custom_range'] | None = None
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode='after')
    def valid_period(self):
        if self.report_type in (None, 'monthly_details'):
            if not self.month or self.start_date or self.end_date:
                raise ValueError('A month is required for monthly exports.')
        elif self.report_type == 'custom_range':
            if not self.start_date or not self.end_date or self.month:
                raise ValueError('A custom range requires both dates and no month.')
            if not 0 <= (self.end_date - self.start_date).days <= 92:
                raise ValueError('Select an inclusive range of 1 to 93 days.')
        elif self.month or self.start_date or self.end_date:
            raise ValueError('Relative reports use the server date, not supplied dates.')
        return self


class EmployeeCountArgs(StrictModel):
    scope: Literal['self', 'team', 'company']


class DailyAttendanceArgs(EmployeeCountArgs):
    date: date
    employee_id: int | None = Field(default=None, ge=1)


class WorkReportArgs(EmployeeCountArgs):
    employee_id: int | None = Field(default=None, ge=1)
    start_date: date
    end_date: date
    detailed_review: bool = False
    include_daily: bool = False

    @model_validator(mode='after')
    def bounded_period(self):
        if not 0 <= (self.end_date - self.start_date).days <= 30:
            raise ValueError('Work reports support up to 31 inclusive days.')
        return self


class TaskSummaryArgs(AttendanceArgs):
    pass


class DailyTaskArgs(DailyAttendanceArgs):
    page: int = Field(default=1, ge=1, le=10000)


class EmployeeTaskHistoryArgs(EmployeeCountArgs):
    employee_id: int | None = Field(default=None, ge=1)
    start_date: date | None = None
    end_date: date | None = None
    latest_limit: int | None = Field(default=None, ge=1, le=50)
    page: int = Field(default=1, ge=1, le=10000)

    @model_validator(mode='after')
    def valid_range(self):
        if self.start_date is None and self.end_date is None and self.latest_limit is not None:
            return self
        if self.start_date is None or self.end_date is None:
            raise ValueError('Select both range endpoints or a latest-task limit.')
        if not 0 <= (self.end_date - self.start_date).days <= 92:
            raise ValueError('Select an inclusive range of 1 to 93 days.')
        return self


class ProjectTaskArgs(EmployeeTaskHistoryArgs):
    project_id: int = Field(ge=1)


class ManagerReportArgs(EmployeeCountArgs):
    report: Literal['late', 'late_punchin', 'missing_punchout', 'present', 'absent', 'work_arrangements', 'locations',
                    'missing_location', 'effort_by_employee', 'effort_by_project', 'task_overruns', 'workload', 'approved_leave', 'attendance_totals', 'booked_time']
    employee_id: int | None = Field(default=None, ge=1)
    project_id: int | None = Field(default=None, ge=1)
    start_date: date | None = None
    end_date: date | None = None
    threshold: int = Field(default=0, ge=0, le=100000)

    @model_validator(mode='after')
    def valid_period(self):
        if self.project_id is not None and self.report != 'booked_time':
            raise ValueError('Project filters are supported for booked effort only.')
        if self.report == 'work_arrangements':
            if self.start_date or self.end_date:
                raise ValueError('Work arrangements are current configuration, not historical daily selections.')
        elif not self.start_date or not self.end_date or not 0 <= (self.end_date - self.start_date).days <= 92:
            raise ValueError('Select an inclusive range of 1 to 93 days.')
        return self


class Artifact(StrictModel):
    report_id: UUID
    filename: str
    format: Literal['pdf', 'xlsx']
    download_url: str


class ChartSeries(StrictModel):
    name: str = Field(min_length=1, max_length=250)
    values: list[float | None] = Field(min_length=1, max_length=50)

    @field_validator('values')
    @classmethod
    def finite_values(cls, values):
        import math
        if any(value is not None and (not math.isfinite(value) or value < 0) for value in values):
            raise ValueError('Invalid chart value')
        return values


class ReportChart(StrictModel):
    subtitle: str = Field(default='', max_length=500)
    type: Literal['pie', 'doughnut', 'bar', 'line', 'table']
    title: str = Field(min_length=1, max_length=250)
    unit: Literal['employee-days', 'employees', 'hours', 'percent']
    labels: list[str] = Field(min_length=1, max_length=50)
    series: list[ChartSeries] = Field(min_length=1, max_length=20)
    note: str = Field(max_length=1000)

    @model_validator(mode='after')
    def dimensions(self):
        if any(len(label) > 250 for label in self.labels) or any(len(series.values) != len(self.labels) for series in self.series):
            raise ValueError('Invalid chart dimensions')
        if self.type in ('pie', 'doughnut') and len(self.series) != 1:
            raise ValueError('Pie charts require one series')
        return self


class ChatData(StrictModel):
    charts: list[ReportChart] = Field(default_factory=list, max_length=3)
    quota: dict | None = None
    conversation_id: UUID
    reply: str
    mode: Literal['demo', 'openai']
    data_source: Literal['fictional_sample', 'effortrak_backend'] = 'fictional_sample'
    tools_used: list[str]
    artifacts: list[Artifact]


class ChatResponse(StrictModel):
    success: bool = True
    data: ChatData
