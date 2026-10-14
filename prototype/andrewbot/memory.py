import re


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_ -]?key|password|token|secret)\s*[:=]\s*\S+"),
    re.compile(r"(?i)[\"'](?:api[_ -]?key|password|token|secret)[\"']\s*:\s*[\"'][^\"']+[\"']"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
)
_PRIVATE_REASONING = re.compile(
    r"(?i)<\s*/?\s*(?:analysis|think|private_reasoning|chain_of_thought)\b|"
    r"\bprivate\s+chain.of.thought\b"
)
_QUERY_TOKEN = re.compile(r"[\w]+", re.UNICODE)
_FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


def validate_memory_text(value, maximum=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("memory text is empty or exceeds its allowed length")
    normalized = value.strip()
    if _PRIVATE_REASONING.search(normalized) or any(pattern.search(normalized) for pattern in _SECRET_PATTERNS):
        raise ValueError("memory text contains disallowed sensitive material")
    return normalized


def normalize_memory_query(query):
    if not isinstance(query, str):
        return ""
    terms = []
    for token in _QUERY_TOKEN.findall(query)[:24]:
        token = token[:64]
        if token and token.upper() not in _FTS_OPERATORS and token not in terms:
            terms.append(token)
        if len(terms) == 12:
            break
    return " OR ".join(f'"{term}"' for term in terms)


class MemoryService:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _validate_references(event_ids, diary_ids):
        for references, expected_type in ((event_ids, int), (diary_ids, str)):
            if not isinstance(references, (list, tuple)) or len(references) > 20:
                raise ValueError("memory references are invalid")
            if any(isinstance(item, bool) or not isinstance(item, expected_type) for item in references):
                raise ValueError("memory references are invalid")
        return list(event_ids), list(diary_ids)

    def _reject(self, kind):
        self.store.add_activity(f"Memory edit rejected ({kind}; validation)")

    def reject_edit_attempt(self, kind="experience"):
        self._reject("personality" if kind == "personality" else "experience")

    def propose_personality_edit(self, content, reason, event_ids=(), diary_ids=()):
        try:
            normalized = validate_memory_text(content)
            normalized_reason = validate_memory_text(reason, maximum=1000)
            events, diaries = self._validate_references(event_ids, diary_ids)
            return self.store.create_personality_revision(
                normalized, normalized_reason, "andrew", events, diaries
            )
        except (TypeError, ValueError):
            self._reject("personality")
            raise ValueError("personality edit was rejected; check its text and references") from None

    def add_experience(self, content, record_type, event_ids=(), diary_ids=()):
        try:
            normalized = validate_memory_text(content)
            if not isinstance(record_type, str) or record_type not in {"experience", "interest"}:
                raise ValueError("invalid memory record type")
            events, diaries = self._validate_references(event_ids, diary_ids)
            return self.store.create_memory_record(record_type, normalized, "andrew", events, diaries)
        except (TypeError, ValueError):
            self._reject("experience")
            raise ValueError("memory record was rejected; check its type, text, and references") from None

    def search(self, query, limit=8):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("memory result limit must be from 1 to 20")
        normalized = normalize_memory_query(query)
        if not normalized:
            return []
        return self.store.search_memory_rows(normalized, limit)

    def current_personality_context(self):
        revision = self.store.current_personality_revision()
        if revision is None:
            return ""
        header = (
            f"Current personality revision {revision['revision_id']} "
            f"(effective {revision['effective_date']}; source {revision['source']}):\n"
        )
        suffix = f"\nReason: {revision['reason']}"
        available = max(0, 4000 - len(header) - len(suffix))
        content = revision["content"][:available]
        return f"{header}{content}{suffix}"
