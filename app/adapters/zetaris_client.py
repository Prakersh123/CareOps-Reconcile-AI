"""Read-only access to Zetaris over its REST API (the data layer adapter).

Two ways to connect, chosen by from_env():

- API key (the hackathon setup guide's route): ZETARIS_REST_URL (http://localhost:8888/api/v1.0),
  ZETARIS_API_KEY sent as a Bearer token, ZETARIS_ORG_ID (1 on a local install).
- Sign-in: ZETARIS_BASE_URL (the web interface, http://localhost:3000) with ZETARIS_USERNAME and
  ZETARIS_PASSWORD; the session token is renewed once if it expires.

Either way, agents get two methods:
- select()   -> /query/sql/start, which Zetaris itself restricts to SELECT
- metadata() -> /sql-editor/sqls/run, restricted here to SHOW/DESCRIBE

execute_admin() runs any statement and exists only for scripts/setup_zetaris.py (registering the
data sources); no agent tool calls it. Credentials and tokens never appear in exceptions or repr().
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

# Paths relative to the base URL in each mode.
_API_KEY_PATHS = {"select": "/query/sql/start", "run": "/sql-editor/sqls/run"}
_SIGN_IN_PATHS = {"select": "/api/proxy/query/sql/start", "run": "/api/proxy/sql-editor/sqls/run"}
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
    def __init__(self, base_url: str, username: str | None = None, password: str | None = None,
                 api_key: str | None = None, org_id: str | None = None, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        if not api_key and not (username and password):
            raise ZetarisError("Zetaris needs ZETARIS_API_KEY, or ZETARIS_USERNAME and ZETARIS_PASSWORD")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self._api_key = api_key
        self._token: str | None = api_key
        self._org_id: str | None = org_id if api_key else None
        self._paths = _API_KEY_PATHS if api_key else _SIGN_IN_PATHS
        self._login_error: str | None = None  # set once a sign-in is refused; never retried
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)  # transport: tests

    @property
    def mode(self) -> str:
        return "api-key" if self._api_key else "sign-in"

    @classmethod
    def from_env(cls) -> ZetarisClient:
        """API key mode when ZETARIS_API_KEY is set, otherwise sign-in with username and password."""
        load_dotenv()
        api_key = os.environ.get("ZETARIS_API_KEY", "").strip()
        if api_key:
            return cls(base_url=os.environ.get("ZETARIS_REST_URL") or "http://localhost:8888/api/v1.0",
                       api_key=api_key, org_id=os.environ.get("ZETARIS_ORG_ID") or "1")
        try:
            return cls(
                base_url=os.environ.get("ZETARIS_BASE_URL", "http://localhost:3000"),
                username=os.environ["ZETARIS_USERNAME"],
                password=os.environ["ZETARIS_PASSWORD"],
            )
        except KeyError as missing:
            raise ZetarisError(f"Missing environment variable {missing} (or set ZETARIS_API_KEY)") from None

    def __repr__(self) -> str:
        who = "api key" if self._api_key else f"username={self.username!r}"
        return f"ZetarisClient(base_url={self.base_url!r}, {who})"

    def _login(self) -> None:
        # Zetaris locks an account after repeated failed sign-ins, so a refused sign-in is never
        # retried by this client: every later call fails fast with the same error instead.
        if self._login_error:
            raise ZetarisError(self._login_error)
        try:
            resp = self._http.post(LOGIN_PATH, json={"username": self.username, "password": self._password})
        except httpx.HTTPError as exc:
            raise ZetarisError(f"Cannot reach Zetaris at {self.base_url}: {type(exc).__name__}") from None
        if resp.status_code != 200:
            self._login_error = (f"Zetaris login failed (HTTP {resp.status_code}: {_error_text(resp)}); check "
                                 f"ZETARIS_USERNAME/ZETARIS_PASSWORD. Not retried: repeated failures lock the account.")
            raise ZetarisError(self._login_error)
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
                raise ZetarisError(f"Zetaris request failed ({self.base_url}): {type(exc).__name__}") from None
            if resp.status_code == 401 and attempt == 0 and not self._api_key:
                self._login()  # session token expired; sign in again once
                continue
            # A bad key comes back as 401/403, or as 400 "Not Allowed" on Zetaris 2.4.
            if self._api_key and (resp.status_code in (401, 403)
                                  or (resp.status_code == 400 and "not allowed" in _error_text(resp).lower())):
                raise ZetarisError(f"Zetaris rejected the API key (HTTP {resp.status_code}); check ZETARIS_API_KEY "
                                   f"and ZETARIS_ORG_ID")
            if resp.status_code != 200:
                raise ZetarisError(f"Zetaris returned HTTP {resp.status_code}: {_error_text(resp)}")
            return resp.json()
        raise ZetarisError("Zetaris authentication failed after re-login")

    def select(self, sql: str, max_rows: int = 200) -> QueryResult:
        """Run one SELECT and return up to max_rows rows as dicts."""
        if _first_keyword(sql) != "SELECT":
            raise ZetarisError("select() only accepts SELECT statements")
        _check_single_statement(sql)
        body = self._post(self._paths["select"], {"select": sql, "pageLimit": max_rows})
        rows = body.get("records", [])
        return QueryResult(sql=sql, rows=rows, total_count=int(body.get("totalCount", len(rows))))

    def metadata(self, sql: str) -> list[dict]:
        """Run a SHOW/DESCRIBE statement and return rows as dicts keyed by header."""
        if _first_keyword(sql) not in _METADATA_KEYWORDS:
            raise ZetarisError("metadata() only accepts SHOW or DESCRIBE statements")
        _check_single_statement(sql)
        return self._run(sql, limit=500)

    def execute_admin(self, sql: str) -> list[dict]:
        """Run any single statement (DDL included). For the data setup script only; agents never call it."""
        _check_single_statement(sql)
        return self._run(sql, limit=50)

    def _run(self, sql: str, limit: int) -> list[dict]:
        body = self._post(self._paths["run"], {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor",
                                               "limit": limit})
        headers = body.get("headers", [])
        return [dict(zip(headers, row)) for row in body.get("data", [])]

    def close(self) -> None:
        self._http.close()


def _error_text(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("msg", resp.text))[:300]
    except ValueError:
        return resp.text[:300]
