"""Realistic sendMessage receipts for offline senders."""
from itertools import count

_MESSAGE_IDS = count(1)


def sent_text(chat_id, text):
    return {'message_id': next(_MESSAGE_IDS), 'chat': {'id': chat_id, 'type': 'private'}, 'text': text}


def sent_media(chat_id, mime, message_id=None):
    message_id = next(_MESSAGE_IDS) if message_id is None else message_id
    file = {'file_id': 'fixture-file-' + str(message_id), 'file_unique_id': 'fixture-unique-' + str(message_id)}
    result = {'message_id': message_id, 'chat': {'id': chat_id, 'type': 'private'}}
    if mime == 'audio/mpeg':
        result['voice'] = file | {'duration': 1, 'mime_type': 'audio/mpeg'}
    else:
        result['photo'] = [file | {'width': 1, 'height': 1}]
    return result
