import logging
from contextlib import contextmanager

import mysql.connector
from mysql.connector import Error as MySQLError

from config import Config

logger = logging.getLogger(__name__)


class DatabaseUnavailable(Exception):
    pass


def _connect(database=None):
    try:
        return mysql.connector.connect(
            host=Config.DB_HOST,
            port=Config.DB_PORT,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            database=database,
            autocommit=False,
        )
    except MySQLError as exc:
        logger.exception("MySQL connection failed")
        raise DatabaseUnavailable(
            "Database connection unavailable. Please check the server configuration."
        ) from exc


@contextmanager
def get_conn(database=Config.DB_NAME):
    conn = _connect(database)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query_all(sql, params=None):
    with get_conn() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        cur.close()
        return rows


def query_one(sql, params=None):
    rows = query_all(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None, conn=None):
    own = conn is None
    if own:
        conn = _connect(Config.DB_NAME)
    cur = conn.cursor(dictionary=True)
    cur.execute(sql, params or ())
    last_id = cur.lastrowid
    cur.close()
    if own:
        conn.commit()
        conn.close()
    return last_id
