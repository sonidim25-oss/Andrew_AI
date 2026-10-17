from datetime import datetime, timezone
from zoneinfo import ZoneInfo


TORONTO = ZoneInfo("America/Toronto")


def budget_snapshot(store, now=None, monthly_limit_usd=10.0, monthly_reserve_usd=1.0,
                    daily_target_usd=0.30):
    """Report spending for Andrew to manage; legacy limit arguments are ignored."""
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise ValueError("budget time must be timezone-aware")
    local = current.astimezone(TORONTO)
    month = local.strftime("%Y-%m")
    day = local.strftime("%Y-%m-%d")
    totals = store.usage_totals(month, day)
    monthly_spend = float(totals["monthly_cost"])
    daily_spend = float(totals["daily_cost"])
    return {
        "month": month,
        "day": day,
        "monthly_cost": monthly_spend,
        "daily_cost": daily_spend,
        "spending_policy": "agent_managed_api_credits",
        "monthly_limit": None,
        "monthly_reserve": 0.0,
        "ordinary_ceiling": None,
        "accrued_allowance": None,
        "remaining_ordinary": None,
        "remaining_daily_target": None,
    }
