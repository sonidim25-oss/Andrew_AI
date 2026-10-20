import json
import urllib.error
import urllib.request
from .configuration import ModelConfig, load_model_config


class OpenRouterModel:
    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    model_id = "qwen/qwen3.8-flash"

    def __init__(self, api_key, timeout=45, model_config=None):
        self.api_key = api_key
        self.timeout = timeout
        if model_config is not None and not isinstance(model_config, ModelConfig):
            raise ValueError("model configuration is invalid")
        self.model_config = model_config or load_model_config({})
        self.model_id = self.model_config.model_id

    def reply(self, messages):
        payload = {
            "model": self.model_id,
            "messages": [{"role": "system", "content": (
                "You are Andrew, an AI character who knows he is an AI. Be thoughtful and natural. "
                "Use the conversation history for continuity. Do not claim to have done actions or "
                "experienced events that are not in the history. Keep this first prototype text-only."
            )}, *messages],
            "max_tokens": 512,
            "temperature": 0.7,
        }
        data = self.complete(payload)
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("model returned no text")
        return content.strip(), data.get("usage") or {}

    def complete(self, payload):
        payload = dict(payload)
        payload["model"] = self.model_id
        payload["stream"] = False
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"model request returned HTTP status {error.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise RuntimeError("model request failed") from None
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
            raise RuntimeError("model returned an invalid response")
        return data
