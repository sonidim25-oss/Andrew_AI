"""Controller-side validation for isolated runtime and browser tools."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from .runner_client import RunnerProtocolError
from .runner_format import prepare_runner_request, format_runner_response


MAX_CODE_BYTES = 64 * 1024
MAX_COMMAND_BYTES = 64 * 1024
MAX_RESULT_CHARS = 15_000


@dataclass(frozen=True)
class ToolPolicy:
    enabled: bool = False


class ToolRuntime:
    def __init__(self, runner, policy: ToolPolicy, on_visit=None, browser_tools=None):
        self.runner = runner
        self.policy = policy
        self.on_visit = on_visit
        self.browser_tools = browser_tools

    def execute(self, name: str, arguments: dict) -> str:
        if not self.policy.enabled:
            raise ValueError("isolated runtime is unavailable")
        if name in {"browser.open", "browser.control", "browser.account"} and self.browser_tools:
            return self.browser_tools.execute(name, arguments)
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        if name in {"python.run", "node.run", "shell.run"}:
            request = prepare_runner_request(name, arguments)
        elif name == "browser.open":
            if self.on_visit is None:
                raise ValueError("browser history recording is unavailable")
            if set(arguments) != {"url"} or not isinstance(arguments["url"], str):
                raise ValueError("url must be a string")
            parsed = urlsplit(arguments["url"])
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("browser URL must be an absolute HTTPS URL")
            request = {"type": "browser_open", "url": arguments["url"]}
        else:
            raise ValueError("unsupported runtime action")
        try:
            response = self.runner.run(request)
        except RunnerProtocolError:
            raise ValueError("isolated runtime request failed") from None
        if name == "browser.open" and response["status"] == "ok":
            visit = response.get("visit")
            if not visit:
                raise ValueError("browser visit could not be recorded")
            try:
                self.on_visit(visit["url"], visit["title"])
            except Exception:
                raise ValueError("browser history storage is unavailable") from None
        return format_runner_response(response)
