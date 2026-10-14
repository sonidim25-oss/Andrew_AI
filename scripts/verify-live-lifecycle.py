#!/usr/bin/env python3
"""Two separate commissioning processes, using the real gateway, agent and diary outbox."""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from andrewbot.appserver import LettaAgent
from andrewbot.controller import Controller
from andrewbot.diary import deliver_pending_diary
from andrewbot.gateway import GatewayThread, ModelGateway, make_server
from andrewbot.lifecycle_policy import load_lifecycle_policy
from andrewbot.main import systemd_credential, vault_directory
from andrewbot.memory import MemoryService
from andrewbot.model import OpenRouterModel
from andrewbot.configuration import load_model_config
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store
from andrewbot.telegram import TelegramAPI
from andrewbot.vault import Vault


ROOT = Path("/var/lib/andrew/control")
EVIDENCE = ROOT / "lifecycle-validation.json"


class ValidationFailure(Exception):
    """Contains only a locally written, credential-free check name."""


def require(condition, message):
    if not condition:
        raise ValidationFailure(message)


def ledger(store):
    return [list(row) for row in store.connection.execute(
        "SELECT id,cost_usd,input_tokens,output_tokens,created_at FROM usage ORDER BY id")]


def write_evidence(report):
    temporary = EVIDENCE.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(report, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, EVIDENCE)


