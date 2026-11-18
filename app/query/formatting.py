"""Display durations without changing the numeric reporting contract."""

import math


def display(value, unit=""):
    if value is None:
        return "Unavailable"
    if unit == "hours" and isinstance(value, (int, float)) and not isinstance(value, bool):
        minutes = math.floor(abs(value) * 60 + 0.5)
        sign = "-" if value < 0 and minutes else ""
        return f"{sign}{minutes // 60}h {minutes % 60:02d}m"
    return f"{value:g}" if isinstance(value, float) else str(value)


def hour_totals(data):
    """Sum returned hour measures only; never imply a partial page is complete."""
    if not data["rows"]:
        return []
    totals = []
    page = " (this page)" if data.get("has_more") or data.get("offset") else ""
    for column in data["columns"]:
        if column.get("unit") != "hours":
            continue
        values = [row.get(column["key"]) for row in data["rows"]]
        total = (
            math.fsum(values)
            if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values)
            else None
        )
        totals.append((f"Total {column['label']}{page}", total))
    return totals
