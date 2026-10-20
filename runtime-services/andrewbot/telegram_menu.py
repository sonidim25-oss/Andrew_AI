"""Owner command keyboard; button labels are routed before model messages."""

BUTTON_ROWS = (
    (("📊 Status", "/status"), ("💰 Budget", "/budget")),
    (("🌐 Browsing history", "/history"), ("📖 Diary", "/diary")),
    (("📋 Activity", "/activity"), ("🔄 Retry messages", "/replay")),
    (("☀️ Wake up", "/wake"), ("🌙 Sleep", "/sleep")),
    (("⏸ Pause", "/pause"), ("⏰ Set wake time", "/wake_at")),
    (("❓ Help", "/help"), ("👋 Start", "/start")),
    (("📬 Gmail", "/gmail"),),
)
BUTTON_COMMANDS = {label: command for row in BUTTON_ROWS for label, command in row}


def command_keyboard():
    # A fresh value prevents callers from modifying future keyboards.
    return {
        "keyboard": [[{"text": label} for label, _ in row] for row in BUTTON_ROWS],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "Message Andrew or choose a command",
    }


HELP_TEXT = (
    "Andrew · Your controls\n\n"
    "📊 /status — current state and next wake\n"
    "💰 /budget — spending and remaining allowance\n"
    "🌐 /history — recent browsing; /history example.com to search\n"
    "📖 /diary — latest entry; /diary YYYY-MM-DD for a date\n"
    "📋 /activity — recent actions\n\n"
    "📬 /gmail — connection and separate Gmail permissions\n\n"
    "☀️ /wake — wake up and clear pause\n"
    "🌙 /sleep — sleep until the next scheduled wake; /sleep 30 for 30 minutes\n"
    "⏰ /wake_at — choose a local wake time\n"
    "⏸ /pause — pause until you wake me\n"
    "🔄 /replay — retry messages with uncertain delivery (may duplicate replies)\n\n"
    "History: /history delete <id> or /history delete all. Deleting all requires confirmation.\n"
    "Diary delivery: /diary retry YYYY-MM-DD (may duplicate delivery).\n"
    "/cancel — cancel time entry\n"
    "/start or /menu — show controls · /help — this guide\n\n"
    "You can also type a normal message. If the buttons are hidden, tap the keyboard icon."
)
