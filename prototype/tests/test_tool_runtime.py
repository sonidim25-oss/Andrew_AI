import importlib
import importlib.util
import unittest


def runtime_module(case):
    case.assertIsNotNone(importlib.util.find_spec("andrewbot.tool_runtime"), "tool runtime is implemented")
    return importlib.import_module("andrewbot.tool_runtime")


class FakeRunner:
    def __init__(self, result=None):
        self.requests = []
        self.result = result or {"status": "ok", "stdout": "done", "stderr": "", "artifacts": [], "safe_error": None}

    def run(self, request):
        self.requests.append(request)
        return self.result


class ToolRuntimeTests(unittest.TestCase):
    def test_disabled_runtime_rejects_before_runner_call(self):
        module = runtime_module(self)
        runner = FakeRunner()
        runtime = module.ToolRuntime(runner, module.ToolPolicy(enabled=False))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            runtime.execute("python.run", {"code": "print(1)"})
        self.assertEqual(runner.requests, [])

    def test_python_maps_to_runner_request_and_marks_output_untrusted(self):
        module = runtime_module(self)
        runner = FakeRunner()
        runtime = module.ToolRuntime(runner, module.ToolPolicy(enabled=True))
        output = runtime.execute("python.run", {"code": "print(1)"})
        self.assertEqual(runner.requests, [{"type": "python", "code": "print(1)"}])
        self.assertIn("untrusted", output.lower())
        self.assertIn("done", output)

    def test_rejects_extra_arguments_without_runner_call(self):
        module = runtime_module(self)
        runner = FakeRunner()
        runtime = module.ToolRuntime(runner, module.ToolPolicy(enabled=True))
        with self.assertRaises(ValueError):
            runtime.execute("node.run", {"code": "1", "env": {"SECRET": "x"}})
        self.assertEqual(runner.requests, [])

    def test_browser_requires_https_url(self):
        module = runtime_module(self)
        runner = FakeRunner()
        runtime = module.ToolRuntime(runner, module.ToolPolicy(enabled=True))
        with self.assertRaises(ValueError):
            runtime.execute("browser.open", {"url": "file:///etc/passwd"})
        self.assertEqual(runner.requests, [])

    def test_browser_records_sanitized_visit_before_returning_content(self):
        module = runtime_module(self)
        runner = FakeRunner({"status": "ok", "stdout": "page text", "stderr": "", "artifacts": [],
                             "safe_error": None, "visit": {"url": "https://example.com/path?secret=1", "title": "Example"}})
        visits = []
        runtime = module.ToolRuntime(runner, module.ToolPolicy(enabled=True),
                                     on_visit=lambda url, title: visits.append((url, title)))
        result = runtime.execute("browser.open", {"url": "https://example.com"})
        self.assertEqual(visits, [("https://example.com/path?secret=1", "Example")])
        self.assertIn("page text", result)

    def test_browser_hides_result_when_history_recording_fails(self):
        module = runtime_module(self)
        runner = FakeRunner({"status": "ok", "stdout": "sensitive page", "stderr": "", "artifacts": [],
                             "safe_error": None, "visit": {"url": "https://example.com", "title": "Example"}})
        def fail(*_args):
            raise OSError("disk full")
        runtime = module.ToolRuntime(runner, module.ToolPolicy(enabled=True), on_visit=fail)
        with self.assertRaisesRegex(ValueError, "history storage"):
            runtime.execute("browser.open", {"url": "https://example.com"})


if __name__ == "__main__":
    unittest.main()
