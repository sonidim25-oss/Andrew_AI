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
from .gmail_tools import GmailTools
from .media import MediaService, MediaWorker, load_media_policy
from .media_provider import OpenRouterMedia
from .dispatch import ControllerDispatch
from .latency import LatencyMetrics
from .skills import SkillCatalog


def close_controller_resources(dispatch, gateway_thread, history, store):
    workers_stopped = dispatch.stop()
    gateway_thread.close()
    if not workers_stopped:
        # A timed-out sender/poller may still need these objects. Process exit
        # releases them; closing them here would race its acknowledgement.
        logging.error('Controller workers did not stop; shared storage kept open until process exit')
        return False
    history.close()
    store.close()
    return True


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


def daily_spend_alert_usd():
    raw = os.environ.get("ANDREW_DAILY_SPEND_ALERT_USD", "1.00")
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not 0 <= value < 1000:
        logging.warning("Invalid ANDREW_DAILY_SPEND_ALERT_USD; using 1.00")
        return 1.0
    return value or None


def env_enabled(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def run(validate_only=False):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        model_config = load_model_config(os.environ)
        media_policy = load_media_policy(os.environ)
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
    if any(store.get_state(key,'0')=='1' for key in ('restore_recovery_required','local_recovery_restore_required')):
        logging.error('Restored database is halted pending administrator billing and action reconciliation')
        store.close()
        return 2 if validate_only else 0
    try:
        SkillCatalog().validate()
    except ValueError:
        logging.error('Startup validation failed: trusted skill documents unavailable or changed')
        store.close()
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
    metrics = LatencyMetrics()
    telegram = TelegramAPI(telegram_token, metrics=metrics)
    try:
        gateway_token = systemd_credential("andrew-gateway-token")
    except Exception as error:
        logging.error("Startup validation failed: internal gateway systemd credential (%s)", type(error).__name__)
        store.close()
        return 2
    def credit_key_reader():
        # Read on demand so securely importing the balance credential needs no restart.
        vault = Vault(vault_directory())
        try:
            return vault.get_secret('openrouter_credits') if 'openrouter_credits' in vault.names() else None
        finally:
            vault.close()
    provider = OpenRouterModel(openrouter_key, model_config=model_config, credit_key_reader=credit_key_reader)
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
        owner_id=owner_id,
    )
    media = MediaService(store, root / "media-artifacts", owner_id, media_policy)
    agent.media_tools_enabled = media_policy.enabled
    from .runner_adoption import startup_record, mount_controller
    receipt_record = startup_record('controller')
    runner = None
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
        gmail_tools=GmailTools(vault_directory(), store),
        media=media,
        credit_reader=provider.credit_status,
        daily_spend_alert_usd=daily_spend_alert_usd(),
    )
    agent.tool_handler = controller.execute_tool
    if env_enabled('ANDREW_LOCAL_RECOVERY_ENABLED',False):
        from .recovery import RecoveryCoordinator
        recovery=RecoveryCoordinator(store,controller,enabled=True)
        agent.local_recovery=controller.local_recovery=recovery
        model_gateway.recovery_tool_handler=controller.execute_tool
    if receipt_record is not None:
        mount_controller(controller,runner,receipt_record)
    check = "Letta provider and persistent agent initialization"
    try:
        agent.initialize()
        check = "Telegram bot authentication (getMe)"
        logging.info('Startup check: %s', check)
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
    media.reconcile_deliveries(startup=True)
    store.reconcile_telegram_receipts(owner_id, startup=True)
    sessions.recover()
    interrupted_messages = store.recover_queued_claims()
    if interrupted_messages and not store.get_state('autonomy_error', ''):
        store.record_session_failure('interrupted', owner_id)
    store.recover_update_claims(owner_id)
    store.recover_reply_claims()
    store.recover_diary_claims()
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
        agent.cancel_active_turn()

    def deliver_diary_outbox(should_stop):
        try:
            with metrics.measure('diary'):
                return deliver_pending_diary(telegram, store, owner_id, should_stop=should_stop)
        except Exception as error:
            logging.warning("Diary delivery processing stopped (%s)", type(error).__name__)
            return 0

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    dispatch = ControllerDispatch(store, controller, telegram, sessions, limiter,
                                  owner_id, autonomous_enabled, fallback_enabled, lambda: stopping, metrics=metrics,
                                  diary_action=deliver_diary_outbox, recovery_action=controller.reconcile_actions,
                                  media_worker=MediaWorker(media, OpenRouterMedia(openrouter_key), telegram) if media_policy.enabled else None)
    dispatch.start()
    logging.info("Andrew Telegram controller started; secrets are loaded from the encrypted vault")
    try:
        while not stopping:
            try:
                try:
                    with metrics.measure('backup'):
                        snapshot_created = backup_scheduler.maybe_snapshot()
                    if snapshot_created:
                        logging.info("Encrypted memory backup snapshot created")
                except Exception as error:
                    logging.warning("Encrypted memory backup failed (%s)", type(error).__name__)
                if not dispatch.step():
                    time.sleep(5)
            except RuntimeError as error:
                logging.warning("A remote request failed (%s)", type(error).__name__)
                time.sleep(5)
            except Exception as error:
                logging.warning("Controller iteration failed (%s)", type(error).__name__)
                time.sleep(5)
    finally:
        shutdown_complete = close_controller_resources(dispatch, gateway_thread, history, store)
    logging.info("Andrew Telegram controller stopped")
    return 0 if shutdown_complete else 2


if __name__ == "__main__":
    raise SystemExit(run(validate_only="--validate-only" in sys.argv[1:]))
