"""Create the mutualtrack database, tables, and demo data."""
import logging
from pathlib import Path

from mysql.connector import Error as MySQLError

from config import Config
from db import _connect, get_conn
from logic import apply_holding

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("setup_db")
ROOT = Path(__file__).parent
SQL_DIR = ROOT / "database"


def run_sql_file(conn, path: Path):
    sql = path.read_text(encoding="utf-8")
    cursor = conn.cursor()
    for result in cursor.execute(sql, multi=True):
        _ = result
    cursor.close()


def rebuild_holdings():
    with get_conn() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("DELETE FROM portfolio_holding")
        cur.execute(
            """
            SELECT investor_id, scheme_id, txn_type, amount, units
            FROM transaction
            ORDER BY txn_date, transaction_id
            """
        )
        txns = cur.fetchall()
        holdings = {}
        for txn in txns:
            key = (txn["investor_id"], txn["scheme_id"])
            current = holdings.get(key, {"units": 0, "invested_amount": 0})
            holdings[key] = apply_holding(current, txn["txn_type"], txn["amount"], txn["units"])
        for (investor_id, scheme_id), row in holdings.items():
            if row["units"] <= 0:
                continue
            cur.execute(
                """
                INSERT INTO portfolio_holding (investor_id, scheme_id, units, invested_amount)
                VALUES (%s, %s, %s, %s)
                """,
                (investor_id, scheme_id, row["units"], row["invested_amount"]),
            )
        cur.close()


def main():
    logger.info("Connecting to MySQL at %s:%s", Config.DB_HOST, Config.DB_PORT)
    try:
        conn = _connect(database=None)
    except Exception as exc:
        raise SystemExit(
            "Could not connect to MySQL. Copy .env.example to .env and set DB_PASSWORD."
        ) from exc

    cur = conn.cursor()
    cur.execute(
        f"CREATE DATABASE IF NOT EXISTS `{Config.DB_NAME}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    conn.commit()
    cur.close()
    conn.close()

    conn = _connect(Config.DB_NAME)
    try:
        logger.info("Applying schema.sql")
        run_sql_file(conn, SQL_DIR / "schema.sql")
        conn.commit()
        logger.info("Applying seed.sql")
        run_sql_file(conn, SQL_DIR / "seed.sql")
        conn.commit()
    except MySQLError:
        conn.rollback()
        logger.exception("Failed while loading SQL")
        raise
    finally:
        conn.close()

    logger.info("Rebuilding portfolio holdings from transactions")
    rebuild_holdings()
    logger.info("Database '%s' is ready.", Config.DB_NAME)


if __name__ == "__main__":
    main()
