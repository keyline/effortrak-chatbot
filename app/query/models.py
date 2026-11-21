import re
from copy import deepcopy
from datetime import date
from typing import Literal

from pydantic import Field, ValidationInfo, model_validator

from app.schemas import StrictModel

SNAPSHOT_DATASETS = ("employees", "assets", "lms_leads")


class Condition(StrictModel):
    field: str = Field(min_length=1, max_length=60)
    operator: Literal[
        "=", "!=", ">", ">=", "<", "<=", "contains", "in", "is_null", "not_null"
    ]
    value: str | int | float | list[str | int | float] | None = None


class Sort(StrictModel):
    field: str = Field(min_length=1, max_length=60)
    direction: Literal["asc", "desc"] = "asc"


class QueryPlan(StrictModel):
    dataset: Literal[
        "employees",
        "attendance",
        "effort",
        "tasks",
        "leave",
        "assets",
        "asset_assignments",
        "lms_leads",
        "lms_followups",
    ]
    mode: Literal["details", "aggregate"] = "details"
    scope: Literal["self", "team", "company"] = "self"
    employee_id: int | None = Field(default=None, gt=0)
    employee_name: str | None = Field(default=None, min_length=2, max_length=120)
    project_id: int | None = Field(default=None, gt=0)
    project_name: str | None = Field(default=None, min_length=2, max_length=120)
    start_date: date | None = None
    end_date: date | None = None
    fields: list[str] = Field(default_factory=list, max_length=10)
    metrics: list[str] = Field(default_factory=list, max_length=10)
    group_by: list[str] = Field(default_factory=list, max_length=10)
    filters: list[Condition] = Field(default_factory=list, max_length=10)
    having: list[Condition] = Field(default_factory=list, max_length=10,
        description="Only explicitly requested numeric aggregate thresholds. Use [] for often/frequently/consistently without a stated minimum.")
    having_group_by: list[str] | None = Field(default=None, max_length=10,
        description="Attendance only: grouping at which having selects the cohort before displaying finer groups. Null applies having to the output groups.")
    order_by: list[Sort] = Field(default_factory=list, max_length=4)
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10000)

    @model_validator(mode="after")
    def reporting_contract(self):
        if self.project_name and self.dataset in ("effort", "tasks"):
            # One resolved project selector replaces its duplicate literal name filter.
            self.filters = [item for item in self.filters if not (
                item.field == "project_name" and item.operator in ("=", "contains")
                and isinstance(item.value, str)
                and item.value.strip().lower() == self.project_name.strip().lower()
            )]
        if self.dataset in SNAPSHOT_DATASETS:
            if self.start_date or self.end_date:
                raise ValueError(
                    "Snapshot reports do not accept a reporting period; use approved date filters."
                )
        elif (
            not self.start_date
            or not self.end_date
            or not 0 <= (self.end_date - self.start_date).days <= 365
        ):
            raise ValueError("Select both dates, up to 366 inclusive days.")
        if self.dataset == "attendance" and (self.end_date - self.start_date).days > 92:
            raise ValueError("Attendance supports up to 93 days.")
        if self.mode == "details" and (self.metrics or self.group_by or self.having):
            raise ValueError("Detail queries cannot contain aggregate selections.")
        if self.mode == "aggregate" and (not self.metrics or self.fields):
            raise ValueError("Aggregate queries require metrics and no detail fields.")
        if len(self.group_by) + len(self.metrics) > 10:
            raise ValueError("Select at most ten output columns.")
        if self.having_group_by is not None and (
            self.dataset != "attendance" or self.mode != "aggregate" or not self.having
            or not set(self.having_group_by) <= set(self.group_by)
            or len(set(self.having_group_by)) != len(self.having_group_by)
        ):
            raise ValueError("Attendance cohort grouping requires aggregate thresholds and a subset of output groups.")
        if self.dataset == "attendance":
            for condition in self.filters:
                if condition.field not in (
                    "punch_in",
                    "punch_out",
                ) or condition.operator in ("is_null", "not_null"):
                    continue
                if condition.operator == "contains":
                    raise ValueError("Use a time comparison for punch times.")
                values = (
                    condition.value if condition.operator == "in" else [condition.value]
                )
                if not isinstance(values, list) or not values:
                    raise ValueError("Select valid punch times.")
                normalized = []
                for value in values:
                    if not isinstance(value, str) or not re.fullmatch(
                        r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", value
                    ):
                        raise ValueError(
                            "Use a valid 24-hour punch time, such as 09:00:00."
                        )
                    parts = [int(part) for part in value.split(":")]
                    normalized.append(
                        f"{parts[0]:02}:{parts[1]:02}:{parts[2] if len(parts) == 3 else 0:02}"
                    )
                condition.value = (
                    normalized if condition.operator == "in" else normalized[0]
                )
        return self

    @model_validator(mode="after")
    def catalogue_vocabulary(self, info: ValidationInfo):
        """Check dataset vocabulary independently of provider schema generation.

        PHP still authorizes and validates execution. This check also applies
        after refinements, before a model selection can reach that backend.
        """
        if not info.context or "catalogue" not in info.context:
            return self
        datasets = info.context["catalogue"].get("datasets", {})
        if self.dataset not in datasets:
            raise ValueError("Select an available catalogue dataset.")
        metadata = datasets[self.dataset]
        fields = set(metadata["fields"])
        metrics = set(metadata["metrics"])
        checks = [
            (self.fields + self.group_by, fields),
            (self.metrics, metrics),
            ([item.field for item in self.filters], fields),
            ([item.field for item in self.having], metrics),
            ([item.field for item in self.order_by], fields | metrics),
        ]
        if any(not set(selected) <= allowed for selected, allowed in checks):
            raise ValueError(
                "Use only the selected dataset's catalogue fields and metrics."
            )
        return self