def run(stage):
    require(stage in {"prepare", "resume"}, "Expected prepare or resume")
    policy = load_lifecycle_policy(os.environ)
    require(policy.autonomous_enabled and policy.fallback_enabled, "Lifecycle opt-in is missing")
    store = Store(ROOT / "controller.sqlite3")
    gateway_thread = None
    try:
        require(store.get_state("paused", "0") != "1", "Owner pause is active; activation refused")
        require(not store.get_state("active_session_id", ""), "An existing session needs controller recovery first")
        require(store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity failed")
        require(store.connection.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0,
                "Uncertain spending reservations require review before commissioning")
        vault = Vault(vault_directory())
        try:
            provider_key = vault.get_secret("openrouter_api")
            telegram = TelegramAPI(vault.get_secret("telegram_bot"))
        finally:
            vault.close()
        token = systemd_credential("andrew-gateway-token")
        config = load_model_config(os.environ)
        gateway = ModelGateway(OpenRouterModel(provider_key, model_config=config), store, token,
                               memory_service=MemoryService(store), model_config=config, **policy.budget_arguments())
        gateway_thread = GatewayThread(make_server(gateway, port=8284))
        gateway_thread.start()
        agent = LettaAgent(store, token, model_config=config)
        agent.initialize()
        controller = Controller(store, agent, int(os.environ["ANDREW_OWNER_ID"]), autonomous_enabled=True,
                                fallback_enabled=True, memory_service=MemoryService(store),
                                model_config=config, **policy.budget_arguments())
        agent.tool_handler = controller.execute_tool
        runner = SessionRunner(store, controller, agent, enabled=True)
        runner.recover()
        now = datetime.now(timezone.utc)
        day = now.astimezone(ZoneInfo("America/Toronto")).date().isoformat()
        if stage == "prepare":
            require(not EVIDENCE.exists(), "Commissioning evidence already exists; inspect it before repeating")
            # Exercise the real fallback code at the current minute; production remains 09:00 Toronto.
            controller.fallback_wake_time = now.astimezone(ZoneInfo("America/Toronto")).strftime("%H:%M")
            report = {"started_at": now.isoformat(), "usage_before": ledger(store),
                      "agent_id": agent.agent_id, "conversation_id": agent.conversation_id,
                      "cycles_before": store.cycle_count(day)}
            require(controller.check_fallback_wake(now), "Existing lifecycle state prevents a fallback wake")
            require(not controller.check_fallback_wake(now), "Fallback wake was duplicated")
            runner.SESSION_PROMPT += (
                " The owner is commissioning this lifecycle. For this first validation session, save today's diary "
                "if missing, then call sleep with minutes=1. Use the tools to complete these actions now."
            )
        else:
            report = json.loads(EVIDENCE.read_text())
            require(report.get("prepare") == "passed", "First commissioning stage did not pass")
            require(agent.agent_id == report["agent_id"], "Persistent agent changed")
            require(agent.conversation_id == report["conversation_id_after"], "Saved conversation changed")
            require(store.diary_entry(day)["entry_id"] == report["diary_id"], "Diary changed after process restart")
            require(store.get_state("sleeping") == "1" and store.get_state("sleep_until") == report["sleep_until"],
                    "Timed sleep did not survive restart")
            require(ledger(store) == report["usage_after_prepare"], "Settled ledger changed across restart")
            due = datetime.fromisoformat(report["sleep_until"])
            require((due - datetime.now(timezone.utc)).total_seconds() <= 90, "Probe wake exceeds its observation window")
            while datetime.now(timezone.utc) < due:
                time.sleep(min(1, max(0.01, (due - datetime.now(timezone.utc)).total_seconds())))
            require(controller.check_scheduled_wake(), "Scheduled wake did not fire")
            require(not controller.check_scheduled_wake(), "Scheduled wake fired twice")
            runner.SESSION_PROMPT += (
                " This is the second and last commissioning session. Today's diary is already saved. "
                "Call wake_at with time=09:00 now, so your next independent session is tomorrow morning in Toronto."
            )
        for _ in range(3):
            require(runner.tick(), "Autonomous cycle failed or was blocked")
            if store.get_state("sleeping") == "1":
                break
        require(store.get_state("sleeping") == "1" and not runner.active_session_id, "Agent did not choose sleep")
        entry = store.diary_entry(day)
        require(entry is not None, "Agent did not save a diary")
        due = datetime.fromisoformat(store.get_state("sleep_until"))
        require(due > datetime.now(timezone.utc), "Next wake is not in the future")
        if stage == "prepare":
            require((due - datetime.now(timezone.utc)).total_seconds() <= 65, "Agent did not request the one-minute test sleep")
        else:
            local_due = due.astimezone(ZoneInfo("America/Toronto"))
            require((local_due.hour, local_due.minute) == (9, 0), "Agent did not schedule the final 09:00 wake")
        deliver_pending_diary(telegram, store, controller.owner_id)
        require(store.diary_delivery_state(entry["entry_id"]) == "delivered", "Diary Telegram delivery is unconfirmed")
        status, _ = gateway.complete(f"Bearer {token}", {"model": config.model_id, "messages": [{"role": "user", "content": "blocked sleep probe"}]})
        require(status == 423, "Sleeping did not block the model gateway")
        settled = ledger(store)
        require(settled[:len(report["usage_before"])] == report["usage_before"], "Existing spending rows changed")
        stage_before = report["usage_before"] if stage == "prepare" else report["usage_after_prepare"]
        require(len(settled) > len(stage_before), "No new live spending settlement was recorded in this stage")
        require(all(row[1] > 0 and row[2] > 0 and row[3] > 0 for row in settled[len(report["usage_before"]):]),
                "Live spending rows lack positive cost or token counts")
        require(store.connection.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0,
                "Unsettled spending remains; commissioning stopped")
        report.update({stage: "passed", "diary_id": entry["entry_id"], "diary_delivery": "delivered",
                       "conversation_id_after": agent.conversation_id, "sleep_until": due.isoformat(timespec="seconds"),
                       "usage_after_prepare" if stage == "prepare" else "usage_after_resume": settled,
                       "cycles_after_" + stage: store.cycle_count(day)})
        write_evidence(report)
        new = settled[len(report["usage_before"]):]
        print(json.dumps({"stage": stage, "result": "passed", "diary_delivery": "delivered",
                          "new_settled_charges": len(new), "new_cost_usd": sum(row[1] for row in new),
                          "unsettled_reservations": 0, "next_wake_utc": due.isoformat(timespec="seconds")}))
    finally:
        if gateway_thread:
            gateway_thread.close()
        store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run(sys.argv[1] if len(sys.argv) == 2 else "")
    except ValidationFailure as error:
        sys.exit("Live lifecycle validation failed: " + str(error))
    except Exception as error:
        sys.exit("Live lifecycle validation failed (" + type(error).__name__ + ")")
