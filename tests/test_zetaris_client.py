import base64
import json
import time

import httpx
import pytest

from app.adapters.zetaris_client import ZetarisClient, ZetarisError

USL_TABLE = "lightning.metastore.careops.CAREOPS_RECONCILIATION.CAREOPS_RECONCILIATION"


# --- guards: rejected before any network call -------------------------------------

@pytest.fixture
def offline_client():
    return ZetarisClient("http://unused.invalid", "user", "secret-pw")


@pytest.mark.parametrize("sql", [
    "DELETE FROM t",
    "DROP TABLE t",
    "INSERT INTO t VALUES (1)",
    "SHOW TABLES",
    "  update t set x = 1",
])
def test_select_rejects_non_select(offline_client, sql):
    with pytest.raises(ZetarisError, match="only accepts SELECT"):
        offline_client.select(sql)


def test_select_rejects_stacked_statements(offline_client):
    with pytest.raises(ZetarisError, match="Multiple SQL statements"):
        offline_client.select("SELECT 1; DROP TABLE t")


def test_metadata_rejects_select_and_ddl(offline_client):
    for sql in ("SELECT 1", "CREATE NAMESPACE x", "REMOVE USL A NAMESPACE b"):
        with pytest.raises(ZetarisError, match="only accepts SHOW or DESCRIBE"):
            offline_client.metadata(sql)


def test_password_not_in_repr(offline_client):
    assert "secret-pw" not in repr(offline_client)


# --- live Zetaris (skipped if not reachable) ---------------------------------------

@pytest.fixture(scope="module")
def live_client():
    client = ZetarisClient.from_env()
    try:
        client.select("SELECT 1 AS one")
    except ZetarisError as exc:
        pytest.skip(f"Zetaris not available: {exc}")
    yield client
    client.close()


def test_usl_returns_ten_rows(live_client):
    result = live_client.select(f"SELECT * FROM {USL_TABLE}")
    assert result.total_count == 10
    assert not result.truncated
    assert {r["order_id"] for r in result.rows} == {f"L10{i}" for i in range(1, 9)}


def test_messy_billing_keys_and_statuses_come_through_as_stored(live_client):
    rows = live_client.select(f"SELECT order_id, invoice_id, payment_status FROM {USL_TABLE} "
                              f"WHERE order_id IN ('L105', 'L107', 'L108')").rows
    status = {r["invoice_id"]: r["payment_status"] for r in rows}
    assert status["I106"] == "Paid" and status["I108"] == "unpaid "
    assert [r["invoice_id"] for r in rows if r["order_id"] == "L108"] == [None]  # billed as 'L-108'


def test_l103_has_cancelled_and_replacement(live_client):
    result = live_client.select(f"SELECT * FROM {USL_TABLE} WHERE order_id = 'L103'")
    by_invoice = {r["invoice_id"]: r for r in result.rows}
    assert by_invoice["I102"]["invoice_status"] == "CANCELLED"
    assert by_invoice["I104"]["notes"] == "Replacement for I102"
    assert by_invoice["I104"]["payment_status"] == "UNPAID"


def test_truncated_flag(live_client):
    result = live_client.select(f"SELECT * FROM {USL_TABLE}", max_rows=2)
    assert len(result.rows) == 2 and result.truncated


def test_describe_usl_table(live_client):
    columns = [r["col_name"] for r in live_client.metadata(f"DESCRIBE {USL_TABLE}")]
    assert columns[:2] == ["order_id", "patient_id"] and "notes" in columns


def test_list_usls_in_namespace(live_client):
    names = {r["name"] for r in live_client.metadata("SHOW NAMESPACES OR TABLES IN lightning.metastore.careops")}
    assert "CAREOPS_RECONCILIATION" in names


def test_unknown_table_raises_clean_error(live_client):
    with pytest.raises(ZetarisError, match="HTTP 400"):
        live_client.select("SELECT * FROM NO_SUCH_DB.no_such_table")


def test_refused_sign_in_is_a_clean_error_and_never_retried():
    # A fake Zetaris, never the real one: real failed sign-ins count towards Zetaris' account lock.
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(404, json={"msg": "Not Allowed"})

    client = ZetarisClient("http://zetaris.test", "admin@example.com", "wrong-password",
                           transport=httpx.MockTransport(handler))
    for _ in range(3):
        with pytest.raises(ZetarisError, match="login failed") as exc:
            client.select("SELECT 1")
        assert "wrong-password" not in str(exc.value) and "Not retried" in str(exc.value)
    assert calls == ["/api/auth/login"]  # one sign-in attempt, however many queries follow



