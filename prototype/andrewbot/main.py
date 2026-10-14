import logging
import os
import sys
import signal
import time
from pathlib import Path

from .controller import Controller
from .appserver import LettaAgent, StartupValidationError
from .diary import deliver_pending_diary
from .gateway import GatewayThread, ModelGateway, make_server
from .model import OpenRouterModel
from .memory import MemoryService
from .memory_backup import PeriodicMemoryBackup
from .configuration import load_model_config
from .lifecycle_policy import load_lifecycle_policy
from .sessions import SessionRunner
from .store import Store
from .telegram import OwnerRateLimit, TelegramAPI
from .vault import Vault
from .browser_history import VisitHistory
from .runner_client import RunnerClient
from .tool_runtime import ToolPolicy, ToolRuntime
from .browser_client import BrowserClient
from .browser_tools import BrowserTools
from .account_broker import StoredAccountBroker


def data_directory():
    return Path(os.environ.get("ANDREW_DATA_DIR", Path.home() / ".local/state/andrew"))


def vault_directory():
    return Path(os.environ.get("ANDREW_VAULT_DIR", data_directory() / "vault"))


def systemd_credential(name):
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise FileNotFoundError("systemd credentials are not available")
    path = Path(directory) / name
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("required systemd credential is unavailable")
    return path.read_text(encoding="utf-8").strip()


def systemd_credential_bytes(name, label=None):
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise FileNotFoundError("systemd credentials are not available")
    path = Path(directory) / name
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("required systemd credential is unavailable")
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError(f"required {label or name} credential is invalid")
    return key


def build_memory_backup_scheduler(store, key, backup_directory, validate_only=False):
    if validate_only:
        return None
    return PeriodicMemoryBackup(store, backup_directory, key)