class AggregateRefinement(StrictModel):
    operation: Literal["aggregate"]
    metrics: list[str] = Field(min_length=1, max_length=10)
    group_by: list[str] = Field(default_factory=list, max_length=10)
    order_by: list[Sort] = Field(default_factory=list, max_length=4)


class SortRefinement(StrictModel):
    operation: Literal["sort"]
    order_by: list[Sort] = Field(min_length=1, max_length=4)


class ColumnsRefinement(StrictModel):
    operation: Literal["columns"]
    columns: list[str] = Field(min_length=1, max_length=10)


class FilterRefinement(StrictModel):
    operation: Literal["filter"]
    filters: list[Condition] = Field(max_length=10)


class PeriodRefinement(StrictModel):
    operation: Literal["period"]
    start_date: date
    end_date: date


class PageRefinement(StrictModel):
    operation: Literal["page"]
    direction: Literal["next", "previous", "first"]


Refinement = (
    AggregateRefinement
    | SortRefinement
    | ColumnsRefinement
    | FilterRefinement
    | PeriodRefinement
    | PageRefinement
)


class Interpretation(StrictModel):
    action: Literal[
        "query",
        "refine",
        "clarify",
        "denied",
        "unsupported",
        "help",
        "specialized",
        "product_overview",
    ]
    clarification: (
        Literal["period", "subject", "metric", "report", "selection"] | None
    ) = None
    plan: QueryPlan | None = None
    specialized: "SpecializedPlan | None" = None
    refinement: Refinement | None = None
    chart: Literal["pie", "bar"] | None = None
    export: Literal["pdf", "xlsx", "both"] | None = None

    @model_validator(mode="after")
    def selection(self):
        if self.action == "query" and self.plan is None:
            raise ValueError("A query requires a plan.")
        if self.action == "specialized" and self.specialized is None:
            raise ValueError("A specialized query requires a selection.")
        if self.action == "refine" and self.refinement is None:
            raise ValueError("A report refinement requires an operation.")
        return self