def fake_jwt(expires_in: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + expires_in}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.sig"


def session_client(answers):
    """A fake Zetaris: each sign-in issues a fresh token; queries are answered by answers(token, logins)."""
    calls = {"logins": 0, "queries": 0}

    def handler(request):
        if request.url.path == "/api/auth/login":
            calls["logins"] += 1
            return httpx.Response(200, json={"idToken": calls["token"](calls["logins"]), "orgId": 1})
        calls["queries"] += 1
        return answers(request.headers["Authorization"], calls["logins"])

    client = ZetarisClient("http://zetaris.test", "admin@example.com", "pw", transport=httpx.MockTransport(handler))
    calls["token"] = lambda n: fake_jwt(900)
    return client, calls


@pytest.mark.parametrize("msg", ["Bad Request", "Not Allowed"])
def test_session_rejected_with_400_signs_in_again_once(msg):
    # Zetaris answers an expired session token with 400 and a bare message, not 401.
    def answers(auth, logins):
        if logins == 1:
            return httpx.Response(400, json={"msg": msg})
        return httpx.Response(200, json={"records": [{"n": "5"}], "totalCount": 1})

    client, calls = session_client(answers)
    assert client.select("SELECT 1").rows == [{"n": "5"}]
    assert calls["logins"] == 2 and calls["queries"] == 2


def test_token_is_renewed_before_it_expires():
    client, calls = session_client(lambda auth, logins: httpx.Response(200, json={"records": [], "totalCount": 0}))
    calls["token"] = lambda n: fake_jwt(30 if n == 1 else 900)  # first token is about to expire
    client.select("SELECT 1")
    client.select("SELECT 1")
    assert calls["logins"] == 2 and calls["queries"] == 2  # renewed before the second query, no failed call


def test_a_real_query_error_does_not_trigger_a_new_sign_in():
    client, calls = session_client(lambda auth, logins: httpx.Response(400, json={"msg": "table is not found : x"}))
    with pytest.raises(ZetarisError, match="table is not found"):
        client.select("SELECT * FROM x")
    assert calls["logins"] == 1 and calls["queries"] == 1

# --- API key mode (the setup guide's REST route), against a fake Zetaris -------------

def api_key_client(handler):
        return ZetarisClient("http://zetaris.test/api/v1.0", api_key="secret-key", org_id="1",
                             transport=httpx.MockTransport(handler))


def test_api_key_mode_sends_bearer_key_and_org_to_the_select_only_endpoint():
        seen = {}

        def handler(request):
                seen.update(path=request.url.path, auth=request.headers["Authorization"], org=request.headers["X-Org-ID"])
                return httpx.Response(200, json={"records": [{"one": "1"}], "totalCount": 1})

        result = api_key_client(handler).select("SELECT 1 AS one")
        assert result.rows == [{"one": "1"}]
        assert seen == {"path": "/api/v1.0/query/sql/start", "auth": "Bearer secret-key", "org": "1"}


def test_api_key_mode_metadata_uses_the_guide_run_endpoint():
        def handler(request):
                assert request.url.path == "/api/v1.0/sql-editor/sqls/run"
                return httpx.Response(200, json={"headers": ["col_name"], "data": [["order_id"]]})

        assert api_key_client(handler).metadata("DESCRIBE t") == [{"col_name": "order_id"}]


@pytest.mark.parametrize("status, body", [(401, {"msg": "Unauthorized"}), (400, {"msg": "Not Allowed"})])
def test_rejected_api_key_is_a_clear_error_without_the_key(status, body):
        client = api_key_client(lambda request: httpx.Response(status, json=body))
        with pytest.raises(ZetarisError, match="rejected the API key") as exc:
                client.select("SELECT 1")
        assert "secret-key" not in str(exc.value) and "secret-key" not in repr(client)


def test_from_env_prefers_the_api_key(monkeypatch):
        monkeypatch.setenv("ZETARIS_API_KEY", "k")
        monkeypatch.setenv("ZETARIS_REST_URL", "http://zetaris.test/api/v1.0")
        monkeypatch.delenv("ZETARIS_ORG_ID", raising=False)
        client = ZetarisClient.from_env()
        assert client.mode == "api-key" and client.base_url == "http://zetaris.test/api/v1.0" and client._org_id == "1"
