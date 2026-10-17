"""Realistic sendMessage receipts for offline senders."""
from itertools import count

_MESSAGE_IDS = count(1)


def sent_text(chat_id, text):
    return {'message_id': next(_MESSAGE_IDS), 'chat': {'id': chat_id, 'type': 'private'}, 'text': text}