class SpecializedPlan(StrictModel):
    kind: Literal[
        "late_punchin",
        "punch_locations",
        "work_summary",
        "leave_balance",
        "visits",
        "last_location",
        "client_locations",
        "lms_location",
        "work_arrangements",
    ]
    scope: Literal["self", "team", "company"] = "self"
    employee_id: int | None = Field(default=None, gt=0)
    employee_name: str | None = Field(default=None, min_length=2, max_length=120)
    start_date: date | None = None
    end_date: date | None = None
    financial_year: str | None = Field(default=None, max_length=20)
    leave_type: str | None = Field(default=None, max_length=120)
    client_name: str | None = Field(default=None, max_length=120)
    punch_basis: Literal["late_cutoff", "shift_start", "fixed_time"] = "late_cutoff"
    punch_after: str | None = Field(default=None, max_length=8)
    min_delay_minutes: int = Field(default=0, ge=0, le=1440)
    balance_filter: Literal["all", "positive", "nonnegative", "negative", "zero"] = (
        "all"
    )
    ranking: Literal["highest", "lowest"] = "highest"
    limit: int = Field(default=20, ge=1, le=50)
    page: int = Field(default=1, ge=1, le=1000)


Interpretation.model_rebuild()


class EmployeeSubject(StrictModel):
    kind: Literal["self", "employee", "company", "previous", "unspecified"]
    employee_name: str | None = Field(default=None, min_length=2, max_length=120)
    employee_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def subject_contract(self):
        selectors = int(self.employee_name is not None) + int(self.employee_id is not None)
        if selectors != (1 if self.kind == "employee" else 0):
            raise ValueError("Employee subjects require exactly one explicit name or ID; other subjects require neither.")
        return self


class InterpretationEnvelope(StrictModel):
    subject: EmployeeSubject
    selection: Interpretation


def strict_schema(model):
    """Responses strict mode requires every property, including nullable values."""
    schema = model.model_json_schema()

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema


def interpretation_schema(catalogue):
    """Expose mutually exclusive report shapes, rather than prompt-only rules.

    PHP remains authoritative. This schema prevents the model from mixing detail
    columns with aggregate metrics and restricts vocabulary to the catalogue.
    """
    schema = strict_schema(Interpretation)
    datasets = catalogue.get("datasets", {})
    if not datasets:
        return _selection_envelope(schema)
    base = schema["$defs"]["QueryPlan"]
    branches = []
    for dataset in QueryPlan.model_fields["dataset"].annotation.__args__:
        if dataset not in datasets:
            continue
        fields = list(datasets[dataset]["fields"])
        metrics = list(datasets[dataset]["metrics"])
        for mode in ("details", "aggregate"):
            branch = deepcopy(base)
            branch["description"] = datasets[dataset].get("description", "")
            props = branch["properties"]
            props["dataset"] = {"type": "string", "enum": [dataset]}
            props["mode"] = {"type": "string", "enum": [mode]}
            for key, allowed in [
                ("fields", fields),
                ("group_by", fields),
                ("metrics", metrics),
            ]:
                props[key]["items"] = {"type": "string", "enum": allowed}
            for key, allowed in [("filters", fields), ("having", metrics)]:
                condition = deepcopy(schema["$defs"]["Condition"])
                condition["properties"]["field"] = {"type": "string", "enum": allowed}
                props[key]["items"] = condition
            sort = deepcopy(schema["$defs"]["Sort"])
            sort["properties"]["field"] = {
                "type": "string",
                "enum": list(dict.fromkeys(fields + metrics)),
            }
            props["order_by"]["items"] = sort
            if mode == "details":
                props["having_group_by"] = {"type": "null"}
                for key in ("metrics", "group_by", "having"):
                    props[key]["maxItems"] = 0
            else:
                props["fields"]["maxItems"] = 0
                props["metrics"]["minItems"] = 1
            if dataset in SNAPSHOT_DATASETS:
                for key in ("start_date", "end_date"):
                    props[key] = {"type": "null"}
            else:
                for key in ("start_date", "end_date"):
                    props[key] = {"type": "string", "format": "date"}
            branches.append(branch)
    if branches:
        schema["$defs"]["QueryPlan"] = {"anyOf": branches}
    return _selection_envelope(schema)


