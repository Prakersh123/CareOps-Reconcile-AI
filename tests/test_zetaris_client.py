import httpx
import pytest

from careops.zetaris_client import ZetarisClient, ZetarisError

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


def test_usl_returns_five_rows(live_client):
    result = live_client.select(f"SELECT * FROM {USL_TABLE}")
    assert result.total_count == 5
    assert not result.truncated
    assert {r["order_id"] for r in result.rows} == {"L101", "L102", "L103", "L104"}


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


def test_bad_password_is_clean_error():
    client = ZetarisClient.from_env()
    client._password = "wrong-password"
    with pytest.raises(ZetarisError, match="login failed") as exc:
        client.select("SELECT 1")
    assert "wrong-password" not in str(exc.value)
