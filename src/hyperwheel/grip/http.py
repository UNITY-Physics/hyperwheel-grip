"""HTTPS to GRIP: tokens and GRIP's client retry rule, in one place.

The retry rule (from GRIP's mock API document):
- 429 and 503: wait for Retry-After, then retry;
- other 5xx and timeouts: retry with back-off and jitter;
- 401: get a new token and retry once;
- any other 4xx: never retry; hand it back to the caller to report.

When the in-call retries run out, ``Transient`` is raised and the caller parks
the file to try again on a later run. Nothing in here deletes anything.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from typing import Any

import requests

from ..config import UploadSettings
from ..keys import Credentials
from .errors import AuthFailed, KeyRejected, Transient

log = logging.getLogger("hyperwheel.grip")

_ERROR_CODE = re.compile(r"[a-z_]{1,40}")


class TokenManager:
    """Gets a client-credentials token and renews it shortly before it expires.

    ``credentials`` is called before every token request, so a key replaced
    on disk is picked up straight away.
    """

    REFRESH_MARGIN_S = 30

    def __init__(self, token_url: str, credentials: Callable[[], Credentials], session: requests.Session,
                 *, timeout_s: float, scope: str = "openid grip", clock: Callable[[], float] = time.monotonic):
        self.token_url = token_url
        self.credentials = credentials
        self.session = session
        self.timeout_s = timeout_s
        self.scope = scope
        self.clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def invalidate(self) -> None:
        self._token = None

    def get(self) -> str:
        if self._token and self.clock() < self._expires_at - self.REFRESH_MARGIN_S:
            return self._token
        creds = self.credentials()
        try:
            r = self.session.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
                    "scope": self.scope,
                },
                timeout=self.timeout_s,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            raise Transient(f"token request: no answer ({type(e).__name__})", no_answer=True) from None
        if r.status_code == 200:
            try:
                body = r.json()
                token = body["access_token"]
                expires_in = float(body.get("expires_in", 300))
            except (ValueError, KeyError, TypeError):
                raise Transient("token request: answer not understood") from None
            if not isinstance(token, str) or not token:
                raise Transient("token request: answer has no token")
            self._token = token
            self._expires_at = self.clock() + expires_in
            log.debug("new GRIP token, valid for %.0f s", expires_in)
            return token
        if r.status_code in (400, 401, 403):
            code = _error_code(r)
            raise KeyRejected(f"GRIP refused the key: HTTP {r.status_code}{f' ({code})' if code else ''}",
                              r.status_code)
        raise Transient(f"token request: HTTP {r.status_code}")


def _error_code(r: requests.Response) -> str | None:
    try:
        code = r.json().get("error")
    except (ValueError, AttributeError):
        return None
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else None


class Http:
    def __init__(self, base_url: str, tokens: TokenManager, session: requests.Session, settings: UploadSettings,
                 *, sleep: Callable[[float], None] = time.sleep, rng: Callable[[], float] = random.random,
                 extra_headers: Callable[[str, str, int], dict[str, str]] | None = None):
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self.session = session
        self.settings = settings
        self.sleep = sleep
        self.rng = rng
        # Tests only: adds headers such as X-Mock-Fault to a given attempt.
        self.extra_headers = extra_headers

    def call(self, method: str, path: str, *, headers: dict[str, str] | None = None, attempts: int | None = None,
             **kw: Any) -> requests.Response:
        """Send one request, following the retry rule. Returns any 2xx, 3xx or 4xx response except 401.

        ``attempts`` overrides the tries per call; with 1, a failure is raised at once and not
        logged here (the caller decides what to do, e.g. send a smaller chunk).
        """
        url = self.base_url + path
        attempts = attempts or self.settings.attempts_per_call
        renewed = False
        last, no_answer, retry_after = "no attempt made", False, None
        attempt = 0
        # The body must be sent within the connect timeout (urllib3 sends it under that
        # deadline), so give a large body the time it needs on a slow link.
        body = kw.get("data")
        size = len(body) if isinstance(body, (bytes, bytearray, memoryview)) else 0
        timeout = (max(self.settings.timeout_s, size / self.settings.slowest_link_bytes_per_s), self.settings.timeout_s)
        while attempt < attempts:
            attempt += 1
            try:
                token = self.tokens.get()
            except Transient as e:
                last, no_answer, retry_after = str(e), e.no_answer, None
                self._wait(attempt, attempts, self._backoff(attempt), f"{method} {path}: {last}")
                continue
            h = dict(headers or {})
            h["Authorization"] = f"Bearer {token}"
            if self.extra_headers:
                h.update(self.extra_headers(method, path, attempt))
            try:
                r = self.session.request(method, url, headers=h, timeout=timeout, **kw)
            except (requests.ConnectionError, requests.Timeout) as e:
                last, no_answer, retry_after = f"no answer ({type(e).__name__})", True, None
                self._wait(attempt, attempts, self._backoff(attempt), f"{method} {path}: {last}")
                continue

            if r.status_code == 401:
                if renewed:
                    raise AuthFailed(f"{method} {path}: HTTP 401 with a new token")
                renewed = True
                self.tokens.invalidate()
                attempt -= 1  # a token renewal doesn't use up an attempt
                log.info("%s %s: token not accepted, getting a new one", method, path)
                continue
            if r.status_code in (429, 503):
                last, no_answer, retry_after = f"HTTP {r.status_code}", False, self._retry_after(r)
                wait = retry_after if retry_after is not None else self._backoff(attempt)
                self._wait(attempt, attempts, wait, f"{method} {path}: {last}")
                continue
            if r.status_code >= 500:
                last, no_answer, retry_after = f"HTTP {r.status_code}", False, None
                self._wait(attempt, attempts, self._backoff(attempt), f"{method} {path}: {last}")
                continue
            return r
        tries = f", after {attempts} tries" if attempts > 1 else ""
        raise Transient(f"{method} {path}: {last}{tries}", no_answer=no_answer, retry_after=retry_after)

    def _wait(self, attempt: int, attempts: int, seconds: float, why: str) -> None:
        if attempt >= attempts:
            if attempts > 1:
                log.warning("%s. Giving up for now", why)
            return
        log.warning("%s. Trying again in %.1f s", why, seconds)
        self.sleep(seconds)

    def _retry_after(self, r: requests.Response) -> float | None:
        try:
            value = float(r.headers.get("Retry-After", ""))
        except ValueError:
            return None
        return max(0.0, min(value, self.settings.max_retry_after_s))

    def backoff(self, attempt: int) -> float:
        return self._backoff(attempt)

    def _backoff(self, attempt: int) -> float:
        base = min(self.settings.backoff_max_s, self.settings.backoff_base_s * 2 ** (attempt - 1))
        return base * (0.5 + self.rng())
