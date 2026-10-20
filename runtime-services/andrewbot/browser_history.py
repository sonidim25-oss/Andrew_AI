"""Encrypted, indefinite retention of sanitized browser visit metadata."""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_bytes
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class HistoryCapacityError(RuntimeError):
    pass


def sanitize_visit(url, title):
    if not isinstance(url, str) or not isinstance(title, str):
        raise ValueError("visit fields are invalid")
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("visit URL is invalid")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError):
        raise ValueError("visit URL is invalid") from None
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((parts.scheme.lower() == "http" and port == 80) or (parts.scheme.lower() == "https" and port == 443)):
        netloc += f":{port}"
    clean = urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", "", ""))[:2048]
    clean_title = " ".join(title.split())[:512]
    return clean, clean_title


class VisitHistory:
    def __init__(self, directory, key, max_bytes=256 * 1024 * 1024):
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("history key must be 32 bytes")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 4096:
            raise ValueError("history capacity is invalid")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / "history.sqlite3"
        self.key = key
        self.max_bytes = max_bytes
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("CREATE TABLE IF NOT EXISTS visits (id INTEGER PRIMARY KEY, visited_at TEXT NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL)")
        self.db.commit()
        self.path.chmod(0o600)
        if self._allocated_bytes() > max_bytes:
            self.db.close()
            raise HistoryCapacityError("history capacity reached")

    def _allocated_bytes(self):
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        page_count = self.db.execute("PRAGMA page_count").fetchone()[0]
        return page_size * page_count

    @staticmethod
    def _timestamp(value):
        value = datetime.now(timezone.utc) if value is None else value
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("visit time must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")

    def record(self, url, title, visited_at=None):
        clean_url, clean_title = sanitize_visit(url, title)
        stamp = self._timestamp(visited_at)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.db.execute("INSERT INTO visits(visited_at, nonce, ciphertext) VALUES(?,?,?)", (stamp, b"", b""))
            visit_id = cursor.lastrowid
            nonce = token_bytes(12)
            payload = (clean_url + "\0" + clean_title).encode("utf-8")
            ciphertext = AESGCM(self.key).encrypt(nonce, payload, str(visit_id).encode("ascii"))
            self.db.execute("UPDATE visits SET nonce=?, ciphertext=? WHERE id=?", (nonce, ciphertext, visit_id))
            if self._allocated_bytes() > self.max_bytes:
                raise HistoryCapacityError("history capacity reached")
            self.db.commit()
            self.path.chmod(0o600)
            return visit_id
        except Exception:
            self.db.rollback()
            raise

    def search(self, query, limit=20):
        if not isinstance(query, str) or len(query) > 256:
            raise ValueError("history query is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("history result limit is invalid")
        needle = query.casefold()
        results = []
        rows = self.db.execute("SELECT id, visited_at, nonce, ciphertext FROM visits ORDER BY id DESC").fetchall()
        for visit_id, stamp, nonce, ciphertext in rows:
            try:
                plaintext = AESGCM(self.key).decrypt(nonce, ciphertext, str(visit_id).encode("ascii")).decode("utf-8")
                url, title = plaintext.split("\0", 1)
            except Exception:
                raise ValueError("history record authentication failed") from None
            if needle in (title + " " + url).casefold():
                results.append({"id": visit_id, "visited_at": stamp, "title": title, "url": url})
                if len(results) >= limit:
                    break
        return results

    def delete(self, visit_id):
        if isinstance(visit_id, bool) or not isinstance(visit_id, int) or visit_id <= 0:
            raise ValueError("visit id is invalid")
        cursor = self.db.execute("DELETE FROM visits WHERE id=?", (visit_id,))
        self.db.commit()
        self.db.execute("VACUUM")
        return cursor.rowcount > 0

    def delete_all(self):
        count = self.db.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
        self.db.execute("DELETE FROM visits")
        self.db.commit()
        self.db.execute("VACUUM")
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(self.path) + suffix).unlink()
            except FileNotFoundError:
                pass
        return count

    def close(self):
        self.db.close()
