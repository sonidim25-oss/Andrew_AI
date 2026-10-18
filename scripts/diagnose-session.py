#!/usr/bin/env python3
"""Read incident metadata without model calls, content output, or state writes."""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def message_metadata(message):
    """Describe response structure without exposing text, reasoning, or tool arguments."""
    allowed_types = {"user_message", "assistant_message", "reasoning_message", "tool_call_message",
                     "tool_return_message", "system_message", "error_message", "loop_error",
                     "approval_request_message", "approval_response_message", "stop_reason", "usage_statistics",
                     "summary_message", "reasoning_summary_message"}
    kind = message.get("message_type")
    result = {"message_type": kind if kind in allowed_types else "other"}
    reason = message.get('stop_reason')
    if reason is not None:
        result['stop_reason'] = reason if reason in {
            'end_turn', 'error', 'error_retry_exhausted', 'max_steps', 'max_tokens_exceeded',
            'context_window_overflow_in_system_prompt', 'no_tool_call', 'tool_rule', 'requires_approval', 'cancelled'
        } else 'other'
    if kind in {'loop_error', 'error_message'} or reason in {'error', 'error_retry_exhausted'}:
        # Reduce saved errors to an existing fixed vocabulary; never print raw
        # errors, which can contain credentials, request bodies or chat content.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'prototype'))
        from andrewbot.appserver import _safe_error_category
        result['error_category'] = _safe_error_category(message)
    try:
        result["created_at"] = datetime.fromisoformat(message.get("date") or message.get("created_at", "")).isoformat()
    except (ValueError, TypeError):
        result["created_at"] = None
    content = message.get("content")
    result["content_present"] = bool(content)
    if message.get("status") in {"success", "error"}:
        result["tool_status"] = message["status"]
    calls = message.get("tool_calls") or []
    if isinstance(calls, dict):
        calls = [calls]
    if isinstance(message.get("tool_call"), dict):
        calls = [message["tool_call"], *calls]
    result["tool_calls"] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function", call)
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments")
        try:
            decoded = json.loads(arguments) if isinstance(arguments, str) else arguments
            valid = isinstance(decoded, dict)
        except (ValueError, TypeError):
            valid = False
        # Tool names and arguments are model-controlled; print only structural facts.
        name = function.get("name")
        known_tools = {"sleep", "wake_at", "continue_activity", "check_budget", "recent_activity", "save_diary_entry",
                       "memory_search", "memory_add_experience", "memory_propose_personality_edit",
                       "python.run", "node.run", "shell.run", "browser.open", "browser.control", "browser.account"}
        result["tool_calls"].append({"name": name if name in known_tools else "other",
                                     "arguments_characters": len(arguments) if isinstance(arguments, str) else None,
                                     "arguments_valid_object": valid})
    return result


