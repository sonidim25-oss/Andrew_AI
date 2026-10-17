import math
import re
from dataclasses import dataclass
from typing import Mapping


# Used only for provisional cost accounting when provider usage is missing.
# No output token cap is sent to the provider.
OUTPUT_TOKEN_COST_ESTIMATE = 6000


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}")


@dataclass(frozen=True)
class ModelConfig:
    model_id: str
    input_usd_per_million: float
    output_usd_per_million: float


def _rate(environment, name, default):
    raw = environment.get(name)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise ValueError("model pricing configuration is invalid")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("model pricing configuration is invalid") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError("model pricing configuration is invalid")
    return value


def load_model_config(environment: Mapping[str, str]) -> ModelConfig:
    if not isinstance(environment, Mapping):
        raise ValueError("model configuration is invalid")
    model_id = environment.get("ANDREW_MODEL_ID", "qwen/qwen3.8-flash")
    if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
        raise ValueError("model identifier configuration is invalid")
    input_name = "ANDREW_MODEL_INPUT_USD_PER_MILLION"
    output_name = "ANDREW_MODEL_OUTPUT_USD_PER_MILLION"
    input_configured = input_name in environment
    output_configured = output_name in environment
    if input_configured != output_configured or (
        model_id != "qwen/qwen3.8-flash" and not (input_configured and output_configured)
    ):
        raise ValueError("model pricing configuration must specify both rates for the selected model")
    return ModelConfig(
        model_id=model_id,
        input_usd_per_million=_rate(environment, "ANDREW_MODEL_INPUT_USD_PER_MILLION", 0.15),
        output_usd_per_million=_rate(environment, "ANDREW_MODEL_OUTPUT_USD_PER_MILLION", 0.47),
    )
