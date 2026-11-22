from collections import defaultdict

from .storage import many


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lower = int(index)
    return round(values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower), 3)


def metrics(db, start, end, now):
    apps = many(db, "SELECT id,state,submitted_at,decided_at,info_request_round FROM applications WHERE created_at BETWEEN ? AND ?", (start, end))
    ids = {row["id"] for row in apps}
    by_state = defaultdict(int)
    for app in apps:
        by_state[app["state"]] += 1
    transitions = defaultdict(list)
    for row in db.execute("SELECT * FROM state_transitions ORDER BY id"):
        if row["application_id"] in ids:
            transitions[row["application_id"]].append(row)
    durations = defaultdict(list)
    turnaround, straight = [], 0
    for rows in transitions.values():
        for first, second in zip(rows, rows[1:]):
            durations[first["to_state"]].append(second["occurred_at"] - first["occurred_at"])
        states = {row["to_state"]: row["occurred_at"] for row in rows}
        if "ACTIVE" in states and "SUBMITTED" in states:
            turnaround.append(states["ACTIVE"] - states["SUBMITTED"])
            straight += int("MANUAL_REVIEW" not in states and "PROVISIONING_FAILED" not in states)
    decided = sum(app["decided_at"] is not None for app in apps)
    exception_counts = defaultdict(int)
    for row in db.execute("SELECT application_id,code FROM exceptions"):
        if row["application_id"] in ids:
            exception_counts[row["code"]] += 1
    provider = defaultdict(list)
    for req in db.execute("""SELECT p.*, c.application_id FROM provider_requests p JOIN verification_checks c ON c.id=p.check_id"""):
        if req["application_id"] in ids:
            provider[req["provider"]].append(req)
    review = [row for row in db.execute("SELECT * FROM review_tasks") if row["application_id"] in ids]
    open_tasks = [row for row in review if row["status"] not in {"DECIDED", "CANCELLED"}]
    fields = [row["status"] for row in db.execute("SELECT application_id,status FROM extracted_fields") if row["application_id"] in ids]
    total = len(apps)
    rounds = [app["info_request_round"] for app in apps if app["info_request_round"]]
    return {
        "applications_by_state": by_state,
        "time_in_state_seconds": {key: {"p50": percentile(values, .5), "p90": percentile(values, .9)} for key, values in durations.items()},
        "turnaround_seconds": {"p50": percentile(turnaround, .5), "p90": percentile(turnaround, .9)},
        "straight_through_rate": straight / decided if decided else 0,
        "exceptions": exception_counts,
        "exception_rate": {key: value / total for key, value in exception_counts.items()},
        "info_request_rate": len(rounds) / total if total else 0,
        "average_info_rounds": sum(rounds) / len(rounds) if rounds else 0,
        "providers": {name: {"requests": len(rows),
                             "callback_success_rate": sum(row["status"] in {"CALLBACK_RECEIVED", "LATE_CALLBACK"} for row in rows) / len(rows),
                             "timeout_rate": sum(row["status"] == "TIMED_OUT" for row in rows) / len(rows),
                             "error_rate": sum(row["status"] == "FAILED" for row in rows) / len(rows),
                             "p95_latency_ms": percentile([row["latency_ms"] for row in rows if row["latency_ms"] is not None], .95)}
                      for name, rows in provider.items()},
        "callback_totals": {row["status"]: row["count"] for row in db.execute("SELECT * FROM callback_counts")},
        "review_queue_depth": len(open_tasks),
        "review_sla_breaches": sum(row["sla_due_at"] < now for row in open_tasks),
        "review_handling_p50_seconds": percentile([row["decided_at"] - row["created_at"] for row in review if row["decided_at"]], .5),
        "extraction_auto_accept_rate": fields.count("AUTO_ACCEPTED") / len(fields) if fields else 0,
        "field_override_rate": fields.count("OPERATOR_OVERRIDDEN") / len(fields) if fields else 0,
        "dead_letters": sum(row["reference_id"] in ids for row in db.execute("SELECT reference_id FROM dead_letters WHERE status='PARKED'")),
    }
