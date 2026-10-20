from datetime import datetime, timezone
from zoneinfo import ZoneInfo


TORONTO = ZoneInfo("America/Toronto")


def budget_snapshot(store, now=None, monthly_limit_usd=10.0, monthly_reserve_usd=1.0,
                    daily_target_usd=0.30):
    """Return the Toronto-calendar budget available to ordinary model work."""
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise ValueError("budget time must be timezone-aware")
    local = current.astimezone(TORONTO)
    month = local.strftime("%Y-%m")
    day = local.strftime("%Y-%m-%d")
    totals = store.usage_totals(month, day)
    ordinary_ceiling = max(0.0, float(monthly_limit_usd) - float(monthly_reserve_usd))
    accrued_allowance = min(ordinary_ceiling, local.day * float(daily_target_usd))
    monthly_spend = float(totals["monthly_cost"])
    daily_spend = float(totals["daily_cost"])
    return {
        "month": month,
        "day": day,
        "monthly_cost": monthly_spend,
        "daily_cost": daily_spend,
        "monthly_limit": float(monthly_limit_usd),
        "monthly_reserve": float(monthly_reserve_usd),
        "ordinary_ceiling": ordinary_ceiling,
        "accrued_allowance": accrued_allowance,
        "remaining_ordinary": max(0.0, min(ordinary_ceiling, accrued_allowance) - monthly_spend),
        "remaining_daily_target": max(0.0, float(daily_target_usd) - daily_spend),
    }
