"""Read-only access to Zetaris over its REST API (Layer 1: data access).

Two endpoints are used, matching what the Zetaris web UI itself calls:

- /api/proxy/query/sql/start            SELECT only (Zetaris rejects anything else)
- /api/proxy/sql-editor/sqls/run-query  any statement; we restrict it to SHOW/DESCRIBE

Credentials and the session token never appear in exceptions or repr().
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

SELECT_PATH = "/api/proxy/query/sql/start"
METADATA_PATH = "/api/proxy/sql-editor/sqls/run-query"
LOGIN_PATH = "/api/auth/login"

_METADATA_KEYWORDS = ("SHOW", "DESCRIBE", "DESC")


class ZetarisError(Exception):
    """Login, transport, or query failure. Message is safe to log."""


@dataclass
class QueryResult:
    sql: str
    rows: list[dict] = field(default_factory=list)
    total_count: int = 0

    @property
    def truncated(self) -> bool:
        # More rows matched than were returned: evidence is incomplete.
        return self.total_count > len(self.rows)


def _first_keyword(sql: str) -> str:
    match = re.match(r"\s*([A-Za-z]+)", sql)
    return match.group(1).upper() if match else ""


def _check_single_statement(sql: str) -> None:
    # A trailing semicolon is harmless; one in the middle means a second statement.
    if ";" in sql.strip().rstrip(";"):
        raise ZetarisError("Multiple SQL statements are not allowed")


class ZetarisClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self._token: str | None = None
        self._org_id: str | None = None
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)

    @classmethod
    def from_env(cls) -> ZetarisClient:
        load_dotenv()
        try:
            return cls(
                base_url=os.environ.get("ZETARIS_BASE_URL", "http://localhost:3000"),
                username=os.environ["ZETARIS_USERNAME"],
                password=os.environ["ZETARIS_PASSWORD"],
            )
        except KeyError as missing:
            raise ZetarisError(f"Missing environment variable {missing}") from None

    def __repr__(self) -> str:
        return f"ZetarisClient(base_url={self.base_url!r}, username={self.username!r})"

    def _login(self) -> None:
        try:
            resp = self._http.post(LOGIN_PATH, json={"username": self.username, "password": self._password})
        except httpx.HTTPError as exc:
            raise ZetarisError(f"Cannot reach Zetaris at {self.base_url}: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise ZetarisError(f"Zetaris login failed (HTTP {resp.status_code})")
        body = resp.json()
        self._token, self._org_id = body["idToken"], str(body["orgId"])

    def _post(self, path: str, payload: dict) -> dict:
        if self._token is None:
            self._login()
        for attempt in range(2):
            headers = {
                "Authorization": f"Bearer {self._token}",
                "X-Org-ID": self._org_id,
                "X-Request-ID": str(uuid.uuid4()),
            }
            try:
                resp = self._http.post(path, json=payload, headers=headers)
            except httpx.TimeoutException:
                raise ZetarisError("Zetaris query timed out") from None
            except httpx.HTTPError as exc:
                raise ZetarisError(f"Zetaris request failed: {type(exc).__name__}") from None
            if resp.status_code == 401 and attempt == 0:
                self._login()  # token expired; retry once
                continue
            if resp.status_code != 200:
                raise ZetarisError(f"Zetaris returned HTTP {resp.status_code}: {_error_text(resp)}")
            return resp.json()
        raise ZetarisError("Zetaris authentication failed after re-login")

    def select(self, sql: str, max_rows: int = 200) -> QueryResult:
        """Run one SELECT and return up to max_rows rows as dicts."""
        if _first_keyword(sql) != "SELECT":
            raise ZetarisError("select() only accepts SELECT statements")
        _check_single_statement(sql)
        body = self._post(SELECT_PATH, {"select": sql, "pageLimit": max_rows})
        rows = body.get("records", [])
        return QueryResult(sql=sql, rows=rows, total_count=int(body.get("totalCount", len(rows))))

    def metadata(self, sql: str) -> list[dict]:
        """Run a SHOW/DESCRIBE statement and return rows as dicts keyed by header."""
        if _first_keyword(sql) not in _METADATA_KEYWORDS:
            raise ZetarisError("metadata() only accepts SHOW or DESCRIBE statements")
        _check_single_statement(sql)
        body = self._post(METADATA_PATH, {"sql": sql, "limit": 500})
        headers = body.get("headers", [])
        return [dict(zip(headers, row)) for row in body.get("data", [])]

    def close(self) -> None:
        self._http.close()


def _error_text(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("msg", resp.text))[:300]
    except ValueError:
        return resp.text[:300]
