"""Meterless Runtime as an OpenAI-compatible LLM provider (Meterless Runtime 1.4, provider contract v4).

Model ids are routing modes, not models: meterless/<disposition>, meterless/<class>,
meterless/<class>.<disposition>. The runtime picks the actual model.

Settings live in CareOps/.env:
    meterless_base_url       e.g. http://127.0.0.1:8088/v1 (saved by detect/connect)
    meterless_runtime_token  the token the runtime writes to <stateDir>/proxy-token
    meterless_state_dir      optional; if set and no token is stored, <stateDir>/proxy-token is read

CLI:
    python -m careops.meterless status            check the configured runtime (asks for a token if unauthorized)
    python -m careops.meterless detect            Auto Detect a local runtime and save its URL
    python -m careops.meterless connect <url>     save a base URL, or a connect link http://<host>:<port>/connect#k=<key>
    python -m careops.meterless chat "<text>" [model]

The token and connect-link key are never logged or printed.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import httpx
from dotenv import load_dotenv, set_key
from langchain_openai import ChatOpenAI

PROVIDER_ID = "meterless"
PROVIDER_NAME = "Meterless Runtime"
DEFAULT_BASE_URL = "http://127.0.0.1:8088/v1"
APP_ID = "careops-reconcile"  # sent as meterless-app on every request
TOKEN_KEY = "meterless_runtime_token"
BASE_URL_KEY = "meterless_base_url"
STATE_DIR_KEY = "meterless_state_dir"
PLACEHOLDER_TOKEN = "rt-local"
CHAT_TIMEOUT = 180.0  # a model load can take minutes
PROBE_TIMEOUT = 3.0

DETECT_PORTS = (8088, 8090, 8787, 11434)
DETECT_HOSTS = ("127.0.0.1", "localhost", "[::1]")
DISPOSITIONS = ("auto", "fast", "deep", "cheap", "local", "fusion")

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(ENV_PATH)

log = logging.getLogger("careops.meterless")

# Longest first, so "/v1/models" is stripped before "/v1".
_PATH_SUFFIXES = ("/v1/chat/completions", "/v1/models", "/chat/completions", "/models", "/healthz", "/v1")


class MeterlessError(Exception):
        """Configuration or connection problem. Message never contains the token."""


@dataclass
class RuntimeStatus:
        state: Literal["online", "unauthorized", "offline"]
        base_url: str
        models: list[str] = field(default_factory=list)
        detail: str = ""


# --- URLs and connect links ------------------------------------------------------------

def normalize_base_url(url: str) -> str:
        """Turn any pasted runtime URL into <scheme>://<host>[:port][/prefix]/v1, never /v1/v1."""
        url = url.strip()
        if not url:
                raise MeterlessError("Empty Meterless URL")
        if "://" not in url:
                url = "http://" + url
        parts = urlsplit(url)
        if not parts.netloc:
                raise MeterlessError("Meterless URL has no host")
        path = parts.path.rstrip("/")
        stripped = True
        while stripped:
                stripped = False
                for suffix in _PATH_SUFFIXES:
                        if path.endswith(suffix):
                                path = path[: -len(suffix)].rstrip("/")
                                stripped = True
                                break
        return f"{parts.scheme}://{parts.netloc}{path}/v1"


def parse_connect_link(link: str) -> tuple[str, str] | None:
        """Split http://<host>:<port>/connect#k=<key> into (base_url, key). None if it is not a connect link."""
        parts = urlsplit(link.strip())
        if not parts.scheme or not parts.netloc or parts.path.rstrip("/") != "/connect":
                return None
        key = parse_qs(parts.fragment).get("k", [""])[0]
        if not key:
                raise MeterlessError("Connect link has no k=<key> in its fragment")
        return f"{parts.scheme}://{parts.netloc}/v1", key


# --- token ---------------------------------------------------------------------------------

def check_token(token: str | None) -> str:
        token = (token or "").strip()
        if not token:
                raise MeterlessError(f"No Meterless token. Set {TOKEN_KEY} in .env or run: python -m careops.meterless status")
        if token == PLACEHOLDER_TOKEN:
                raise MeterlessError(f"'{PLACEHOLDER_TOKEN}' is a placeholder and is rejected; use the token from <stateDir>/proxy-token")
        return token


def read_token() -> str | None:
        """Stored token, else <stateDir>/proxy-token if meterless_state_dir is set."""
        token = os.environ.get(TOKEN_KEY, "").strip()
        if token:
                return token
        state_dir = os.environ.get(STATE_DIR_KEY, "").strip()
        if state_dir:
                token_file = Path(state_dir).expanduser() / "proxy-token"
                if token_file.is_file():
                        return token_file.read_text().strip() or None
        return None


def save_token(token: str) -> None:
        token = check_token(token)
        set_key(ENV_PATH, TOKEN_KEY, token)
        os.environ[TOKEN_KEY] = token
        log.info("saved token to .env (%s)", TOKEN_KEY)


def save_base_url(base_url: str) -> None:
        base_url = normalize_base_url(base_url)
        set_key(ENV_PATH, BASE_URL_KEY, base_url)
        os.environ[BASE_URL_KEY] = base_url
        log.info("saved base URL %s to .env (%s)", base_url, BASE_URL_KEY)


def configured_base_url() -> str:
        return normalize_base_url(os.environ.get(BASE_URL_KEY) or DEFAULT_BASE_URL)


