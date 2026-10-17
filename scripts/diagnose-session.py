#!/usr/bin/env python3
"""Read incident metadata without model calls, content output, or state writes."""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


def message_metadata(message):
    """Describe response structure without exposing text, reasoning, or tool arguments."""
    allowed_types = {"user_message", "assistant_message", "reasoning_message", "tool_call_message",
                     "tool_return_message", "system_message", "error_message", "loop_error",
                     "approval_request_message", "approval_response_message"}
    kind = message.get("message_type")
    result = {"message_type": kind if kind in allowed_types else "other"}
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
    args = parser.parse_args()
    if args.session != 'latest' and not re.fullmatch(r"[0-9a-f]{12,32}", args.session):
        parser.error("session must be 12 to 32 lowercase hexadecimal characters")
    with sqlite3.connect(Path(args.database).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        if args.session == 'latest':
            latest = db.execute("SELECT session_id FROM cycles WHERE status='failed' "
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
        report["lifecycle"] = rows("SELECT key,value FROM state WHERE key IN "
            "('sleeping','sleep_until','paused','active_session_status','active_session_id',"
            "'session_start_requested','fallback_wake_day','autonomy_error') ORDER BY key")
        report["diary_metadata"] = rows("SELECT local_date,record_type,created_at,delivery_state "
            "FROM diary_entries WHERE session_id LIKE ?", (args.session + "%",))
        if cycles:
            start, end = cycles[0]["started_at"], cycles[-1]["ended_at"] or cycles[-1]["started_at"]
            # Only controller-generated event families relevant to this incident.
            report["events"] = rows("SELECT id,created_at,event FROM activity "
                "WHERE datetime(created_at) BETWEEN datetime(?, '-5 minutes') AND datetime(?, '+5 minutes') "
                "AND (event LIKE 'Autonomous %' OR event LIKE 'Letta model gateway %' "
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
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