def env_enabled(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def run(validate_only=False):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        model_config = load_model_config(os.environ)
        lifecycle_policy = load_lifecycle_policy(os.environ)
    except ValueError as error:
        logging.error("Startup validation failed: model or lifecycle configuration (%s)", str(error))
        return 2
    root = data_directory()
    try:
        vault = Vault(vault_directory())
        try:
            names = set(vault.names())
            required = {"telegram_bot", "openrouter_api"} - names
            if required:
                logging.error("Startup validation failed: vault records missing (%s)", ", ".join(sorted(required)))
                return 2
            telegram_token = vault.get_secret("telegram_bot")
            openrouter_key = vault.get_secret("openrouter_api")
        finally:
            vault.close()
    except Exception as error:
        logging.error("Startup validation failed: encrypted vault access (%s)", type(error).__name__)
        return 2

    try:
        owner_id = int(os.environ.get("ANDREW_OWNER_ID", ""))
        if owner_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        logging.error("Startup validation failed: owner Telegram ID configuration (invalid value)")
        return 2
    try:
        store = Store(root / "controller.sqlite3")
    except Exception as error:
        logging.error("Startup validation failed: controller database access (%s)", type(error).__name__)
        return 2
    try:
        memory_backup_key = systemd_credential_bytes("andrew-memory-backup-key")
    except Exception as error:
        logging.error("Startup validation failed: memory backup credential (%s)", type(error).__name__)
        store.close()
        return 2
    try:
        history_key = systemd_credential_bytes("andrew-history-key", "browser history key")
        history = VisitHistory(root / "browser-history", history_key,
                               max_bytes=int(os.environ.get("ANDREW_HISTORY_MAX_BYTES", str(256 * 1024 * 1024))))
    except Exception as error:
        logging.error("Startup validation failed: browser history storage (%s)", type(error).__name__)
        store.close()
        return 2
    telegram = TelegramAPI(telegram_token)
    try:
        gateway_token = systemd_credential("andrew-gateway-token")
    except Exception as error:
        logging.error("Startup validation failed: internal gateway systemd credential (%s)", type(error).__name__)
        store.close()
        return 2
    provider = OpenRouterModel(openrouter_key, model_config=model_config)
    memory_service = MemoryService(store)
    model_gateway = ModelGateway(
        provider, store, gateway_token, memory_service=memory_service, model_config=model_config,
        **lifecycle_policy.budget_arguments(),
    )
    autonomous_enabled = lifecycle_policy.autonomous_enabled
    fallback_enabled = lifecycle_policy.fallback_enabled
    gateway_server = make_server(
        model_gateway,
        host="127.0.0.1",
        port=int(os.environ.get("ANDREW_GATEWAY_PORT", "8284")),
    )
    gateway_thread = GatewayThread(gateway_server)
    gateway_thread.start()
    agent = LettaAgent(
        store,
        gateway_token,
        url=os.environ.get("ANDREW_LETTA_URL", "ws://127.0.0.1:8283/ws"),
        model_config=model_config,
    )
    tool_runtime = None
    if env_enabled("ANDREW_ISOLATED_RUNTIME_ENABLED", False):
        runner = RunnerClient(os.environ.get("ANDREW_RUNNER_SOCKET", "/run/andrew/runner.sock"))
        browser = BrowserClient()
        browser_tools = BrowserTools(browser, StoredAccountBroker(vault_directory(), browser),
                                     "/srv/andrew/workspace/projects", root / "browser-artifacts", history.record)
        tool_runtime = ToolRuntime(runner, ToolPolicy(enabled=True), browser_tools=browser_tools,
                                   on_visit=history.record)
        agent.external_tools_enabled = True
    controller = Controller(
        store,
        agent,
        owner_id,
        **lifecycle_policy.budget_arguments(),
        timezone_name=os.environ.get("ANDREW_TIMEZONE", "America/Toronto"),
        fallback_wake_time=os.environ.get("ANDREW_FALLBACK_WAKE", "09:00"),
        autonomous_enabled=autonomous_enabled,
        fallback_enabled=fallback_enabled,
        memory_service=memory_service,
        model_config=model_config,
        visit_history=history,
        tool_runtime=tool_runtime,
    )
    agent.tool_handler = controller.execute_tool
    check = "Letta provider and persistent agent initialization"
    try:
        agent.initialize()
        check = "Telegram bot authentication (getMe)"
        telegram.get_me()
        logging.info("Startup checks passed: Letta agent, model gateway, Telegram authentication, and persistent database")
    except StartupValidationError as error:
        if error.safe_category:
            logging.error("Startup validation failed: %s (%s: %s)", error.check, error.cause_type, error.safe_category)
        else:
            logging.error("Startup validation failed: %s (%s)", error.check, error.cause_type)
        gateway_thread.close()
        history.close()
        store.close()
        return 2
    except Exception as error:
        logging.error("Startup validation failed: %s (%s)", check, type(error).__name__)
        gateway_thread.close()
        history.close()
        store.close()
        return 2
    if validate_only:
        gateway_thread.close()
        history.close()
        store.close()
        logging.info("Validation-only run completed; controller service was not started")
        return 0
    backup_scheduler = build_memory_backup_scheduler(
        store, memory_backup_key, root / "backups", validate_only=False
    )
    sessions = SessionRunner(store, controller, agent, enabled=autonomous_enabled)
    sessions.recover()
    store.recover_queued_claims()
    if not autonomous_enabled:
        store.set_state("session_start_requested", "")
    logging.info(
        "Lifecycle schedules: fallback=%s autonomous=%s timezone=%s fallback_time=%s",
        "enabled" if fallback_enabled else "disabled",
        "enabled" if autonomous_enabled else "disabled",
        controller.timezone_name,
        controller.fallback_wake_time,
    )
    limiter = OwnerRateLimit(maximum=15, window_seconds=60)
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    def send_owner_text(text):
        if text:
            telegram.send_message(owner_id, text)

    def deliver_diary_outbox():
        try:
            return deliver_pending_diary(telegram, store, owner_id)
        except Exception as error:
            logging.warning("Diary delivery processing stopped (%s)", type(error).__name__)
            return 0

    def process_update(update_id, update):
        message = update.get("message") or {}
        sender = (message.get("from") or {}).get("id")
        if sender == owner_id and not limiter.allow():
            store.add_activity("Owner message dropped by notification rate limit")
            store.complete_update(update_id)
            return
        reply = controller.handle_update(update)
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id == owner_id:
            queued_reply = controller.flush_queued()
            if reply and queued_reply:
                reply = reply + "\n\n" + queued_reply
            elif queued_reply:
                reply = queued_reply
            if reply:
                telegram.send_message(chat_id, reply)
        store.complete_update(update_id)
        deliver_diary_outbox()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    offset = store.last_update_id()
    logging.info("Andrew Telegram controller started; secrets are loaded from the encrypted vault")
    try:
        while not stopping:
            try:
                try:
                    if backup_scheduler.maybe_snapshot():
                        logging.info("Encrypted memory backup snapshot created")
                except Exception as error:
                    logging.warning("Encrypted memory backup failed (%s)", type(error).__name__)
                deliver_diary_outbox()
                scheduled = autonomous_enabled and controller.check_scheduled_wake()
                fallback = fallback_enabled and controller.check_fallback_wake()
                if scheduled or fallback:
                    send_owner_text(controller.flush_queued())
                updates = telegram.get_updates(offset=None if offset is None else offset + 1)
                for update in updates:
                    store.enqueue_update(update)
                offset = store.last_update_id()
                scheduled = autonomous_enabled and controller.check_scheduled_wake()
                fallback = fallback_enabled and controller.check_fallback_wake()
                if scheduled or fallback:
                    send_owner_text(controller.flush_queued())
                for update_id, update in store.pending_updates():
                    process_update(update_id, update)
                offset = store.last_update_id()
                sessions.tick()
                deliver_diary_outbox()
            except RuntimeError as error:
                logging.warning("A remote request failed (%s)", str(error))
                time.sleep(5)
            except Exception:
                logging.exception("Controller iteration failed")
                time.sleep(5)
    finally:
        gateway_thread.close()
        history.close()
        store.close()
    logging.info("Andrew Telegram controller stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(validate_only="--validate-only" in sys.argv[1:]))
