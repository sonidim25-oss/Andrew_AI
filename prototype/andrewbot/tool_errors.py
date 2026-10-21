"""Explicit tool rejections whose action has not started.

Never wrap an arbitrary execution exception in this type. Messages must be fixed
controller vocabulary, without raw arguments, credentials or provider responses.
Audit bookkeeping may have been recorded; the requested action has not occurred.
"""


class ToolRejected(ValueError):
    def __init__(self, message, *, code='tool_rejected'):
        super().__init__(message)
        self.code = code

    def result(self):
        return {'status': 'error', 'error': {'code': self.code, 'message': str(self)},
                'executed': False}
