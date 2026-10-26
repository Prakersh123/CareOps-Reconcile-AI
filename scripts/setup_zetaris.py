"""Register CareOps' synthetic data sources in Zetaris and build the USL table the agents query.

    python scripts/setup_zetaris.py                       set everything up (skips what already exists)
    python scripts/setup_zetaris.py --check               only check that the USL table answers
    python scripts/setup_zetaris.py --prefix t_ ...       use prefixed names (t_HOSPITAL_RECON, t_careops ...) for a trial
    python scripts/setup_zetaris.py --prefix t_ --remove  remove what a prefixed trial created

What it does, following the hackathon setup guide (Lightning SQL, one statement at a time):
1. Billing system: starts a PostgreSQL container on the Zetaris Docker network (or reuses it) and
   (re)loads data/billing_records.sql, so the table always matches the file.
2. Lab and history files: copies data/lab_orders.csv and data/billing_history.csv into Zetaris'
   shared storage (/home/zetaris/customer-data/shared/careops/), since Zetaris cannot read files
   on your laptop.
3. Registers them: CREATE LIGHTNING DATABASE + FILESTORE TABLEs for the CSVs (re-created when a
   CSV has changed), CREATE DATASOURCE + REGISTER DATASOURCE TABLE for PostgreSQL.
4. Unified Semantic Layer: CREATE NAMESPACE, COMPILE USL, ACTIVATE USL TABLE joining the three sources.
5. Checks that the USL table returns the expected 10 rows (8 lab orders; L105 is billed twice,
   L103 has a cancelled invoice and its replacement, L102 and L108 join to no invoice).

Needs: Docker, a running Zetaris platform, and admin access to it in .env (ZETARIS_API_KEY, or
ZETARIS_USERNAME/ZETARIS_PASSWORD). The billing database password is BILLING_DB_PASSWORD (default
below; the data is synthetic and the database is only reachable on the Docker network).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app.adapters.zetaris_client import ZetarisClient, ZetarisError  # noqa: E402

SHARED = "/home/zetaris/customer-data/shared"
USL_TABLE = "CAREOPS_RECONCILIATION"
COLUMNS = ["order_id", "patient_id", "test_code", "order_date", "lab_status", "invoice_id", "invoice_status",
           "payment_status", "event_type", "event_date", "notes"]
# I109 is keyed 'L-108' in billing, so L108 joins to nothing: the lab system and billing disagree on the id.
EXPECTED = {("L101", "I101"), ("L102", None), ("L103", "I102"), ("L103", "I104"), ("L104", "I103"),
            ("L105", "I105"), ("L105", "I106"), ("L106", "I107"), ("L107", "I108"), ("L108", None)}


def say(message: str) -> None:
        print(f"[setup] {message}", flush=True)


# ------------------------------------------------------------------- docker

def docker(*args: str, stdin: bytes | None = None, check: bool = True) -> str:
        result = subprocess.run(["docker", *args], input=stdin, capture_output=True)
        if check and result.returncode != 0:
                raise SystemExit(f"[setup] docker {args[0]} failed: {result.stderr.decode().strip()[:300]}")
        return result.stdout.decode().strip()


def zetaris_driver() -> str:
        """The Zetaris query-engine container (it reads the shared files and connects to PostgreSQL)."""
        name = os.environ.get("ZETARIS_DRIVER_CONTAINER") or next(
                (n for n in docker("ps", "--filter", "label=com.docker.compose.service=driver",
                                   "--format", "{{.Names}}").splitlines() if "zetaris" in n), "")
        if not name:
                raise SystemExit("[setup] no running Zetaris driver container found; start Zetaris first "
                                 "(or set ZETARIS_DRIVER_CONTAINER)")
        return name


def network_of(container: str) -> str:
        return docker("inspect", container, "--format", "{{range $n, $v := .NetworkSettings.Networks}}{{$n}} {{end}}").split()[0]


def container_env(container: str) -> dict[str, str]:
        lines = docker("inspect", container, "--format", "{{range .Config.Env}}{{println .}}{{end}}").splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)


def ensure_billing_db(name: str, network: str) -> dict[str, str]:
        """Start (or reuse) the billing PostgreSQL container; return its connection settings."""
        exists = name in docker("ps", "-a", "--format", "{{.Names}}").splitlines()
        if exists:
                say(f"billing database: reusing container {name}")
                if docker("inspect", name, "--format", "{{.State.Running}}") != "true":
                        docker("start", name)
                if network not in docker("inspect", name, "--format", "{{json .NetworkSettings.Networks}}"):
                        docker("network", "connect", network, name)
        else:
                say(f"billing database: starting container {name} on network {network}")
                docker("run", "-d", "--name", name, "--network", network,
                       "-e", "POSTGRES_USER=careops", "-e", "POSTGRES_DB=careops_billing",
                       "-e", f"POSTGRES_PASSWORD={os.environ.get('BILLING_DB_PASSWORD', 'careops-synthetic-data')}",
                       "-v", f"{ROOT / 'data' / 'billing_records.sql'}:/docker-entrypoint-initdb.d/01_billing_records.sql:ro",
                       "-v", f"{name}-pgdata:/var/lib/postgresql/data", "postgres:16-alpine")
        env = container_env(name)
        settings = {"host": name, "user": env.get("POSTGRES_USER", "postgres"),
                    "db": env.get("POSTGRES_DB") or env.get("POSTGRES_USER", "postgres"),
                    "password": env.get("POSTGRES_PASSWORD", "")}
        for _ in range(60):  # the first start runs the init script, then restarts the server
                count = docker("exec", name, "psql", "-U", settings["user"], "-d", settings["db"], "-Atc",
                               "SELECT count(*) FROM billing_records", check=False)
                if count.isdigit():
                        load_billing_records(name, settings)
                        return settings
                time.sleep(1)
        raise SystemExit("[setup] the billing database did not become ready")


def load_billing_records(name: str, settings: dict[str, str]) -> None:
        """Replace the billing table's rows with data/billing_records.sql, in one transaction.

        The init script only runs when the container is first created, so an existing container
        would keep old rows after the data file changes.
        """
        sql = (ROOT / "data" / "billing_records.sql").read_bytes()
        empty_first = (b"DO $$ BEGIN IF to_regclass('public.billing_records') IS NOT NULL "
                       b"THEN TRUNCATE billing_records; END IF; END $$;\n")
        docker("exec", "-i", name, "psql", "-q", "-v", "ON_ERROR_STOP=1", "-U", settings["user"], "-d", settings["db"],
               stdin=b"BEGIN;\n" + empty_first + sql + b"\nCOMMIT;\n")
        count = docker("exec", name, "psql", "-U", settings["user"], "-d", settings["db"], "-Atc",
                       "SELECT count(*) FROM billing_records")
        say(f"billing database ready: {count} invoice(s) loaded from data/billing_records.sql")


def copy_csvs(driver: str, folder: str) -> None:
        for name in ("lab_orders.csv", "billing_history.csv"):
                docker("exec", "-i", driver, "sh", "-c", f"mkdir -p {folder} && cat > {folder}/{name}",
                       stdin=(ROOT / "data" / name).read_bytes())
        say(f"copied lab_orders.csv and billing_history.csv to {driver}:{folder}")


# ------------------------------------------------------------------ zetaris

def names(rows: list[dict]) -> set[str]:
        return {str(r.get("name")) for r in rows}


def _rows_in(z: ZetarisClient, table: str) -> int:
        return int(z.select(f"SELECT COUNT(*) AS n FROM {table}").rows[0]["n"])


def _rows_in_file(table: str) -> int:
        lines = (ROOT / "data" / f"{table}.csv").read_text(encoding="utf-8").splitlines()
        return sum(1 for line in lines[1:] if line.strip())


def run(z: ZetarisClient, sql: str, show: str | None = None) -> None:
        say(f"  {show or ' '.join(sql.split())[:160]}")
        z.execute_admin(sql)


def register(z: ZetarisClient, prefix: str, folder: str, db: dict[str, str]) -> str:
        lightning_db, datasource, namespace = f"{prefix}HOSPITAL_RECON", f"{prefix}BILLING_POSTGRES", f"lightning.metastore.{prefix.lower()}careops"

        if lightning_db not in names(z.metadata("SHOW LIGHTNING DATABASES")):
                run(z, f'CREATE LIGHTNING DATABASE {lightning_db} DESCRIBE BY "Synthetic hospital lab orders and billing '
                       f'history for CareOps Reconcile AI"')
        tables = names(z.metadata(f"SHOW LIGHTNING TABLES {lightning_db}"))
        for table in ("lab_orders", "billing_history"):
                if table in tables and _rows_in(z, f"{lightning_db}.{table}") != _rows_in_file(table):
                        # Zetaris keeps a file table's rows from when it was created (REFRESH TABLE does not
                        # reload them), so a changed CSV needs the table re-created. The USL query over it
                        # keeps working.
                        run(z, f"DROP LIGHTNING DATABASE TABLE {lightning_db}.{table}")
                        tables.discard(table)
                if table not in tables:
                        run(z, f'CREATE LIGHTNING FILESTORE TABLE {table} FROM {lightning_db} FORMAT CSV OPTIONS ('
                               f'path "{folder}/{table}.csv", header "true", delimiter ",")')
        say(f"lab orders + billing history: {lightning_db}.lab_orders, {lightning_db}.billing_history")

        if datasource not in names(z.metadata("SHOW DATASOURCES")):
                run(z, f'CREATE DATASOURCE {datasource} DESCRIBE BY "PostgreSQL" OPTIONS (jdbcdriver "org.postgresql.Driver", '
                       f'jdbcurl "jdbc:postgresql://{db["host"]}:5432/{db["db"]}", username "{db["user"]}", '
                       f'password "{db["password"]}")',
                    show=f"CREATE DATASOURCE {datasource} ... jdbc:postgresql://{db['host']}:5432/{db['db']} (password hidden)")
        if "billing_records" not in names(z.metadata(f"SHOW DATASOURCE TABLES {datasource}")):
                run(z, f'REGISTER DATASOURCE TABLE "billing_records" FROM {datasource}')
        say(f"billing records: {datasource}.billing_records")

        run(z, f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
        usls = {r.get("name"): r for r in z.metadata(f"SHOW NAMESPACES OR TABLES IN {namespace}")}
        if USL_TABLE not in usls:
                ddl = ",\n  ".join(f"{c} STRING" for c in COLUMNS)
                run(z, f"COMPILE USL {USL_TABLE} DEPLOY NAMESPACE {namespace} DDL\nCREATE TABLE {USL_TABLE} (\n  {ddl}\n)",
                    show=f"COMPILE USL {USL_TABLE} DEPLOY NAMESPACE {namespace} DDL CREATE TABLE {USL_TABLE} (11 STRING columns)")
        table_path = f"{namespace}.{USL_TABLE}.{USL_TABLE}"
        activated = any(str(r.get("activated")).lower() == "true"
                        for r in z.metadata(f"SHOW NAMESPACES OR TABLES IN {namespace}.{USL_TABLE}"))
        if not activated:
                source = {"l": ["order_id", "patient_id", "test_code", "order_date", "lab_status"],
                          "b": ["invoice_id", "invoice_status", "payment_status"],
                          "h": ["event_type", "event_date", "notes"]}
                select = ", ".join(f"CAST({alias}.{c} AS STRING) AS {c}" for alias, cols in source.items() for c in cols)
                run(z, f"ACTIVATE USL TABLE {table_path} AS SELECT {select} "
                       f"FROM {lightning_db}.lab_orders l "
                       f"LEFT JOIN {datasource}.billing_records b ON l.order_id = b.order_id "
                       f"LEFT JOIN {lightning_db}.billing_history h ON l.order_id = h.order_id AND b.invoice_id = h.invoice_id",
                    show=f"ACTIVATE USL TABLE {table_path} AS lab_orders LEFT JOIN billing_records LEFT JOIN billing_history")
        say(f"USL table: {table_path}")
        return table_path


def check(z: ZetarisClient, table_path: str) -> bool:
        rows = z.select(f"SELECT order_id, invoice_id, invoice_status, payment_status, event_type FROM {table_path} "
                        f"ORDER BY order_id, invoice_id").rows
        for r in rows:
                say(f"  {r['order_id']}  {r['invoice_id'] or '-':5} {r['invoice_status'] or '-':10} "
                    f"{r['payment_status'] or '-':7} {r['event_type'] or ''}")
        got = {(r["order_id"], r["invoice_id"]) for r in rows}
        ok = got == EXPECTED
        say(f"{'OK' if ok else 'UNEXPECTED'}: {len(rows)} row(s) from {table_path}")
        return ok


def remove(z: ZetarisClient, prefix: str, folder: str, container: str, driver: str) -> None:
        namespace = f"lightning.metastore.{prefix.lower()}careops"
        for sql in (f"REMOVE USL {USL_TABLE} NAMESPACE {namespace}", f"DROP NAMESPACE {namespace} CASCADE",
                    f"DROP DATASOURCE {prefix}BILLING_POSTGRES",
                    f"DROP LIGHTNING DATABASE TABLE {prefix}HOSPITAL_RECON.lab_orders",
                    f"DROP LIGHTNING DATABASE TABLE {prefix}HOSPITAL_RECON.billing_history",
                    f"DROP LIGHTNING DATABASE {prefix}HOSPITAL_RECON"):
                try:
                        run(z, sql)
                except ZetarisError as exc:
                        say(f"  (skipped: {exc})")
        docker("rm", "-f", container, check=False)
        docker("volume", "rm", f"{container}-pgdata", check=False)
        docker("exec", driver, "rm", "-rf", folder, check=False)
        say(f"removed container {container}, its volume, and {folder}")


def main() -> int:
        parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
        parser.add_argument("--prefix", default="", help="prefix for every Zetaris object, container and folder (for trials)")
        parser.add_argument("--check", action="store_true", help="only check the USL table")
        parser.add_argument("--remove", action="store_true", help="remove what a --prefix trial created")
        args = parser.parse_args()

        prefix = args.prefix
        folder = f"{SHARED}/{prefix}careops"
        container = f"{prefix.replace('_', '-')}careops-billing-postgres"
        z = ZetarisClient.from_env()
        say(f"Zetaris: {z.base_url} ({z.mode})")

        if args.remove:
                if not prefix:
                        raise SystemExit("[setup] --remove only works with --prefix, so it can never delete the real setup")
                remove(z, prefix, folder, container, zetaris_driver())
                return 0
        if args.check:
                return 0 if check(z, f"lightning.metastore.{prefix.lower()}careops.{USL_TABLE}.{USL_TABLE}") else 1

        driver = zetaris_driver()
        db = ensure_billing_db(container, network_of(driver))
        copy_csvs(driver, folder)
        try:
                table_path = register(z, prefix, folder, db)
        except ZetarisError as exc:
                raise SystemExit(f"[setup] Zetaris refused a step: {exc}")
        return 0 if check(z, table_path) else 1


if __name__ == "__main__":
        raise SystemExit(main())
