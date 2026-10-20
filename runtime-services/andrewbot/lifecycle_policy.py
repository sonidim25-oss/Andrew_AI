"""Release approval is scoped to conversation and memory, with owner opt-in."""
import math
from dataclasses import dataclass
from .runtime_readiness import validate_runtime_proof


TEXT_MEMORY_LIFECYCLE_READY = True


def enabled(environment, name):
    return environment.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LifecyclePolicy:
    autonomous_enabled: bool
    fallback_enabled: bool
    monthly_limit_usd: float
    monthly_reserve_usd: float
    daily_target_usd: float

    def budget_arguments(self):
        return {name: getattr(self, name) for name in (
            "monthly_limit_usd", "monthly_reserve_usd", "daily_target_usd")}


def load_lifecycle_policy(environment):
    autonomous = enabled(environment, "ANDREW_AUTONOMOUS_ENABLED")
    if autonomous and not TEXT_MEMORY_LIFECYCLE_READY:
        raise ValueError("conversation and memory lifecycle release is not ready")
    if enabled(environment, "ANDREW_ISOLATED_RUNTIME_ENABLED"):
        validate_runtime_proof()
    try:
        monthly = float(environment.get("ANDREW_MONTHLY_LIMIT_USD", "10"))
        reserve = float(environment.get("ANDREW_MONTHLY_RESERVE_USD", "1"))
        daily = float(environment.get("ANDREW_DAILY_TARGET_USD", "0.30"))
    except (TypeError, ValueError):
        raise ValueError("budget settings must be finite numbers") from None
    if not all(math.isfinite(value) for value in (monthly, reserve, daily)):
        raise ValueError("budget settings must be finite numbers")
    if not (0 < monthly <= 10 and 1 <= reserve <= monthly and 0 < daily <= 0.30):
        raise ValueError("budget settings exceed the approved lifecycle limits")
    return LifecyclePolicy(autonomous, autonomous and enabled(environment, "ANDREW_FALLBACK_WAKE_ENABLED"),
                           monthly, reserve, daily)