def letta_metadata(db, start, end):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prototype"))
    from andrewbot.appserver import AppServerClient
    row = db.execute("SELECT value FROM state WHERE key='letta_conversation_id'").fetchone()
    if not row or not row[0]:
        return {"status": "conversation_id_unavailable"}
    client = AppServerClient(timeout=15)
    try:
        response = client.request({"type": "conversation_messages_list", "conversation_id": row[0],
                                   "query": {"limit": 100, "order": "desc"}},
                                  "conversation_messages_list_response")
        messages = [message_metadata(m) for m in response.get("messages", []) if isinstance(m, dict)]
        lower, upper = datetime.fromisoformat(start) - timedelta(seconds=5), datetime.fromisoformat(end) + timedelta(seconds=5)
        matched = []
        for message in messages:
            timestamp = datetime.fromisoformat(message["created_at"]) if message["created_at"] else None
            if timestamp is None or timestamp.tzinfo is None or lower <= timestamp <= upper:
                matched.append(message)
        return {"scope": "latest 100 messages in the current saved conversation; incident time window",
                "messages_scanned": len(messages), "messages": matched}
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", nargs="?", default="latest", help="Session ID, hexadecimal prefix, or latest (default)")
    parser.add_argument("--database", default="/var/lib/andrew/control/controller.sqlite3")
    parser.add_argument("--letta-metadata", action="store_true", help="Read saved Letta message structure; no content is printed")
    parser.add_argument("--letta-recovery-metadata", action="store_true",
                        help="Read local recovery journal metadata without attaching to Letta or changing state")
    parser.add_argument("--letta-home", default="/var/lib/andrew/letta",
                        help="Letta service home for optional recovery metadata (not the controller home)")
    parser.add_argument("--current", action="store_true", help="Inspect the latest cycle and current queues, even without a failure")
    parser.add_argument("--brief", action="store_true", help="Show current state, recent events and saved message metadata with usage totals")
    args = parser.parse_args()
    if args.session != 'latest' and not re.fullmatch(r"[0-9a-f]{12,32}", args.session):
        parser.error("session must be 12 to 32 lowercase hexadecimal characters")
    with sqlite3.connect(Path(args.database).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        if args.session == 'latest':
            latest = db.execute("SELECT session_id FROM cycles " + ("" if args.current else "WHERE status='failed' ") +
                                "ORDER BY started_at DESC LIMIT 1").fetchone()
            if latest is None:
                parser.error('no failed autonomous cycle was found')
            args.session = latest['session_id']
        def rows(sql, parameters=()):
            return [dict(row) for row in db.execute(sql, parameters)]
        cycles = rows("SELECT session_id, cycle_id, status, started_at, ended_at FROM cycles "
                      "WHERE session_id LIKE ? ORDER BY started_at", (args.session + "%",))
        if len({row["session_id"] for row in cycles}) > 1:
            parser.error("ambiguous prefix; supply the full session ID")
        report = {"integrity": db.execute("PRAGMA quick_check").fetchone()[0], "cycles": cycles}
        report["recent_failed_cycles"] = rows(
            "SELECT session_id,cycle_id,started_at,ended_at FROM cycles WHERE status='failed' "
            "ORDER BY started_at DESC LIMIT 3")
        # Queue states distinguish waiting input from a dispatched, uncertain turn.
        report["recent_owner_messages"] = rows(
            "SELECT id,created_at,queued,length(content) AS characters FROM messages "
            "WHERE role='user' ORDER BY id DESC LIMIT 15")
        report["queue_state_legend"] = {"0": "processed", "1": "pending", "2": "claimed", "3": "uncertain"}
        for table in ("telegram_inbox", "telegram_uncertain", "telegram_outbox"):
            report[table + "_metadata"] = rows(
                f"SELECT created_at,attempts FROM {table} ORDER BY id DESC LIMIT 15"
                if table == "telegram_outbox" else
                f"SELECT update_id,received_at FROM {table} ORDER BY update_id DESC LIMIT 15")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'external_action_receipts' in tables:
            report['action_recovery'] = rows(
                'SELECT action_id,adapter,operation,state,session_id,cycle_id,created_at,checked_at '
                'FROM external_action_receipts ORDER BY created_at DESC LIMIT 20')
        if 'telegram_chunk_receipts' in tables:
            report['telegram_receipts'] = rows(
                'SELECT delivery_id,kind,record_id,chunk,chat_id,message_id,confirmed_at '
                'FROM telegram_chunk_receipts ORDER BY confirmed_at DESC LIMIT 20')
        if 'media_delivery_receipts' in tables:
            report['media_delivery_receipts'] = rows(
                'SELECT job_id,kind,chat_id,message_id,confirmed_at FROM media_delivery_receipts '
                'ORDER BY confirmed_at DESC LIMIT 20')
        if 'model_turn_receipts' in tables:
            report['model_turn_receipts'] = rows(
                'SELECT cycle_id,session_id,agent_id,conversation_id,state,run_id,terminal_id,created_at,updated_at '
                'FROM model_turn_receipts ORDER BY created_at DESC LIMIT 20')
        if args.letta_recovery_metadata:
            from letta_recovery_metadata import collect_recovery_metadata
            receipts = rows('SELECT cycle_id,agent_id,conversation_id,request_id,client_message_id,run_id '
                            'FROM model_turn_receipts WHERE session_id LIKE ? ORDER BY created_at DESC LIMIT 6',
                            (args.session + '%',)) if 'model_turn_receipts' in tables else []
            report['letta_recovery_metadata'] = collect_recovery_metadata(args.letta_home, receipts)
        if 'cycle_recovery_checkpoints' in tables:
            report['recovery_checkpoints'] = rows(
                'SELECT k.*,c.session_id,c.status AS cycle_status FROM cycle_recovery_checkpoints k '
                'JOIN cycles c ON c.cycle_id=k.cycle_id WHERE c.session_id LIKE ? '
                'ORDER BY c.started_at DESC LIMIT 10', (args.session + '%',))
        if 'cycle_tool_attempts' in tables:
            report['recovery_tool_attempts'] = rows(
                'SELECT t.cycle_id,t.outcome,count(*) AS attempts FROM cycle_tool_attempts t '
                'JOIN cycles c ON c.cycle_id=t.cycle_id WHERE c.session_id LIKE ? '
                'GROUP BY t.cycle_id,t.outcome ORDER BY c.started_at DESC LIMIT 30', (args.session + '%',))
        if "research_entries" in tables:
            report["saved_research_entries"] = rows(
                "SELECT record_id,revision,status,created_at,updated_at FROM research_entries "
                "ORDER BY updated_at DESC LIMIT 15")
        if "research_checkpoints" in tables:
            report["saved_research_checkpoints"] = rows(
                "SELECT entry_id,revision,session_id,cycle_id,stop_type,created_at "
                "FROM research_checkpoints ORDER BY id DESC LIMIT 15")
        if "research_events" in tables:
            report["saved_research_activity"] = rows(
                "SELECT session_id,cycle_id,count(*) AS recorded_events,max(created_at) AS last_saved_at "
                "FROM research_events GROUP BY session_id,cycle_id ORDER BY last_saved_at DESC LIMIT 15")
        report["lifecycle"] = rows("SELECT key,value FROM state WHERE key IN "
            "('sleeping','sleep_until','paused','active_session_status','active_session_id',"
            "'session_start_requested','fallback_wake_day','autonomy_error','autonomous_owner_id',"
            "'owner_control_revision','conversation_until','model_turn_origin','model_turn_progress',"
            "'owner_reply_rounds') ORDER BY key")
        report["diary_metadata"] = rows("SELECT local_date,record_type,created_at,delivery_state "
            "FROM diary_entries WHERE session_id LIKE ?", (args.session + "%",))
        if cycles:
            start, end = cycles[0]["started_at"], cycles[-1]["ended_at"] or cycles[-1]["started_at"]
            if args.current:
                end = datetime.now(timezone.utc).isoformat()
            # Only controller-generated event families relevant to this incident.
            report["events"] = rows("SELECT id,created_at,event FROM activity "
                "WHERE datetime(created_at) BETWEEN datetime(?, '-5 minutes') AND datetime(?, '+5 minutes') "
                "AND (event LIKE 'Autonomous %' OR event LIKE 'Letta model gateway %' "
                "OR event LIKE 'Letta turn failed %' "
                "OR event='Owner reply provider returned tool calls despite text-only mode; execution blocked' "
                "OR event='Owner message outcome uncertain; held to avoid duplicate delivery' "
                "OR event='Andrew replied to the owner' "
                "OR event='Owner reply: requesting a text answer after tool work' "
                "OR event='Owner message queued while automatic model calls are stopped' "
                "OR event LIKE 'Session failed %' OR event LIKE 'Daily fallback %' OR event LIKE 'Scheduled wake %') ORDER BY id", (start, end))
            report["usage"] = rows("SELECT created_at,cost_usd,input_tokens,output_tokens FROM usage "
                "WHERE datetime(created_at) BETWEEN datetime(?, '-5 minutes') AND datetime(?, '+5 minutes')", (start, end))
            report["reservations"] = rows("SELECT created_at,cost_usd FROM budget_reservations "
                "WHERE datetime(created_at) BETWEEN datetime(?, '-5 minutes') AND datetime(?, '+5 minutes')", (start, end))
            if args.letta_metadata:
                try:
                    report["letta_metadata"] = letta_metadata(db, start, end)
                except Exception:
                    report["letta_metadata"] = {"status": "metadata_read_failed"}
        if args.brief:
            usage = report.get('usage', [])
            report = {key: report[key] for key in ('integrity', 'lifecycle', 'letta_metadata', 'letta_recovery_metadata', 'recovery_checkpoints',
                                                  'recovery_tool_attempts', 'action_recovery', 'model_turn_receipts', 'telegram_receipts', 'media_delivery_receipts') if key in report} | {
                'recent_owner_messages': report['recent_owner_messages'][:3],
                'events': report.get('events', [])[-12:],
                'usage_summary': {'settled_requests': len(usage), 'last_request': usage[-1] if usage else None},
                'unsettled_reservations': len(report.get('reservations', [])),
            }
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
