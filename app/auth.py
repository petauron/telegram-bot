from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable


SESSION_COOKIE_NAME = "id"
SESSION_TTL_SECONDS = 8 * 60 * 60
MIN_WEB_PASSWORD_LENGTH = 8
EXAMPLE_WEB_PASSWORD = "change-this-to-a-long-random-password"
MAX_SESSIONS = 128
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_PER_IP_LIMIT = 5
LOGIN_GLOBAL_LIMIT = 50
MAX_TRACKED_IPS = 512


def is_valid_web_password(password: str) -> bool:
    return (
        len(password) >= MIN_WEB_PASSWORD_LENGTH
        and password != EXAMPLE_WEB_PASSWORD
    )


@dataclass(frozen=True, slots=True)
class AdminSession:
    username: str
    expires_at: float


class SessionStore:
    def __init__(
        self,
        *,
        ttl_seconds: int = SESSION_TTL_SECONDS,
        max_sessions: int = MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._sessions: OrderedDict[bytes, AdminSession] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            digest
            for digest, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for digest in expired:
            self._sessions.pop(digest, None)

    def create(self, username: str) -> tuple[str, AdminSession]:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        session = AdminSession(
            username=username,
            expires_at=now + self._ttl_seconds,
        )
        digest = self._digest(token)
        with self._lock:
            self._purge_expired_locked(now)
            while len(self._sessions) >= self._max_sessions:
                self._sessions.popitem(last=False)
            self._sessions[digest] = session
        return token, session

    def validate(self, token: str | None) -> AdminSession | None:
        if not token or len(token) > 256:
            return None
        digest = self._digest(token)
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            session = self._sessions.get(digest)
            if session is None:
                return None
            self._sessions.move_to_end(digest)
            return session

    def revoke(self, token: str | None) -> bool:
        if not token or len(token) > 256:
            return False
        digest = self._digest(token)
        with self._lock:
            return self._sessions.pop(digest, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired_locked(self._clock())
            return len(self._sessions)


class LoginRateLimiter:
    def __init__(
        self,
        *,
        window_seconds: int = LOGIN_WINDOW_SECONDS,
        per_ip_limit: int = LOGIN_PER_IP_LIMIT,
        global_limit: int = LOGIN_GLOBAL_LIMIT,
        max_tracked_ips: int = MAX_TRACKED_IPS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_seconds = window_seconds
        self._per_ip_limit = per_ip_limit
        self._global_limit = global_limit
        self._max_tracked_ips = max_tracked_ips
        self._clock = clock
        self._per_ip: OrderedDict[str, deque[float]] = OrderedDict()
        self._global: deque[tuple[float, str]] = deque(maxlen=global_limit)
        self._lock = threading.Lock()

    def _purge_locked(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._global and self._global[0][0] <= cutoff:
            self._global.popleft()
        empty: list[str] = []
        for client_ip, failures in self._per_ip.items():
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                empty.append(client_ip)
        for client_ip in empty:
            self._per_ip.pop(client_ip, None)

    def allowed(self, client_ip: str) -> bool:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            failures = self._per_ip.get(client_ip)
            return (
                (failures is None or len(failures) < self._per_ip_limit)
                and len(self._global) < self._global_limit
            )

    def record_failure(self, client_ip: str) -> None:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            failures = self._per_ip.get(client_ip)
            if failures is None:
                while len(self._per_ip) >= self._max_tracked_ips:
                    self._per_ip.popitem(last=False)
                failures = deque(maxlen=self._per_ip_limit)
                self._per_ip[client_ip] = failures
            failures.append(now)
            self._per_ip.move_to_end(client_ip)
            self._global.append((now, client_ip))

    def record_success(self, client_ip: str) -> None:
        with self._lock:
            self._per_ip.pop(client_ip, None)
            self._global = deque(
                (
                    (failed_at, failed_ip)
                    for failed_at, failed_ip in self._global
                    if failed_ip != client_ip
                ),
                maxlen=self._global_limit,
            )

    def clear(self) -> None:
        with self._lock:
            self._per_ip.clear()
            self._global.clear()

    def tracked_counts(self) -> tuple[int, int]:
        with self._lock:
            self._purge_locked(self._clock())
            return len(self._per_ip), len(self._global)
