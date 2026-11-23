import httpx
from typing import Literal
from fastapi import HTTPException
from pydantic import Field, ValidationError

from app.auth import Identity
from app.config import Settings
from app.schemas import StrictModel


class BackendContext(StrictModel):
    company_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    name: str
    designation: str
    scopes: list[str]
    can_export: bool
    policy_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    data_source: str
    modules: list[str] = Field(default_factory=lambda: ["attendance"])
    report_scopes: dict[
        Literal["lms_leads", "lms_followups"], list[Literal["self", "company"]]
    ] = Field(default_factory=dict)


class BackendClient:
    def __init__(self, config: Settings):
        self.config = config
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def call(self, endpoint: str, token: str, body: dict | None = None) -> dict:
        url = self.config.backend_url.rstrip("/") + "/apiv2/ai/internal/" + endpoint
        headers = {"X-AI-Service-Key": self.config.ai_service_secret.get_secret_value()}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            # Large report batches need more read time; connection/auth calls stay short.
            response = await self.client.post(url, json=body or {}, headers=headers,
                                             timeout=httpx.Timeout(60, connect=5) if endpoint == 'query' else 20)
        except httpx.HTTPError:
            raise HTTPException(
                502, "Effortrak backend is unavailable or timed out."
            ) from None
        if response.status_code >= 400:
            status = (
                response.status_code
                if response.status_code in (401, 403, 404, 409, 413, 422, 429, 503)
                else 502
            )
            # Forward only audited messages; arbitrary backend details remain private.
            if status == 429 and endpoint == "token-reserve":
                raise HTTPException(
                    429,
                    "Company AI token allowance is insufficient for this request. Contact your administrator.",
                )
            if endpoint == "query" and status in (413, 422):
                try:
                    message = response.json().get("message")
                except (ValueError, AttributeError):
                    message = None
                if message in {
                    "Narrow the reporting period. Attendance supports 93 days; other reports support 366 days.",
                    "Narrow attendance to an employee, smaller team or shorter period.",
                    "This export is too large. Narrow the period, employee selection or grouping.",
                    "Unsupported query selection. Use only approved fields, metrics, filters and reporting operations.",
                    "This dataset is not available through chat.",
                    "Employee selections do not match.",
                    "Project selections do not match.",
                }:
                    raise HTTPException(status, message)
            if endpoint == "visit-location" and status in (413, 422):
                try:
                    message = response.json().get("message")
                except (ValueError, AttributeError):
                    message = None
                if message in (
                    "Select one employee for a last recorded location.",
                    "Future location records are unavailable.",
                    "Client location storage is unavailable.",
                    "Visit storage is unavailable.",
                    "GPS location storage is unavailable.",
                    "Use the existing client report to narrow this large client catalogue.",
                    "Limit the report to a team or employee (maximum 50 employees).",
                ):
                    raise HTTPException(status, message)
            if endpoint == "work-report" and status in (413, 422):
                try:
                    message = response.json().get("message")
                except (ValueError, AttributeError):
                    message = None
                if message in (
                    "Work reports support up to 31 days.",
                    "Future work records are unavailable.",
                    "Select an employee or a team of at most 10 employees for work reports.",
                ):
                    raise HTTPException(status, message)
            if status == 403:
                try:
                    message = response.json().get("message")
                except (ValueError, AttributeError):
                    message = None
                safe_messages = {
                    "LMS permission and an assigned LMS seat are required.": "Your account needs LMS permission and an assigned LMS seat. Super Admin retains the existing seat exemption.",
                    "LMS lead ownership does not permit this scope.": "You can view your assigned LMS records. Company LMS reports require the existing manage-all-leads permission.",
                    "An assigned Asset Management seat is required for this report.": "Your account needs an assigned Asset Management seat for this report. You can still ask about your current assigned assets.",
                    "Asset reports support own or company scope only.": "Asset reports support your own records or authorized company records; team reporting is not available.",
                    "Leave reasons are restricted to Admin and Super Admin.": "Only Admin and Super Admin can view leave reasons through chat.",
                    "Leave balance permission is required.": "Your role needs Leave Balance permission to view employee leave balances.",
                    "Location or client-visit report permission is required.": "Your company subscription and role must allow the requested client-visit or location report. Ask your administrator to check access.",
                    "Daily report permission is required for work reports.": "Your role needs Daily Report permission to view employee work summaries.",
                    "Effort report permission is required.": "Your role needs Effort Report permission to view timesheet booking totals.",
                    "Attendance location permission is required.": "Your role needs the separate attendance-location permission to view recorded locations.",
                    "Employee is outside your permitted scope.": "The requested employee is not available within your authorized scope. "
                    "Choose an employee you are permitted to view; you can still ask about your own attendance.",
                    "Employee scope is not permitted.": "You do not have permission for the requested employee scope. You can still ask about your own attendance.",
                    "Module read permission is not granted for this scope.": "You do not have permission to read this module in the requested scope. Ask your administrator to check the module permissions.",
                    "You do not have report export permission.": "You do not have permission to export reports. You can still ask to view authorized attendance.",
                    "You do not have permission to use the AI assistant.": "Your role does not have permission to use the AI assistant.",
                    "AI assistant is disabled for your company.": "AI assistant is disabled for your company.",
                    "No licensed AI-readable modules are available.": "No licensed AI-readable modules are available for your account.",
                    "Module is not licensed.": "The requested module is not available under your company license.",
                    "Task visibility is not enabled.": "Task visibility is not enabled for your account.",
                    "Existing task visibility allows only your own tasks.": "Your task permissions allow only your own tasks. Try asking for your tasks instead.",
                }
                if isinstance(message, str) and message in safe_messages:
                    raise HTTPException(status, safe_messages[message])
            if status == 422 and endpoint == "export":
                try:
                    message = response.json().get("message")
                except (ValueError, AttributeError):
                    message = None
                if message in (
                    "No active members are assigned to your led teams in this company. Team reports exclude managers, admins and other leads.",
                    "No active direct reports are assigned to you in this company. Team attendance includes only employees whose primary manager is you.",
                    "No active employees were found in your authorized attendance scope.",
                ):
                    raise HTTPException(status, message)
            raise HTTPException(
                status,
                "Effortrak backend rejected the request. Check login, permissions, and configuration.",
            )
        if response.status_code != 200 or len(response.content) > 12 * 1024 * 1024:
            raise HTTPException(502, "Invalid Effortrak backend response.")
        try:
            result = response.json()
            if result.get("success") is not True or not isinstance(
                result.get("data"), dict
            ):
                raise ValueError("Invalid envelope")
            return result["data"]
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(502, "Invalid Effortrak backend response.") from None

    async def identity(self, token: str, company_id: int, user_id: int) -> Identity:
        data = await self.call("context", token)
        try:
            context = BackendContext.model_validate(data)
            if (
                context.company_id != company_id
                or context.user_id != user_id
                or context.data_source != "effortrak_backend"
                or not context.scopes
                or not set(context.scopes).issubset({"self", "team", "company"})
                or not context.modules
                or any(not scopes for scopes in context.report_scopes.values())
                or (context.report_scopes and "lms" not in context.modules)
                or not set(context.modules).issubset(
                    {
                        "attendance",
                        "leave",
                        "tasks",
                        "visits",
                        "location",
                        "lms",
                        "assets",
                    }
                )
            ):
                raise ValueError("Identity mismatch")
        except (ValidationError, ValueError):
            raise HTTPException(
                502, "Backend identity or permission context is invalid."
            ) from None
        allowed = (
            {"self", "company"}
            if context.designation.strip().upper() in ("ADMIN", "SUPER ADMIN")
            else {"self"}
        )
        scopes = tuple(scope for scope in context.scopes if scope in allowed)
        report_scopes = {
            key: tuple(scope for scope in values if scope in allowed)
            for key, values in context.report_scopes.items()
        }
        return Identity(
            company_id,
            user_id,
            context.name,
            context.designation,
            scopes,
            context.can_export,
            "effortrak_backend",
            context.policy_version,
            token,
            tuple(context.modules),
            report_scopes,
        )

    async def revalidate(self, identity: Identity):
        current = await self.identity(
            identity.credential, identity.company_id, identity.user_id
        )
        if current.policy_key != identity.policy_key:
            raise HTTPException(409, "Permissions changed. Start a new conversation.")
