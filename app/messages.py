"""Public wording only. Internal errors, status codes and access rules stay unchanged."""

USAGE_LIMIT = "The AI usage limit has been reached. Please contact your administrator to continue using the AI Assistant."
TRY_AGAIN = "We couldn't complete your request. Please try again."
ACCESS_CHANGED = "Your access has changed. Please start a new chat to continue."

# Exact matches keep report content and names out of message rewriting.
_GROUPS = {
    USAGE_LIMIT: (
        "Company AI token allowance is insufficient for this request. Contact your administrator.",
    ),
    "Your session has expired. Please sign in again to continue.": (
        "Backend mode requires authenticated CodeIgniter delegation.",
        "Invalid or expired delegated credential.",
        "Invalid or missing demo bearer token.",
    ),
    ACCESS_CHANGED: ("Permissions changed. Start a new conversation.",),
    "The AI Assistant is temporarily unavailable. Please try again shortly.": (
        "Effortrak backend is unavailable or timed out.",
        "AI interpretation is unavailable or timed out. Please retry.",
        "AI token budgeting is unavailable. Please retry later.",
        "The reporting request could not be completed. Please retry.",
    ),
    "The AI Assistant isn't available for your account right now. Please contact your administrator for help.": (
        "Natural-language reporting requires the configured AI interpreter and authenticated backend.",
        "Connect the authenticated Effortrak backend to query real records. Sample mode does not answer company-data questions.",
        "AI assistant is disabled for your company.",
        "Your role does not have permission to use the AI assistant.",
        "No licensed AI-readable modules are available for your account.",
    ),
    "We couldn't understand the report you need. Please describe it another way, including the employee, dates and details you'd like to see.": (
        "The AI returned an invalid reporting selection. Please rephrase it.",
        "The AI could not interpret this question. Please rephrase it.",
        "Unsupported query selection. Use only approved fields, metrics, filters and reporting operations.",
    ),
    "This report isn't available in the AI Assistant yet. Please try another report.": (
        "This dataset is not available through chat.",
    ),
    "The AI Assistant can help you view reports, but can't make changes to records. Please ask for a report or use the relevant module to make changes.": (
        "This request is not supported by the approved reporting datasets. You can ask about authorized records; changes to records are not available through chat.",
    ),
    "This information is restricted and can't be shared through the AI Assistant.": (
        "You do not have permission to view the requested information. Salary, passwords, credentials and private financial identifiers are not available through chat. Leave reasons require Admin or Super Admin and existing leave permission.",
    ),
    "You can view your own records. Please contact your administrator if you need access to another employee's records.": (
        "That employee selection is not available within your personal scope.",
        "You do not have permission for the requested employee scope. You can still ask about your own attendance.",
        "The requested employee is not available within your authorized scope. Choose an employee you are permitted to view; you can still ask about your own attendance.",
    ),
    "You don't have access to these employee records. Please contact your administrator if you need access.": (
        "You do not have permission to view the requested employee scope. Please contact your administrator if access is required.",
    ),
    "You don't have access to this report. Please contact your administrator for help.": (
        "You do not have permission to read this module in the requested scope. Ask your administrator to check the module permissions.",
        "The requested module is not available under your company license.",
    ),
    "Exporting reports isn't enabled for your account. Please contact your administrator if you need access.": (
        "You do not have permission to export reports.",
        "You no longer have permission to export reports.",
        "You do not have permission to export reports. You can still ask to view authorized attendance.",
        "Reports require authenticated backend access.",
    ),
    "You no longer have access to this download. Please contact your administrator for help.": (
        "You no longer have permission to download this report.",
    ),
    "This report has expired. Please generate a new report to download it.": (
        "This report has expired. Generate it again.",
        "This report expired. Generate it again.",
    ),
    "This report is no longer available. Please generate it again.": (
        "Report not found.", "Report job not found.",
        "Generate this report again using the current chatbot.",
    ),
    "This chat is no longer available. Please start a new chat.": ("Conversation not found.",),
    "The report details have changed. Please generate a new report to download the latest results.": (
        "The report selection or data has changed. Generate the report again.",
    ),
    "Please view the report again before requesting an export.": (
        "Display the reporting selection again before exporting.",
    ),
    "This report is too large to export. Please select fewer employees, a shorter period or fewer details, then try again.": (
        "This export is too large. Narrow the report and retry.",
        "This file is too large. Narrow the report and retry.",
        "This export is too large. Narrow the period, employee selection or grouping.",
        "Narrow this report before exporting; partial reports are not exported.",
    ),
    "Please select fewer employees or a shorter period to continue with this report.": (
        "This report took too long. Narrow the selection and retry.",
        "Narrow attendance to an employee, smaller team or shorter period.",
        "Narrow the reporting selection before requesting more pages.",
    ),
    "Please select a shorter period: up to 93 days for attendance or 366 days for other reports.": (
        "Narrow the reporting period. Attendance supports 93 days; other reports support 366 days.",
    ),
    "Please select a period of up to 31 days for this work report.": ("Work reports support up to 31 days.",),
    "Please choose today or an earlier date for this report.": (
        "Future location records are unavailable.", "Future work records are unavailable.",
    ),
    "Please select one employee to view their last recorded location.": ("Select one employee for a last recorded location.",),
    "Location reports are currently unavailable. Please contact your administrator for help.": (
        "Client location storage is unavailable.", "Visit storage is unavailable.", "GPS location storage is unavailable.",
    ),
    "Please select a team or employee, with no more than 50 employees in the report.": (
        "Limit the report to a team or employee (maximum 50 employees).",
    ),
    "There are too many clients for this report. Please choose a specific client or narrow the selection in the Clients report.": (
        "Use the existing client report to narrow this large client catalogue.",
    ),
    "No active employees are assigned to your reporting team. Please ask your administrator to check the team setup.": (
        "No active members are assigned to your led teams in this company. Team reports exclude managers, admins and other leads.",
        "No active direct reports are assigned to you in this company. Team attendance includes only employees whose primary manager is you.",
    ),
    "No employees are available for this attendance report. Please contact your administrator if this seems incorrect.": (
        "No active employees were found in your authorized attendance scope.",
    ),
    "Please select an employee or a team of up to 10 employees for this work report.": (
        "Select an employee or a team of at most 10 employees for work reports.",
    ),
    "The employee details don't match. Please select the employee again.": ("Employee selections do not match.",),
    "The project details don't match. Please select the project again.": ("Project selections do not match.",),
    "We couldn't identify a single employee you can access. Please provide their full name or employee ID.": (
        "No unique employee match is available within your access. Please provide the full name or employee ID.",
    ),
    "We couldn't find a matching project you can access for that period. Please try another project name or ID.": (
        "No matching project is available within your access and reporting period. Please provide another project name or ID.",
    ),
    "We couldn't identify the financial year. Please provide its start year or ask your administrator to check the setup.": (
        "No unique configured financial year matches. Please check the company financial-year setup or specify its start year.",
    ),
    "A request is already in progress. Please wait a moment before trying again.": (
        "Chat is busy. Please retry shortly.",
        "Your company has several requests running. Please retry shortly.",
        "A request is already running or chat capacity is busy. Please retry shortly.",
        "A request is already running. Please wait and retry.",
    ),
    "Several reports are being prepared. Please wait a moment before requesting another export.": (
        "Your company has several exports queued. Please retry later.",
    ),
    "Your request has expired. Please try again.": ("This chat turn expired. Please retry.",),
    "Help is temporarily unavailable. Please try again shortly or contact your administrator.": (
        "Help is currently unavailable. Please contact your administrator; no records have been changed.",
    ),
    "Please contact your administrator to enable LMS access for this report.": (
        "Your account needs LMS permission and an assigned LMS seat. Super Admin retains the existing seat exemption.",
    ),
    "You can view your assigned leads. Please contact your administrator if you need access to other leads.": (
        "You can view your assigned LMS records. Company LMS reports require the existing manage-all-leads permission.",
    ),
    "Please contact your administrator to enable Asset Management access for this report. You can still ask about assets assigned to you.": (
        "Your account needs an assigned Asset Management seat for this report. You can still ask about your current assigned assets.",
    ),
    "Please ask for your own assets or an asset report you have permission to view. Team asset reports aren't available.": (
        "Asset reports support your own records or authorized company records; team reporting is not available.",
    ),
    "Leave reasons are available only to Admin and Super Admin users with leave access.": (
        "Only Admin and Super Admin can view leave reasons through chat.",
    ),
    "You don't have access to leave balances. Please contact your administrator if you need access.": (
        "Your role needs Leave Balance permission to view employee leave balances.",
    ),
    "You don't have access to visit or location reports. Please contact your administrator if you need access.": (
        "Your company subscription and role must allow the requested client-visit or location report. Ask your administrator to check access.",
    ),
    "You don't have access to work summaries. Please contact your administrator if you need access.": (
        "Your role needs Daily Report permission to view employee work summaries.",
    ),
    "You don't have access to booked effort reports. Please contact your administrator if you need access.": (
        "Your role needs Effort Report permission to view timesheet booking totals.",
    ),
    "You don't have access to attendance locations. Please contact your administrator if you need access.": (
        "Your role needs the separate attendance-location permission to view recorded locations.",
        "Attendance location permission is required.",
    ),
    "Task reports aren't enabled for your account. Please contact your administrator for help.": (
        "Task visibility is not enabled for your account.",
    ),
    "You can view your own tasks. Please ask for your tasks, or contact your administrator if you need additional access.": (
        "Your task permissions allow only your own tasks. Try asking for your tasks instead.",
    ),
}
_MESSAGES = {old: new for new, originals in _GROUPS.items() for old in originals}


def friendly_message(message):
    return _MESSAGES.get(message, message)


def public_error(message, status):
    """Keep unrecognized service details private while preserving useful known guidance."""
    if isinstance(message, str) and (message in _MESSAGES or message in _GROUPS):
        return friendly_message(message)
    return {
        401: "Your session has expired. Please sign in again to continue.",
        403: "You don't have access to this request. Please contact your administrator for help.",
        404: "We couldn't find this chat or report. Please start a new chat or generate the report again.",
        409: "We couldn't complete this request with the current chat details. Please start a new chat and try again.",
        410: "This report has expired. Please generate a new report to download it.",
        413: "This request is too large. Please select fewer records or shorten your message, then try again.",
        422: "We couldn't process these details. Please check your question and try again.",
        429: "The AI Assistant is handling several requests. Please try again shortly.",
    }.get(status, TRY_AGAIN)