def compact_interpretation_schema(catalogue):
    """Share query shapes by mode/date contract instead of by every dataset.

    Dataset-specific vocabulary and date rules are checked by QueryPlan with
    catalogue context, and again by PHP. All types, bounds, action/payload
    combinations and detail/aggregate separation remain provider-constrained.
    Full vocabulary and business rules remain in the prompt catalogue.
    """
    schema = strict_schema(Interpretation)
    base = schema["$defs"]["QueryPlan"]
    supported = QueryPlan.model_fields["dataset"].annotation.__args__
    available = [name for name in supported if name in catalogue.get("datasets", {})]
    # Match the original empty-catalogue schema fallback. Runtime catalogue
    # validation still rejects execution when a dataset is unavailable.
    available = available or list(supported)
    branches = []
    for snapshot in (True, False):
        datasets = [
            name for name in available if (name in SNAPSHOT_DATASETS) == snapshot
        ]
        if not datasets:
            continue
        for mode in ("details", "aggregate"):
            branch = deepcopy(base)
            props = branch["properties"]
            props["dataset"] = {"type": "string", "enum": datasets}
            props["mode"] = {"type": "string", "enum": [mode]}
            for key in ("start_date", "end_date"):
                props[key] = (
                    {"type": "null"}
                    if snapshot
                    else {"type": "string", "format": "date"}
                )
            if mode == "details":
                props["having_group_by"] = {"type": "null"}
                for key in ("metrics", "group_by", "having"):
                    props[key]["maxItems"] = 0
            else:
                props["fields"]["maxItems"] = 0
                props["metrics"]["minItems"] = 1
            branches.append(branch)
    schema["$defs"]["QueryPlan"] = {"anyOf": branches}
    return _selection_envelope(schema)


def _subject_schema():
    # Match the runtime validator in strict mode, before tokens can form a contradiction.
    branches = []
    for kinds, selector in [
        (["self", "company", "previous", "unspecified"], None),
        (["employee"], "employee_name"),
        (["employee"], "employee_id"),
    ]:
        branch = strict_schema(EmployeeSubject)
        props = branch["properties"]
        props["kind"]["enum"] = kinds
        for field in ("employee_name", "employee_id"):
            props[field] = (
                next(item for item in props[field]["anyOf"] if item.get("type") != "null")
                if field == selector else {"type": "null"}
            )
        branches.append(branch)
    return {"anyOf": branches}


def _selection_envelope(schema):
    """Nested anyOf makes action/payload combinations mutually exclusive."""
    branches = []
    for actions, payload in [
        (["query"], "plan"),
        (["refine"], "refinement"),
        (["specialized"], "specialized"),
        (["clarify", "denied", "unsupported", "help", "product_overview"], None),
    ]:
        branch = deepcopy(
            {key: value for key, value in schema.items() if key != "$defs"}
        )
        props = branch["properties"]
        props["action"] = {"type": "string", "enum": actions}
        for key in ("plan", "refinement", "specialized"):
            if key != payload:
                props[key] = {"type": "null"}
            else:
                options = [
                    item for item in props[key]["anyOf"] if item.get("type") != "null"
                ]
                props[key] = options[0] if len(options) == 1 else {"anyOf": options}
        branches.append(branch)
    return {
        "type": "object",
        "properties": {
            "subject": _subject_schema(),
            "selection": {"anyOf": branches},
        },
        "required": ["subject", "selection"],
        "additionalProperties": False,
        "$defs": schema["$defs"],
    }
