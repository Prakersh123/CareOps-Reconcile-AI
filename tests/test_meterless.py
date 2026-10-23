"""Meterless provider tests against a fake runtime that follows provider contract v4."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from careops import meterless
from careops.meterless import MeterlessError, normalize_base_url, parse_connect_link

GOOD_TOKEN = "test-token-123"


# --- URL normalisation ---------------------------------------------------------------------

@pytest.mark.parametrize("pasted", [
        "127.0.0.1:8088",
        "http://127.0.0.1:8088",
        "http://127.0.0.1:8088/",
        "http://127.0.0.1:8088/v1",
        "http://127.0.0.1:8088/v1/",
        "http://127.0.0.1:8088/v1/chat/completions",
        "http://127.0.0.1:8088/v1/models",
        "http://127.0.0.1:8088/healthz",
        "  127.0.0.1:8088/v1/v1  ",
])
def test_normalize_base_url(pasted):
        assert normalize_base_url(pasted) == "http://127.0.0.1:8088/v1"


def test_normalize_keeps_https_and_ipv6():
        assert normalize_base_url("https://box.local/v1/models") == "https://box.local/v1"
        assert normalize_base_url("[::1]:8787") == "http://[::1]:8787/v1"


def test_parse_connect_link():
        assert parse_connect_link("http://192.168.1.20:8088/connect#k=abc123") == ("http://192.168.1.20:8088/v1", "abc123")
        assert parse_connect_link("http://127.0.0.1:8088/v1") is None
        with pytest.raises(MeterlessError):
                parse_connect_link("http://127.0.0.1:8088/connect#x=1")


def test_placeholder_token_rejected():
        with pytest.raises(MeterlessError, match="placeholder"):
                meterless.check_token("rt-local")
        with pytest.raises(MeterlessError):
                meterless.check_token("")


def test_describe_model_keeps_unknown_ids():
        assert meterless.describe_model("meterless/fast")["disposition"] == "fast"
        assert meterless.describe_model("meterless/code.deep") == {"id": "meterless/code.deep", "class": "code", "disposition": "deep"}
        assert meterless.describe_model("meterless/vision-x")["class"] == "vision-x"


# --- fake runtime --------------------------------------------------------------------------

class FakeRuntime(BaseHTTPRequestHandler):
        chat_requests: list = []

        def log_message(self, *args):
                pass

        def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        def _authorized(self):
                return (self.headers.get("Authorization") == f"Bearer {GOOD_TOKEN}"
                        and self.headers.get("meterless-app") == meterless.APP_ID)

        def do_GET(self):
                if self.path == "/healthz":
                        return self._send(200, {"ok": True})
                if self.path == "/v1/models":
                        if not self._authorized():
                                return self._send(401, {"error": "unauthorized"})
                        return self._send(200, {"object": "list", "data": [
                                {"id": "meterless/auto"}, {"id": "meterless/fast"},
                                {"id": "meterless/code.deep"}, {"id": "meterless/brand-new-mode"},
                        ]})
                self._send(404, {"detail": "Not Found"})

        def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/v1/chat/completions" or not self._authorized():
                        return self._send(401, {"error": "unauthorized"})
                # Header names are case-insensitive; store them lowercased for the assertions.
                headers = {k.lower(): v for k, v in self.headers.items()}
                FakeRuntime.chat_requests.append({"headers": headers, "body": body})
                self._send(200, {
                        "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": body["model"],
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": "pong"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })


@pytest.fixture
def runtime(monkeypatch, tmp_path):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRuntime)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        FakeRuntime.chat_requests = []
        # Never touch the real .env or real ports in tests.
        monkeypatch.setattr(meterless, "ENV_PATH", tmp_path / ".env")
        (tmp_path / ".env").write_text("")
        monkeypatch.setattr(meterless, "DETECT_PORTS", (1, port))  # port 1: nothing listens -> offline
        monkeypatch.setattr(meterless, "DETECT_HOSTS", ("127.0.0.1",))
        monkeypatch.delenv(meterless.TOKEN_KEY, raising=False)
        monkeypatch.delenv(meterless.BASE_URL_KEY, raising=False)
        yield f"http://127.0.0.1:{port}/v1"
        server.shutdown()


def test_wrong_token_is_unauthorized_not_offline(runtime):
        assert meterless.check_status(runtime, "wrong-token").state == "unauthorized"
        assert meterless.check_status(runtime, None).state == "unauthorized"


def test_good_token_is_online_and_keeps_unknown_ids(runtime):
        status = meterless.check_status(runtime, GOOD_TOKEN)
        assert status.state == "online"
        assert "meterless/brand-new-mode" in status.models


def test_non_meterless_service_is_offline(runtime):
        # A different service on a probed port (like the unrelated app on 8088 here) is not a runtime.
        assert meterless.check_status("http://127.0.0.1:1/v1", GOOD_TOKEN).state == "offline"


def test_auto_detect_finds_runtime_and_saves_url(runtime, tmp_path):
        status = meterless.auto_detect(GOOD_TOKEN)
        assert status is not None and status.state == "online" and status.base_url == runtime
        assert f"meterless_base_url='{runtime}'" in (tmp_path / ".env").read_text()


def test_auto_detect_saves_unauthorized_hit(runtime):
        status = meterless.auto_detect("wrong-token")
        assert status is not None and status.state == "unauthorized"


def test_plain_text_on_meterless_auto_returns_string(runtime):
        model = meterless.get_chat_model("meterless/auto", base_url=runtime, token=GOOD_TOKEN)
        reply = model.invoke("ping")
        assert isinstance(reply.content, str) and reply.content == "pong"

        sent = FakeRuntime.chat_requests[-1]
        assert sent["body"]["model"] == "meterless/auto"
        assert sent["body"]["messages"][-1]["content"] == "ping"  # plain string, not a parts list
        assert sent["headers"]["meterless-scope"] == "user"
        assert sent["headers"]["meterless-app"] == meterless.APP_ID
        assert sent["headers"]["authorization"] == f"Bearer {GOOD_TOKEN}"


def test_chat_model_rejects_placeholder_token(runtime):
        with pytest.raises(MeterlessError, match="placeholder"):
                meterless.get_chat_model("meterless/auto", base_url=runtime, token="rt-local")


def test_chat_model_timeout_is_180s(runtime):
        model = meterless.get_chat_model("meterless/auto", base_url=runtime, token=GOOD_TOKEN)
        assert model.request_timeout == 180.0