def _headers(token: str | None, chat: bool = False) -> dict[str, str]:
        headers = {"meterless-app": APP_ID}
        if token:
                headers["Authorization"] = f"Bearer {token}"
        if chat:
                headers["meterless-scope"] = "user"
        return headers


# --- status and discovery ------------------------------------------------------------------

def check_status(base_url: str, token: str | None, timeout: float = PROBE_TIMEOUT) -> RuntimeStatus:
        """GET /v1/models decides: 401/403 unauthorized, 200 with meterless ids online, else offline."""
        base_url = normalize_base_url(base_url)
        try:
                resp = httpx.get(f"{base_url}/models", headers=_headers(token), timeout=timeout)
        except httpx.HTTPError as exc:
                return RuntimeStatus("offline", base_url, detail=type(exc).__name__)
        if resp.status_code in (401, 403):
                return RuntimeStatus("unauthorized", base_url, detail=f"HTTP {resp.status_code}")
        if resp.status_code != 200:
                return RuntimeStatus("offline", base_url, detail=f"HTTP {resp.status_code}")
        try:
                ids = [m.get("id", "") for m in resp.json().get("data", [])]
        except (ValueError, AttributeError):
                return RuntimeStatus("offline", base_url, detail="/v1/models did not return JSON")
        # Keep every meterless/* id, including ones this code does not recognise.
        models = [i for i in ids if isinstance(i, str) and i.startswith("meterless/")]
        if not models:
                return RuntimeStatus("offline", base_url, detail="no meterless/* models listed")
        return RuntimeStatus("online", base_url, models=models)


def auto_detect(token: str | None) -> RuntimeStatus | None:
        """Try the known ports on 127.0.0.1, localhost and [::1]; save and return the first online/unauthorized hit."""
        for port in DETECT_PORTS:
                for host in DETECT_HOSTS:
                        status = check_status(f"http://{host}:{port}/v1", token)
                        log.info("detect %s -> %s%s", status.base_url, status.state,
                                 f" ({status.detail})" if status.detail else "")
                        if status.state in ("online", "unauthorized"):
                                save_base_url(status.base_url)
                                return status
        return None


def describe_model(model_id: str) -> dict:
        """Split meterless/<class>.<disposition> into its parts; unknown ids are kept, just unlabelled."""
        mode = model_id.removeprefix("meterless/")
        if mode in DISPOSITIONS:
                return {"id": model_id, "class": None, "disposition": mode}
        cls, _, disposition = mode.partition(".")
        return {"id": model_id, "class": cls, "disposition": disposition if disposition in DISPOSITIONS else None}


# --- LangChain chat model ------------------------------------------------------------------

def get_chat_model(model: str = "meterless/auto", base_url: str | None = None, token: str | None = None) -> ChatOpenAI:
        """A LangChain chat model that talks to Meterless Runtime over its OpenAI-compatible API."""
        if not model.startswith("meterless/"):
                raise MeterlessError(f"Meterless model ids look like meterless/<mode>, got {model!r}")
        return ChatOpenAI(
                model=model,
                base_url=normalize_base_url(base_url) if base_url else configured_base_url(),
                api_key=check_token(token if token is not None else read_token()),  # sent as Authorization: Bearer
                default_headers={"meterless-app": APP_ID, "meterless-scope": "user"},
                timeout=CHAT_TIMEOUT,
        )


# --- CLI -----------------------------------------------------------------------------------

def _print_status(status: RuntimeStatus) -> None:
        print(f"{PROVIDER_NAME} at {status.base_url}: {status.state.upper()}"
              + (f" ({status.detail})" if status.detail else ""))
        for model_id in status.models:
                print("  ", model_id)


def _ask_for_token(status: RuntimeStatus) -> RuntimeStatus:
        """The 'paste your token' field for a terminal."""
        print("The runtime rejected the token. Paste the token from <stateDir>/proxy-token (input is hidden).")
        token = getpass("Meterless token: ")
        try:
                save_token(token)
        except MeterlessError as exc:
                print(exc)
                return status
        return check_status(status.base_url, token)


def main(argv: list[str]) -> int:
        from careops.logging_setup import setup_logging
        setup_logging()
        command = argv[0] if argv else "status"

        if command == "status":
                status = check_status(configured_base_url(), read_token())
                if status.state == "unauthorized" and sys.stdin.isatty():
                        status = _ask_for_token(status)
                _print_status(status)
                return 0 if status.state == "online" else 1

        if command == "detect":
                status = auto_detect(read_token())
                if status is None:
                        print(f"No {PROVIDER_NAME} found on ports {DETECT_PORTS} of {', '.join(DETECT_HOSTS)}.")
                        return 1
                _print_status(status)
                if status.state == "unauthorized":
                        print(f"Found it, but the token is missing or wrong. Run: python -m careops.meterless status")
                return 0

        if command == "connect" and len(argv) > 1:
                link = parse_connect_link(argv[1])
                if link:
                        base_url, key = link
                        save_base_url(base_url)
                        save_token(key)
                        print(f"Connected via link: base URL {base_url}, token saved.")
                else:
                        save_base_url(argv[1])
                        print(f"Saved base URL {configured_base_url()}")
                return 0

        if command == "chat" and len(argv) > 1:
                model = argv[2] if len(argv) > 2 else "meterless/auto"
                reply = get_chat_model(model).invoke(argv[1])
                print(reply.content)
                return 0

        print(__doc__)
        return 2


if __name__ == "__main__":
        try:
                sys.exit(main(sys.argv[1:]))
        except MeterlessError as exc:
                sys.exit(f"Error: {exc}")
