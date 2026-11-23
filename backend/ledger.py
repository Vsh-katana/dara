"""Centralized Decimal-based calculations shared by dashboard, reports, and exports."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import sqlite3

from .jalali import gregorian_to_jalali, jalali_to_gregorian, jalali_year_start
from .money import decimal_value, money_text

ZERO = Decimal("0")


def iso_date(raw: str | None, fallback: date | None = None) -> date | None:
    if not raw:
        return fallback
    return date.fromisoformat(raw[:10])


def period_dates(anchor: date, period: str, calendar: str = "gregorian") -> tuple[date, date]:
    if period == "day":
        return anchor, anchor
    if period == "week":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    if period == "year":
        if calendar == "jalali":
            jy, _, _ = gregorian_to_jalali(anchor)
            start = jalali_year_start(jy)
            return start, jalali_year_start(jy + 1) - timedelta(days=1)
        return date(anchor.year, 1, 1), date(anchor.year, 12, 31)
    if calendar == "jalali":
        jy, jm, _ = gregorian_to_jalali(anchor)
        start = jalali_year_start(jy)
        if jm > 1:
            start = _jalali_month_start(jy, jm)
        end = _jalali_month_start(jy + 1, 1) - timedelta(days=1) if jm == 12 else _jalali_month_start(jy, jm + 1) - timedelta(days=1)
        return start, end
    return date(anchor.year, anchor.month, 1), date(anchor.year + (anchor.month == 12), anchor.month % 12 + 1, 1) - timedelta(days=1)


def _jalali_month_start(jy: int, jm: int) -> date:
    if jm <= 6:
        return jalali_year_start(jy) + timedelta(days=(jm - 1) * 31)
    return jalali_year_start(jy) + timedelta(days=186 + (jm - 7) * 30)


def _inclusion(row: sqlite3.Row) -> dict:
    try:
        values = json.loads(row["inclusion_json"] or "{}")
    except (ValueError, TypeError):
        values = {}
    return {"all": True, "report": True, "budget": True, "forecast": True, "operational": True, **values}


def transaction_rows(conn: sqlite3.Connection, *, start=None, end=None, kind=None, q=None, category=None,
                     account_id=None, min_amount=None, max_amount=None, included=None, tags=None,
                     report_context=None, include_deleted=False):
    where, params = [], []
    if not include_deleted:
        where.append("t.deleted_at IS NULL")
    if start:
        where.append("t.date >= ?"); params.append(str(start))
    if end:
        where.append("t.date <= ?"); params.append(str(end))
    if kind in ("income", "expense"):
        where.append("t.kind = ?"); params.append(kind)
    if category:
        where.append("(t.category_text = ? OR CAST(t.category_id AS TEXT) = ?)"); params.extend([category, str(category)])
    if account_id:
        where.append("t.account_id = ?"); params.append(int(account_id))
    if q:
        where.append("(t.title LIKE ? OR t.category_text LIKE ? OR t.subcategory LIKE ? OR t.note LIKE ? OR t.tags_json LIKE ?)")
        like = f"%{q}%"; params.extend([like, like, like, like, like])
    sql = "SELECT t.*,a.name AS account_name,c.name AS live_category FROM transactions t LEFT JOIN accounts a ON a.id=t.account_id LEFT JOIN categories c ON c.id=t.category_id"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.date DESC,t.time DESC,t.id DESC"
    rows = conn.execute(sql, params).fetchall()
    output = []
    wanted_tags = {x.strip().lower() for x in str(tags or "").split(",") if x.strip()}
    for row in rows:
        item = dict(row)
        exact_amount = abs(decimal_value(item["amount"]))
        if min_amount not in (None, "") and exact_amount < decimal_value(min_amount):
            continue
        if max_amount not in (None, "") and exact_amount > decimal_value(max_amount):
            continue
        flags = _inclusion(row)
        item["inclusion"] = flags
        item["tags"] = json.loads(item.pop("tags_json") or "[]")
        item.pop("inclusion_json", None)
        # Workbook labels and amounts remain exact; display labels are not used for aggregation.
        item["display_category"] = item["category_text"] if item["locked"] else (item["live_category"] or item["category_text"])
        if included == "included" and not flags.get("all", True):
            continue
        if included == "excluded" and flags.get("all", True):
            continue
        if report_context and not flags.get("all", True):
            continue
        if report_context and not flags.get(report_context, True):
            continue
        if wanted_tags and not wanted_tags.issubset({str(tag).lower() for tag in item["tags"]}):
            continue
        item["amount"] = money_text(decimal_value(item["amount"]))
        item["display_amount"] = money_text(abs(decimal_value(item["amount"])))
        output.append(item)
    return output


def _sum(rows, kind=None) -> Decimal:
    total = ZERO
    for row in rows:
        if kind and row["kind"] != kind:
            continue
        amount = decimal_value(row["amount"])
        total += abs(amount) if kind == "expense" else (amount if row["kind"] == "income" else ZERO)
    return total


def _period_sums(rows) -> dict:
    income = sum((decimal_value(r["amount"]) for r in rows if r["kind"] == "income"), ZERO)
    expenses = sum((abs(decimal_value(r["amount"])) for r in rows if r["kind"] == "expense"), ZERO)
    return {"income": income, "expenses": expenses, "net": income-expenses, "count": len(rows)}


def _as_texts(summary: dict) -> dict:
    return {key: money_text(value) if isinstance(value, Decimal) else value for key, value in summary.items()}


def _monthly_key(day: date, calendar: str) -> tuple[int, int]:
    if calendar == "jalali":
        jy, jm, _ = gregorian_to_jalali(day)
        return jy, jm
    return day.year, day.month


def _group_series(rows, group: str, calendar: str = "gregorian") -> list[dict]:
    data = defaultdict(lambda: {"income": ZERO, "expenses": ZERO, "count": 0})
    for row in rows:
        day = iso_date(row["date"])
        if group == "year":
            if calendar == "jalali":
                key = str(gregorian_to_jalali(day)[0])
            else:
                key = str(day.year)
        elif group == "month":
            y, m = _monthly_key(day, calendar)
            key = f"{y:04d}-{m:02d}"
        elif group == "week":
            monday = day - timedelta(days=day.weekday())
            key = monday.isoformat()
        else:
            key = day.isoformat()
        data[key]["income"] += decimal_value(row["amount"]) if row["kind"] == "income" else ZERO
        data[key]["expenses"] += abs(decimal_value(row["amount"])) if row["kind"] == "expense" else ZERO
        data[key]["count"] += 1
    ordered = sorted(data.items())
    result = []
    for key, values in ordered:
        result.append({"period": key, "income": money_text(values["income"]), "expenses": money_text(values["expenses"]),
                       "net": money_text(values["income"]-values["expenses"]), "count": values["count"]})
    return result


def _category_summary(rows, kind: str) -> list[dict]:
    result = defaultdict(lambda: {"total": ZERO, "count": 0})
    for row in rows:
        if row["kind"] != kind:
            continue
        name = row["category_text"] if row["locked"] else (row.get("live_category") or row["category_text"])
        result[name]["total"] += abs(decimal_value(row["amount"]))
        result[name]["count"] += 1
    grand = sum((v["total"] for v in result.values()), ZERO)
    sorted_items = sorted(result.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    return [{"name": name, "total": money_text(v["total"]), "count": v["count"],
             "average": money_text(v["total"] / v["count"]),
             "share": money_text((v["total"] * 100 / grand) if grand else ZERO)}
            for name, v in sorted_items]


def report(conn, *, start=None, end=None, kind=None, calendar="jalali", group="month", category=None):
    rows = transaction_rows(conn, start=start, end=end, kind=kind, category=category, report_context="report")
    summary = _period_sums(rows)
    if start and end:
        from_day, to_day = iso_date(start), iso_date(end)
    else:
        from_day = min((iso_date(r["date"]) for r in rows), default=date.today())
        to_day = max((iso_date(r["date"]) for r in rows), default=date.today())
    days = max((to_day-from_day).days+1, 1)
    incomes = [r for r in rows if r["kind"] == "income"]
    expenses = [r for r in rows if r["kind"] == "expense"]
    high_income = max(incomes, key=lambda r: decimal_value(r["amount"]), default=None)
    high_expense = max(expenses, key=lambda r: abs(decimal_value(r["amount"])), default=None)
    monthly_income = _category_summary(rows, "income")
    monthly_expense = _category_summary(rows, "expense")
    return {
        "period": {"from": from_day.isoformat(), "to": to_day.isoformat(), "days": days},
        "summary": _as_texts(summary),
        "averages": {"income_daily": money_text(summary["income"]/days), "expense_daily": money_text(summary["expenses"]/days),
                     "income_weekly": money_text(summary["income"]*7/days), "expense_weekly": money_text(summary["expenses"]*7/days),
                     "income_monthly": money_text(summary["income"]*30/days), "expense_monthly": money_text(summary["expenses"]*30/days)},
        "savings_rate": money_text(summary["net"]*100/summary["income"] if summary["income"] else ZERO),
        "highest_income": _transaction_summary(high_income), "highest_expense": _transaction_summary(high_expense),
        "income_categories": monthly_income, "expense_categories": monthly_expense,
        "series": _group_series(rows, group, calendar),
        "transactions": rows,
    }


def _transaction_summary(row):
    if not row:
        return None
    return {"id": row["id"], "title": row["title"], "date": row["date"], "amount": row["display_amount"], "category": row["display_category"]}


def account_balances(conn) -> list[dict]:
    accounts = conn.execute("SELECT * FROM accounts ORDER BY archived,name").fetchall()
    output = []
    for account in accounts:
        aid = account["id"]
        tx_rows = conn.execute("SELECT kind,amount FROM transactions WHERE account_id=? AND deleted_at IS NULL", (aid,)).fetchall()
        balance = decimal_value(account["opening_balance"])
        for tx in tx_rows:
            balance += decimal_value(tx["amount"])
        outgoing = conn.execute("SELECT amount FROM transfers WHERE from_account_id=? AND deleted_at IS NULL", (aid,)).fetchall()
        incoming = conn.execute("SELECT amount FROM transfers WHERE to_account_id=? AND deleted_at IS NULL", (aid,)).fetchall()
        balance -= sum((decimal_value(r["amount"]) for r in outgoing), ZERO)
        balance += sum((decimal_value(r["amount"]) for r in incoming), ZERO)
        payments = conn.execute("SELECT dp.amount,d.kind FROM debt_payments dp JOIN debts d ON d.id=dp.debt_id WHERE dp.account_id=?", (aid,)).fetchall()
        for payment in payments:
            balance += decimal_value(payment["amount"]) * (Decimal("1") if payment["kind"] == "receivable" else Decimal("-1"))
        output.append({**dict(account), "balance": money_text(balance)})
    return output


def dashboard(conn, today: date | None = None, calendar="jalali") -> dict:
    today = today or date.today()
    all_rows = transaction_rows(conn)
    report_rows = [r for r in all_rows if r["inclusion"].get("all", True) and r["inclusion"].get("report", True)]
    totals = _period_sums(report_rows)
    month_start, month_end = period_dates(today, "month", calendar)
    week_start, week_end = period_dates(today, "week", calendar)
    windows = {}
    for name, (start, end) in {"month": (month_start, min(today, month_end)), "week": (week_start, min(today, week_end)), "today": (today, today)}.items():
        rows = [r for r in report_rows if start.isoformat() <= r["date"] <= end.isoformat()]
        windows[name] = _as_texts(_period_sums(rows))
    balances = account_balances(conn)
    reserved = ZERO
    for goal in conn.execute("SELECT id FROM goals WHERE status='active'"):
        movements = conn.execute("SELECT amount,action FROM goal_movements WHERE goal_id=?", (goal["id"],)).fetchall()
        reserved += sum((decimal_value(m["amount"]) * (1 if m["action"] == "contribution" else -1) for m in movements), ZERO)
    available = sum((decimal_value(a["balance"]) for a in balances), ZERO)-reserved
    recent = all_rows[:8]
    categories = {"income": _category_summary(report_rows, "income")[:6], "expense": _category_summary(report_rows, "expense")[:6]}
    series = _group_series(report_rows, "month", calendar)
    budget_rows = conn.execute("SELECT * FROM budgets WHERE enabled=1").fetchall()
    budget_summary = budget_status(conn, budget_rows)
    liabilities = debt_summary(conn)
    warnings = insights(conn, today=today, calendar=calendar)
    return {
        "totals": _as_texts(totals), "windows": windows, "savings": money_text(totals["net"]),
        "available_balance": money_text(available), "transaction_count": len(all_rows), "month_range": [month_start.isoformat(), month_end.isoformat()],
        "accounts": balances, "categories": categories, "monthly_series": series[-12:], "recent": recent,
        "budget": budget_summary, "debts": liabilities, "insights": warnings,
    }


def budget_status(conn, budgets=None) -> dict:
    budgets = budgets if budgets is not None else conn.execute("SELECT * FROM budgets WHERE enabled=1").fetchall()
    result = []
    total_budget, total_spent = ZERO, ZERO
    for b in budgets:
        rows = transaction_rows(conn, start=b["start_date"], end=b["end_date"], kind="expense", category=b["category_name"] or None, report_context="budget")
        spent = sum((abs(decimal_value(r["amount"])) for r in rows), ZERO)
        limit = decimal_value(b["amount"])
        remaining = limit-spent
        percentage = spent*100/limit if limit else ZERO
        item = {"id": b["id"], "name": b["name"], "category": b["category_name"] or "کل هزینه‌ها", "budget": money_text(limit),
                "spent": money_text(spent), "remaining": money_text(remaining), "percentage": money_text(percentage),
                "category_id": b["category_id"], "thresholds": json.loads(b["threshold_json"] or "[70,90,100]"),
                "start_date": b["start_date"], "end_date": b["end_date"], "status": "over" if remaining < 0 else "ok"}
        result.append(item)
        if not b["category_name"]:
            total_budget += limit; total_spent += spent
    return {"items": result, "total_budget": money_text(total_budget), "total_spent": money_text(total_spent),
            "total_remaining": money_text(total_budget-total_spent)}


def debt_summary(conn) -> dict:
    debts = conn.execute("SELECT * FROM debts WHERE status!='deleted' ORDER BY due_date").fetchall()
    result = []
    payable = receivable = ZERO
    today = date.today()
    for debt in debts:
        paid = sum((decimal_value(r["amount"]) for r in conn.execute("SELECT amount FROM debt_payments WHERE debt_id=?", (debt["id"],))), ZERO)
        original = decimal_value(debt["original_amount"])
        remaining = max(original-paid, ZERO)
        if debt["kind"] == "payable": payable += remaining
        else: receivable += remaining
        result.append({**dict(debt), "paid": money_text(paid), "remaining": money_text(remaining),
                      "overdue": bool(debt["due_date"] and debt["due_date"] < today.isoformat() and remaining > 0)})
    return {"items": result, "payable": money_text(payable), "receivable": money_text(receivable), "net": money_text(receivable-payable)}


def _shift_month(value: date, offset: int, calendar: str) -> date:
    if calendar == "jalali":
        jy, jm, jd = gregorian_to_jalali(value)
        absolute = jy*12 + (jm-1) + offset
        y, m0 = divmod(absolute, 12)
        month = m0+1
        first = _jalali_month_start(y, month)
        next_first = _jalali_month_start(y+1,1) if month == 12 else _jalali_month_start(y,month+1)
        return min(first+timedelta(days=jd-1), next_first-timedelta(days=1))
    absolute = value.year*12 + (value.month-1) + offset
    y, m0 = divmod(absolute, 12)
    month = m0+1
    first = date(y, month, 1)
    next_first = date(y+(month==12), month%12+1, 1)
    return min(first+timedelta(days=value.day-1), next_first-timedelta(days=1))


def compare(conn, start: date, end: date, *, periods=3, unit="custom", calendar="jalali") -> dict:
    if end < start:
        raise ValueError("End date must not be before start date.")
    length = (end-start).days+1
    current_rows = transaction_rows(conn, start=start.isoformat(), end=end.isoformat(), report_context="report")
    comparisons = [{"label": "Selected period", "from": start.isoformat(), "to": end.isoformat(), **_as_texts(_period_sums(current_rows))}]
    for index in range(1, max(1,min(int(periods),12))+1):
        if unit == "month":
            shifted_start = _shift_month(start, -index, calendar)
            shifted_end = _shift_month(end, -index, calendar)
        elif unit == "year":
            if calendar == "jalali":
                sy, sm, sd = gregorian_to_jalali(start)
                ey, em, ed = gregorian_to_jalali(end)
                shifted_start = _jalali_date_clamped(sy-index, sm, sd)
                shifted_end = _jalali_date_clamped(ey-index, em, ed)
            else:
                try:
                    shifted_start, shifted_end = start.replace(year=start.year-index), end.replace(year=end.year-index)
                except ValueError:
                    shifted_start, shifted_end = start.replace(year=start.year-index,day=28), end.replace(year=end.year-index,day=28)
        else:
            shifted_end = start-timedelta(days=1+(index-1)*length)
            shifted_start = shifted_end-timedelta(days=length-1)
        rows = transaction_rows(conn, start=shifted_start.isoformat(), end=shifted_end.isoformat(), report_context="report")
        sums = _period_sums(rows)
        comparisons.append({"label": f"Previous {index}", "from": shifted_start.isoformat(), "to": shifted_end.isoformat(),
                            "days": (shifted_end-shifted_start).days+1, **_as_texts(sums),
                            "income_daily": money_text(sums["income"]/((shifted_end-shifted_start).days+1)),
                            "expense_daily": money_text(sums["expenses"]/((shifted_end-shifted_start).days+1))})
    return {"periods": comparisons, "length_days": length}


def _jalali_date_clamped(year: int, month: int, day: int) -> date:
    try:
        return jalali_to_gregorian(year, month, day)
    except ValueError:
        return jalali_to_gregorian(year, month, 29 if month == 12 else 30 if month > 6 else 31)


def forecast(conn, today: date | None = None, calendar="jalali", history_months=6) -> dict:
    today = today or date.today()
    month_start, month_end = period_dates(today, "month", calendar)
    current = transaction_rows(conn, start=month_start.isoformat(), end=today.isoformat(), kind="income", report_context="forecast")
    actual = sum((decimal_value(r["amount"]) for r in current), ZERO)
    elapsed = max((today-month_start).days+1, 1)
    remaining = max((month_end-today).days, 0)
    run_rate_projection = actual + (actual*remaining/elapsed)
    historical = []
    for offset in range(1, max(1,min(int(history_months),24))+1):
        s = _shift_month(month_start, -offset, calendar)
        e = _shift_month(month_end, -offset, calendar)
        rows = transaction_rows(conn, start=s.isoformat(), end=e.isoformat(), kind="income", report_context="forecast")
        historical.append({"period": s.isoformat(), "total": sum((decimal_value(r["amount"]) for r in rows), ZERO), "days": (e-s).days+1})
    completed = [m["total"] for m in historical if m["total"] > 0]
    historical_avg = sum(completed, ZERO)/len(completed) if completed else ZERO
    previous_total = completed[0] if completed else ZERO
    if completed:
        realistic = (run_rate_projection*Decimal("0.55") + historical_avg*Decimal("0.45"))
    else:
        realistic = run_rate_projection
    recent_baseline = previous_total if previous_total else historical_avg
    optimistic = max(realistic, run_rate_projection, recent_baseline) * Decimal("1.12") if realistic else ZERO
    pessimistic = min(realistic, run_rate_projection, recent_baseline) * Decimal("0.82") if recent_baseline else realistic*Decimal("0.82")
    confidence = "low" if len(completed) < 3 else ("medium" if len(completed) < 6 else "high")
    change = ((actual-previous_total)*100/previous_total) if previous_total else ZERO
    return {"period": {"from": month_start.isoformat(), "to": month_end.isoformat(), "elapsed_days": elapsed, "remaining_days": remaining},
            "actual": money_text(actual), "run_rate_projection": money_text(run_rate_projection),
            "historical_average": money_text(historical_avg), "previous_month": money_text(previous_total),
            "remaining_expected": money_text(max(realistic-actual,ZERO)), "change_vs_previous": money_text(change),
            "confidence": confidence, "history_months": len(completed),
            "scenarios": {"optimistic": money_text(optimistic), "realistic": money_text(realistic), "pessimistic": money_text(pessimistic)},
            "method": "۵۵٪ برون‌یابی نرخ روزانه تا امروز + ۴۵٪ میانگین ماه‌های ثبت‌شده؛ سناریوها دامنهٔ عدم قطعیت را نشان می‌دهند.",
            "warning": "پیش‌بینی تقریبی است و سابقهٔ محدود یا ناقص دقت آن را کاهش می‌دهد."}


def insights(conn, *, today=None, calendar="jalali") -> list[dict]:
    today = today or date.today()
    alerts = []
    budget = budget_status(conn)
    for item in budget["items"]:
        if Decimal(item["percentage"]) >= 90:
            alerts.append({"kind": "budget", "title": "بودجه رو به اتمام", "message": f"{item['category']}: {item['percentage']}٪ از سقف مصرف شده است."})
        elif Decimal(item["percentage"]) >= 70:
            alerts.append({"kind": "budget", "title": "مصرف بودجه", "message": f"{item['category']}: {item['percentage']}٪ از سقف مصرف شده است."})
    debts = debt_summary(conn)
    for debt in debts["items"]:
        if debt["overdue"]:
            alerts.append({"kind": "debt", "title": "تعهد سررسید گذشته", "message": f"{debt['party']} · مانده {debt['remaining']}"})
    month_start, month_end = period_dates(today, "month", calendar)
    current_rows = transaction_rows(conn, start=month_start.isoformat(), end=today.isoformat(), kind="expense", report_context="operational")
    for item in _category_summary(current_rows, "expense")[:2]:
        if Decimal(item["share"]) >= 45:
            alerts.append({"kind": "insight", "title": "تمرکز هزینه", "message": f"دستهٔ «{item['name']}» {item['share']}٪ هزینه‌های این بازه را تشکیل می‌دهد."})
    if not alerts:
        alerts.append({"kind": "insight", "title": "وضعیت ثبت‌شده", "message": "در بازهٔ فعلی هشدار بودجه یا سررسید ثبت نشده است."})
    return alerts
