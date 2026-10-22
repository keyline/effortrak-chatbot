"""Keep work metrics and screenshot metadata local; only intent goes to OpenAI."""

import re
from datetime import date

from pydantic import Field, model_validator

from app.schemas import StrictModel
from app.task_report import AssistantReply, TaskResult
from app.attendance_report import duration


def wants_review(message):
    analysis = bool(re.search(r'\b(?:analy[sz]\w*|review|trend\w*)\b', message, re.I))
    expanded = bool(re.search(r'\b(?:detail\w*|breakdown|full|in[- ]depth)\b', message, re.I))
    history = bool(re.search(r'\b(?:30\s*[- ]?days?|month(?:ly)?|histor\w*)\b', message, re.I))
    return history and (analysis or expanded) or analysis and expanded


class WorkTotals(StrictModel):
    days: int = Field(ge=0, le=31)
    punch_in: str | None = Field(max_length=30)
    punch_out: str | None = Field(max_length=30)
    attendance_minutes: int | None = Field(ge=0)
    productive_seconds: int | None = Field(ge=0)
    idle_seconds: int | None = Field(ge=0)
    tracked_seconds: int | None = Field(ge=0)
    screenshot_count: int | None = Field(ge=0)
    attendance_minutes_days: int = Field(ge=0, le=31)
    productive_seconds_days: int = Field(ge=0, le=31)
    idle_seconds_days: int = Field(ge=0, le=31)
    tracked_seconds_days: int = Field(ge=0, le=31)
    screenshot_count_days: int = Field(ge=0, le=31)
    average_activity: float | None = Field(ge=0, le=100)
    activity_days: int = Field(ge=0, le=31)

    @model_validator(mode='after')
    def coverage(self):
        for field in ('attendance_minutes', 'productive_seconds', 'idle_seconds', 'tracked_seconds', 'screenshot_count'):
            known = getattr(self, field + '_days')
            if known > self.days or (getattr(self, field) is None) != (known == 0):
                raise ValueError('Invalid work metric coverage')
        if self.activity_days > self.days or (self.average_activity is None) != (self.activity_days == 0):
            raise ValueError('Invalid activity coverage')
        return self


class WorkDay(StrictModel):
    date: date
    punch_in: str | None = Field(max_length=30)
    punch_out: str | None = Field(max_length=30)
    attendance_minutes: int | None = Field(ge=0)
    eligible: bool
    late: bool
    missing_punchout: bool
    short_day: bool
    productive_seconds: int | None = Field(ge=0)
    idle_seconds: int | None = Field(ge=0)
    tracked_seconds: int | None = Field(ge=0)
    activity: float | None = Field(ge=0, le=100)
    screenshot_count: int | None = Field(ge=0)


class WorkReview(StrictModel):
    summary: WorkTotals
    days: list[WorkDay] = Field(max_length=30)


class WorkEmployee(StrictModel):
    employee_id: int = Field(gt=0)
    employee_name: str = Field(max_length=250)
    tracking_available: bool
    summary: WorkTotals
    attention: list[str] = Field(max_length=2)
    review: WorkReview | None
    daily: list[WorkDay] = Field(default_factory=list, max_length=31)


class WorkPeriod(StrictModel):
    start: date
    end: date


class WorkRequestedPeriod(WorkPeriod):
    as_of: date


class WorkData(StrictModel):
    data_source: str
    scope: str
    period: WorkRequestedPeriod
    review_period: WorkPeriod
    detailed_review: bool
    employee_ids: list[int] = Field(max_length=10)
    rows: list[WorkEmployee] = Field(max_length=10)


def metric(value, *, seconds=False):
    if value is None:
        return 'unavailable'
    return duration(value // 60 if seconds else value)


def format_work_report(data: WorkData) -> TaskResult:
    period = data.period
    label = str(period.start) if period.start == period.end else f'{period.start} to {period.end}'
    lines = [f'Employee work report - {label}']
    for row in data.rows:
        summary = row.summary
        lines.extend(['', row.employee_name])
        if period.start == period.end:
            lines.append(f'Punch-in/out: {summary.punch_in or "unavailable"} - {summary.punch_out or "unavailable"}')
        coverage = f' ({summary.attendance_minutes_days}/{summary.days} days with complete punches)' if summary.days != 1 or summary.attendance_minutes_days != 1 else ''
        lines.append(f'Attendance hours: {metric(summary.attendance_minutes)}{coverage}.')
        if row.tracking_available:
            lines.append(f'Tracked: {metric(summary.tracked_seconds, seconds=True)} | Productive: {metric(summary.productive_seconds, seconds=True)} | Idle: {metric(summary.idle_seconds, seconds=True)}')
            activity = 'unavailable' if summary.average_activity is None else f'{summary.average_activity:g}% ({summary.activity_days} scored {"day" if summary.activity_days == 1 else "days"})'
            captures = 'unavailable' if summary.screenshot_count is None else str(summary.screenshot_count)
            lines.append(f'Average activity: {activity} | Screenshot captures: {captures}')
            if any(getattr(summary, field + '_days') < summary.days for field in ('productive_seconds', 'idle_seconds', 'tracked_seconds')):
                lines.append('Tracking totals cover recorded days only; missing days are not zero activity.')
        else:
            lines.append('Screenshot/activity tracking: unavailable under current module access or assignment.')
        if row.attention:
            lines.append('Needs attention (30 completed days ending ' + str(data.review_period.end) + '): ' + ' '.join(row.attention))
        if row.review is not None:
            lines.append(f'Detailed review: {data.review_period.start} to {data.review_period.end}')
            for day in row.review.days:
                activity = 'unavailable' if day.activity is None else f'{day.activity:g}%'
                flags = [label for flag, label in [('late', 'late arrival'), ('missing_punchout', 'missing punch-out'), ('short_day', 'below shift threshold')] if getattr(day, flag)]
                lines.append(f'{day.date}: attendance {metric(day.attendance_minutes)}; productive {metric(day.productive_seconds, seconds=True)}; idle {metric(day.idle_seconds, seconds=True)}; activity {activity}' + ('; ' + ', '.join(flags) if flags else ''))
    if not data.rows:
        lines.append('No active employees are available in this authorized scope.')
    lines.append('Attendance and tracker hours differ; activity is not a work-quality rating.')
    context = {'module': 'attendance', 'report': 'work_summary', 'scope': data.scope, 'employee_ids': data.employee_ids,
               'start_date': str(period.start), 'end_date': str(period.end), 'work_report': True}
    return TaskResult({'handled_locally': True, 'report': 'work_summary'},
                      AssistantReply('\n'.join(lines), model_content='An authorized employee work report was displayed locally; records and screenshots were not sent to the model.', query_context=context), chart_source=data)
