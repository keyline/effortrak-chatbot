# Manager question catalogue

These are behavioral examples for the local query router and regression tests, not
model fine-tuning data. Reports use authenticated backend tools; the model does not
decide access or calculate database totals. Existing web/app workflows are unchanged.

## Implemented read-only reports

| Question | Meaning |
| --- | --- |
| Who was late in my team yesterday? | Canonical attendance-calendar late status |
| Who was late more than 3 times this month? | More than 3 matching days per authorized employee |
| How many days was Joydeep Ghosh late last month? | Locally resolve one employee, then count canonical late days across the full month |
| Who missed punch-out yesterday? | Canonical pending punch-out status |
| Who was present/absent yesterday? | Calendar status, with blank days excluded from absence |
| Compare my team attendance with last month | Current and previous calendar-month summaries; current month may be incomplete |
| Where did I mark attendance yesterday? | Recorded attendance coordinates/address; requires location permission |
| Show attendance with missing GPS yesterday | Missing/invalid coordinates for a recorded punch; no misconduct inference |
| Show permitted work arrangements for my team | Current configured WFH/WFO allowances, not daily choices |
| Show planned versus booked time for 2026-09 | Per-employee scheduled and linked-timesheet totals |
| Tell me Subhomoy Samanta (Employee) last month total booking hours | Locally resolve the employee and total timesheet bookings by booking date |
| Show my booked hours for 2026-09 | Personal timesheet totals, not attendance or linked-task-only effort |
| Show Subhomoy Samanta last month billable and non-billable hours | Same timesheet report, with a billing breakdown |
| Also show billable and non-billable | Retain the previous booking report's employee and period |
| Compare planned and booked time across projects for 2026-09 | Per-project totals, under existing task visibility |
| Which tasks have booked time greater than planned for 2026-09? | Linked booked minutes strictly exceed planned minutes |
| Who has the highest planned workload this week? | Scheduled planned time, descending; not available capacity |
| Who has more than 8 planned hours tomorrow? | Planned minutes strictly greater than 480 |
| Who is on approved leave tomorrow? | Approved requests overlapping the day; pending withdrawal remains approved |
| Show missing punch-outs for employee ID 45 for 2026-09 | Explicit employee constraint retained during clarification |
| Where did Subhomoy Samanta mark attendance yesterday? | Local authorized name lookup; ambiguity never expands the query |

Periods support today, yesterday, tomorrow, this/last month, this week, last 7 days,
YYYY-MM, a single date and custom ranges up to 93 days. Missing periods prompt one
clarification. Reply with a period to retain the report, employee and threshold.
Named attendance counts never expand to company-wide daily attendance. Ambiguous names
retain the requested period and ask for a full name or employee ID. An authorized
single-employee count of zero is reported as zero, not missing records.
Report details are capped at 50 and clearly labelled when truncated. Location reports
with more than 500 punch records must be narrowed rather than returning partial counts.

## Privacy and deployment

Names, project names, addresses, coordinates and rendered manager reports stay local,
including later conversation turns. All queries enforce company and employee access;
task reports also enforce existing tracker/department visibility.

Whole timesheet totals additionally require the existing `EFFORT REPORT` permission
(the web route uses the normalized alias `EFFORT_REPORT`).
That report is separate from Task Assign's department-filtered linked-booking totals.
Company/team scope still comes from authenticated employee visibility. No roles are
granted new permissions. Duplicate assignment links do not duplicate bookings, and
entries with unavailable durations are explicitly identified rather than treated as zero.
Booking reports always show billable (`timesheet.bill=0`) and non-billable (`bill=1`)
recorded minutes. Other or missing billing flags are labelled unclassified; category
counts and minutes must reconcile with the total. No billing amounts or invoice status
are inferred from these flags.

Precise location requires `AI ATTENDANCE LOCATION / ai.attendance.location` in addition
to attendance access. Apply the new CodeIgniter migration or
`effortrak_web/docs/sql/2026-10-12-add-ai-attendance-location.sql`, then assign the
permission only to approved roles. Neither option grants roles automatically.

## Explicitly not enabled yet

- Historical/daily WFH/WFO selections, monthly WFH day counts, WFH approvals and WFH-day task comparisons.
- GPS accuracy, geofence violations, office distances and live/current employee tracking.
- Deadline/completion/aging analysis across project tasks and scheduled Task Assign records.
- Available capacity, irregular-attendance judgments, arbitrary comparison periods and client-wide aggregation.
- Approval queues, assignments, approvals/rejections, reminders and scheduled delivery.
- Task/project PDF/XLSX exports and attendance exports containing precise locations or work modes.

These questions receive explicit limitation replies. Do not claim that records are
missing or an operation succeeded. New capabilities require their business rules,
read/write permissions, confirmation safeguards and regression cases before enabling.

## Reply contract

Answer first, identify the subject and period, and use short factual lines. Ask only
the missing clarification. Do not expose credentials, raw backend errors or sensitive
details to the model. Separate planned effort, linked booked time and attendance worked
time; preserve unavailable values instead of converting them to zero.
