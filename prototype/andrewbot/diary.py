import re
from datetime import datetime
from .telegram import deliver_saved_message
from .latency import record_failure


MAX_DIARY_CHARACTERS = 3000
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk-[a-z0-9_-]{16,}|sk-ant-[a-z0-9_-]{16,})\b"),
    re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(?:API[_-]?KEY|ACCESS[_-]?TOKEN|BOT[_-]?TOKEN|PASSWORD|CLIENT[_-]?SECRET)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(?:token|secret|password|api.?key|authorization)\b\W{0,12}[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_PRIVATE_REASONING = re.compile(r"(?i)\b(?:chain[- ]of[- ]thought|private reasoning|hidden reasoning)\b")


def validate_diary_content(content):
    if not isinstance(content, str):
        raise ValueError("diary content must be text")
    content = content.strip()
    if not content:
        raise ValueError("diary content is empty")
    if len(content) > MAX_DIARY_CHARACTERS:
        raise ValueError("diary content exceeds the 3000 character limit")
    if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
        raise ValueError("diary content failed the safety check")
    if _PRIVATE_REASONING.search(content):
        raise ValueError("diary content failed the safety check")
    return content


def format_diary_entry(entry):
    if not entry:
        raise ValueError("a diary entry is required")
    if entry.get("record_type") == "incident_summary":
        content = validate_diary_content(entry.get("content"))
        if not content.startswith("AUTOMATED INCIDENT SUMMARY — this was not written by Andrew."):
            raise ValueError("incident summary label is missing")
        return content
    if entry.get("record_type") != "diary":
        raise ValueError("a diary entry is required")
    content = validate_diary_content(entry.get("content"))
    rendered = f"Andrew’s diary — {entry['local_date']}\n\n{content}"
    event_ids = entry.get("event_ids") or []
    if event_ids:
        rendered += "\n\nObjective activity event IDs: " + ", ".join(str(item) for item in event_ids)
    if entry.get("delivery_state") == "uncertain":
        rendered += "\n\nTelegram delivery is uncertain; a retry may send this entry twice."
    elif entry.get("delivery_state") == "pending" and int(entry.get("delivery_attempts", 0)) > 0:
        rendered += "\n\nRetry requested after an uncertain Telegram delivery; this may produce a duplicate."
    return rendered


def incident_summary(session_id, reason, activity):
    safe_reasons = {
        "provider_error": "a model request or session ledger operation failed",
        "restart": "the controller restarted while the session was active",
    }
    reason_text = safe_reasons.get(reason, "the session ended unexpectedly")
    lines = [
        "AUTOMATED INCIDENT SUMMARY — this was not written by Andrew.",
        f"Session reference: {str(session_id)[:12] or 'unavailable'}.",
        f"Recorded cause: {reason_text}.",
    ]
    safe_events = []
    for row in activity or []:
        try:
            event = row.get("event", "") if isinstance(row, dict) else row["event"]
        except (KeyError, IndexError, TypeError):
            event = ""
        if not isinstance(event, str) or not event:
            continue
        try:
            safe_events.append(validate_diary_content(event)[:240])
        except ValueError:
            continue
    if safe_events:
        lines.append("Last recorded objective events: " + " | ".join(safe_events[-4:]))
    else:
        lines.append("No additional safe objective event details were available.")
    lines.append("No first-person diary was saved before this interruption.")
    return "\n".join(lines)[:MAX_DIARY_CHARACTERS]


def parse_diary_command(text):
    if not isinstance(text, str):
        raise ValueError("invalid diary command")
    pieces = text.strip().split()
    if not pieces:
        raise ValueError("invalid diary command")
    command = pieces[0].split("@", 1)[0].lower()
    if command != "/diary":
        raise ValueError("invalid diary command")
    if len(pieces) == 1:
        return "latest", None
    if len(pieces) == 2:
        try:
            parsed = datetime.strptime(pieces[1], "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("use /diary YYYY-MM-DD") from None
        if parsed.isoformat() != pieces[1]:
            raise ValueError("use /diary YYYY-MM-DD")
        return "date", pieces[1]
    if len(pieces) == 3 and pieces[1].lower() == "retry":
        try:
            parsed = datetime.strptime(pieces[2], "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("use /diary retry YYYY-MM-DD") from None
        if parsed.isoformat() != pieces[2]:
            raise ValueError("use /diary retry YYYY-MM-DD")
        return "retry", pieces[2]
    raise ValueError("use /diary, /diary YYYY-MM-DD, or /diary retry YYYY-MM-DD")


def deliver_pending_diary(telegram, store, owner_id):
    delivered = 0
    for entry in store.pending_diary_deliveries():
        entry_id = entry["entry_id"]
        if not store.claim_pending_diary_delivery(entry_id):
            continue
        try:
            deliver_saved_message(telegram, store, 'diary', entry_id, owner_id,
                                  format_diary_entry(entry), entry['delivery_attempts'] > 0)
        except Exception as error:
            record_failure(error)
            store.add_activity(f"Diary Telegram delivery outcome uncertain ({entry_id})")
            continue
        if store.mark_diary_delivered(entry_id):
            delivered += 1
            store.add_activity(f"Diary Telegram delivery confirmed ({entry_id})")
    return delivered
