import tempfile
import unittest
from pathlib import Path

from andrewbot.configuration import MAX_LETTA_OUTPUT_TOKENS, load_model_config
from andrewbot.controller import Controller
from andrewbot.store import Store


class ModelConfigurationTests(unittest.TestCase):
    def test_model_configuration_defaults_to_current_qwen_and_rates(self):
        config = load_model_config({})
        self.assertEqual(config.model_id, "qwen/qwen3.8-flash")
        self.assertEqual(config.input_usd_per_million, 0.15)
        self.assertEqual(config.output_usd_per_million, 0.47)

    def test_owner_configuration_selects_model_and_both_rates_together(self):
        config = load_model_config({
            "ANDREW_MODEL_ID": "google/gemini-2.5-flash",
            "ANDREW_MODEL_INPUT_USD_PER_MILLION": "0.25",
            "ANDREW_MODEL_OUTPUT_USD_PER_MILLION": "1.5",
        })
        self.assertEqual(config.model_id, "google/gemini-2.5-flash")
        self.assertEqual(config.input_usd_per_million, 0.25)
        self.assertEqual(config.output_usd_per_million, 1.5)

    def test_invalid_model_identifiers_and_rates_are_rejected(self):
        for environment in (
            {"ANDREW_MODEL_ID": ""},
            {"ANDREW_MODEL_ID": "provider/model with spaces"},
            {"ANDREW_MODEL_ID": "provider/model/extra"},
            {"ANDREW_MODEL_INPUT_USD_PER_MILLION": "-1"},
            {"ANDREW_MODEL_OUTPUT_USD_PER_MILLION": "nan"},
            {"ANDREW_MODEL_OUTPUT_USD_PER_MILLION": "inf"},
        ):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                load_model_config(environment)

    def test_custom_model_requires_both_explicit_prices(self):
        for environment in (
            {"ANDREW_MODEL_ID": "google/gemini-2.5-flash"},
            {
                "ANDREW_MODEL_ID": "google/gemini-2.5-flash",
                "ANDREW_MODEL_INPUT_USD_PER_MILLION": "0.25",
            },
            {"ANDREW_MODEL_INPUT_USD_PER_MILLION": "0.25"},
        ):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                load_model_config(environment)

    def test_controller_request_estimate_uses_configured_input_and_output_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "controller.sqlite3")
            try:
                config = load_model_config({
                    "ANDREW_MODEL_ID": "google/gemini-2.5-flash",
                    "ANDREW_MODEL_INPUT_USD_PER_MILLION": "2",
                    "ANDREW_MODEL_OUTPUT_USD_PER_MILLION": "4",
                })
                controller = Controller(store, object(), 17, model_config=config)
                estimate = controller._estimate_cost([{"role": "user", "content": "hi"}])
                self.assertAlmostEqual(estimate, (2 * 2 + MAX_LETTA_OUTPUT_TOKENS * 4) / 1_000_000)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
