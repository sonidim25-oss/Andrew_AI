"""Bounded process-local latency aggregates, without message or error payloads."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import math
import sqlite3
import threading
import time


_CURRENT_ERRORS = ContextVar('latency_current_errors', default=None)
_ERROR_TYPES = ((TimeoutError, 'timeout'), (sqlite3.Error, 'database_error'),
                (ConnectionError, 'connection_error'), (OSError, 'io_error'),
                (ValueError, 'invalid_value'), (RuntimeError, 'runtime_error'))
_ERROR_CATEGORIES = frozenset(label for _, label in _ERROR_TYPES) | {'unexpected_error', 'cancelled'}


def record_failure(error, category=None):
    """Mark the current span when an operation handles its own exception.

    No-op outside a measurement. Retain only fixed labels, never exceptions or
    arbitrary class names. A span counts a category at most once.
    """
    errors = _CURRENT_ERRORS.get()
    if errors is not None:
        if category not in _ERROR_CATEGORIES:
            category = next((label for kind, label in _ERROR_TYPES if isinstance(error, kind)),
                            'unexpected_error')
        errors.add(category)


class LatencyMetrics:
    STAGES = frozenset({'poll', 'inbox_wait', 'control', 'chat', 'schedule',
                        'queued', 'autonomous', 'outbox_wait', 'send', 'diary', 'backup', 'media_send', 'media_generate'})
    WALL_AGES = frozenset({'inbox_wait', 'outbox_wait'})

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._stats = {stage: {'count': 0, 'failures': 0, 'total_seconds': 0.0,
                              'last_seconds': None, 'max_seconds': 0.0,
                              'last_success_monotonic': None, 'errors': {},
                              'clock': 'wall_age' if stage in self.WALL_AGES else 'monotonic'}
                       for stage in self.STAGES}
        self._active = {stage: {} for stage in self.STAGES}

    def _validate_stage(self, stage):
        if stage not in self.STAGES:
            raise ValueError('Unknown latency stage')

    def _record(self, stage, seconds, errors, finished):
        row = self._stats[stage]
        row['count'] += 1
        row['failures'] += int(bool(errors))
        for category in errors:
            row['errors'][category] = row['errors'].get(category, 0) + 1
        row['total_seconds'] += seconds
        row['last_seconds'] = seconds
        row['max_seconds'] = max(row['max_seconds'], seconds)
        if not errors:
            row['last_success_monotonic'] = finished

    @contextmanager
    def measure(self, stage):
        self._validate_stage(stage)
        token = object()
        with self._lock:
            started = self._clock()
            self._active[stage][token] = started
        errors = set()
        context_token = _CURRENT_ERRORS.set(errors)
        try:
            yield
        except BaseException as error:
            record_failure(error)
            raise
        finally:
            _CURRENT_ERRORS.reset(context_token)
            with self._lock:
                finished = self._clock()
                self._active[stage].pop(token)
                self._record(stage, max(0.0, finished - started), errors, finished)

    def observe(self, stage, seconds):
        self._validate_stage(stage)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('Latency duration must be finite and nonnegative')
        with self._lock:
            self._record(stage, seconds, (), self._clock())

    def snapshot(self):
        with self._lock:
            now = self._clock()
            return {stage: {**row, 'errors': dict(row['errors']), 'active_count': len(self._active[stage]),
                            'oldest_active_seconds': max(0.0, now - min(self._active[stage].values()))
                            if self._active[stage] else None}
                    for stage, row in self._stats.items()}


def wall_age_seconds(timestamp, now):
    """UTC queue age, distinct from process-local monotonic durations."""
    if not isinstance(timestamp, str):
        return None
    try:
        created = datetime.fromisoformat(timestamp)
        if created.utcoffset() is None or now.utcoffset() is None:
            return None
        return max(0.0, (now - created).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None
