import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.json.provider import DefaultJSONProvider

from mysql.connector.errors import IntegrityError, OperationalError

from config import Config
from db import DatabaseUnavailable, execute, get_conn, query_all, query_one
from logic import (
    apply_holding,
    calc_units,
    format_inr,
    format_pct,
    monthly_equivalent,
    next_due_after,
    progress_pct,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mutualtrack")


class JSONProvider(DefaultJSONProvider):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, date):
            return o.isoformat()
        return super().default(o)


app = Flask(__name__)
app.json = JSONProvider(app)
app.secret_key = Config.SECRET_KEY

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
PHONE_RE = re.compile(r"^[6-9]\d{9}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def json_error(message, status=400, extra=None):
    payload = {"ok": False, "error": message}
    if extra:
        payload.update(extra)
    return jsonify(payload), status


def api_handler(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DatabaseUnavailable as exc:
            logger.exception("Database unavailable")
            return json_error(str(exc), 503)
        except OperationalError:
            logger.exception("MySQL operational error")
            return json_error(
                "Database connection unavailable. Please check the server configuration.",
                503,
            )
        except IntegrityError:
            logger.exception("Integrity error")
            return json_error("This record conflicts with existing data. Check unique fields such as PAN, email, or scheme name.")
        except ValueError as exc:
            return json_error(str(exc), 400)
        except Exception:
            logger.exception("Unhandled API error")
            return json_error("Something went wrong. Please try again.", 500)

    return wrapper


def current_investor_id():
    iid = session.get("investor_id")
    if iid:
        return int(iid)
    row = query_one("SELECT investor_id FROM investor ORDER BY investor_id LIMIT 1")
    if not row:
        return None
    session["investor_id"] = row["investor_id"]
    return row["investor_id"]


def get_investor(investor_id=None):
    iid = investor_id or current_investor_id()
    if not iid:
        return None
    return query_one("SELECT * FROM investor WHERE investor_id=%s", (iid,))


def parse_decimal(value, field, positive=True):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError):
        raise ValueError(f"{field} must be a valid number.")
    if positive and number <= 0:
        raise ValueError(f"{field} must be greater than 0.")
    if not positive and number < 0:
        raise ValueError(f"{field} cannot be negative.")
    return number


def parse_date(value, field="Date"):
    if not value:
        raise ValueError(f"{field} is required.")
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{field} must be a valid date.")


def require_existing(table, pk, value, label):
    row = query_one(f"SELECT * FROM {table} WHERE {pk}=%s", (value,))
    if not row:
        raise ValueError(f"{label} does not exist.")
    return row


def latest_nav(scheme_id, conn=None):
    sql = """
        SELECT nav_value, nav_date
        FROM nav_history
        WHERE scheme_id=%s
        ORDER BY nav_date DESC, nav_id DESC
        LIMIT 1
    """
    if conn is None:
        return query_one(sql, (scheme_id,))
    cur = conn.cursor(dictionary=True)
    cur.execute(sql, (scheme_id,))
    row = cur.fetchone()
    cur.close()
    return row


def upsert_holding(conn, investor_id, scheme_id, txn_type, amount, units):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT * FROM portfolio_holding
        WHERE investor_id=%s AND scheme_id=%s
        FOR UPDATE
        """,
        (investor_id, scheme_id),
    )
    holding = cur.fetchone()
    updated = apply_holding(holding, txn_type, amount, units)
    if holding:
        if updated["units"] <= 0:
            cur.execute(
                "DELETE FROM portfolio_holding WHERE holding_id=%s",
                (holding["holding_id"],),
            )
        else:
            cur.execute(
                """
                UPDATE portfolio_holding
                SET units=%s, invested_amount=%s
                WHERE holding_id=%s
                """,
                (updated["units"], updated["invested_amount"], holding["holding_id"]),
            )
    else:
        if txn_type == "Redemption":
            raise ValueError("No holdings found for this scheme.")
        cur.execute(
            """
            INSERT INTO portfolio_holding (investor_id, scheme_id, units, invested_amount)
            VALUES (%s, %s, %s, %s)
            """,
            (investor_id, scheme_id, updated["units"], updated["invested_amount"]),
        )
    cur.close()
    return updated


def insert_transaction(conn, investor_id, scheme_id, sip_id, txn_type, txn_date, amount, nav, units):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO transaction
            (investor_id, scheme_id, sip_id, txn_type, txn_date, amount, nav, units)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (investor_id, scheme_id, sip_id, txn_type, txn_date, amount, nav, units),
    )
    txn_id = cur.lastrowid
    cur.close()
    return txn_id


def serialize_holding(row):
    units = Decimal(str(row["units"]))
    invested = Decimal(str(row["invested_amount"]))
    nav = Decimal(str(row["current_nav"] or 0))
    current_value = units * nav
    profit = current_value - invested
    ret = (profit / invested * 100) if invested > 0 else Decimal("0")
    return {
        **row,
        "units": float(units),
        "avg_nav": float(invested / units) if units else 0,
        "current_nav": float(nav),
        "invested": float(invested),
        "current_value": float(current_value),
        "profit": float(profit),
        "return_pct": float(ret),
        "invested_fmt": format_inr(invested),
        "current_value_fmt": format_inr(current_value),
        "profit_fmt": format_inr(profit),
        "return_fmt": format_pct(ret),
        "avg_nav_fmt": format_inr(invested / units) if units else "—",
        "current_nav_fmt": format_inr(nav),
    }


HOLDINGS_SQL = """
    SELECT h.holding_id, h.investor_id, h.scheme_id, h.units, h.invested_amount,
           s.name AS scheme_name, s.category, a.name AS amc_name,
           (SELECT n.nav_value FROM nav_history n
            WHERE n.scheme_id = h.scheme_id
            ORDER BY n.nav_date DESC, n.nav_id DESC LIMIT 1) AS current_nav
    FROM portfolio_holding h
    JOIN mutual_fund_scheme s ON s.scheme_id = h.scheme_id
    JOIN amc a ON a.amc_id = s.amc_id
    WHERE h.investor_id = %s AND h.units > 0
"""


def get_holdings(investor_id):
    rows = query_all(HOLDINGS_SQL, (investor_id,))
    return [serialize_holding(r) for r in rows]


def dashboard_payload(investor_id):
    holdings = get_holdings(investor_id)
    total_value = sum(h["current_value"] for h in holdings)
    total_invested = sum(h["invested"] for h in holdings)
    profit = total_value - total_invested
    ret = (profit / total_invested * 100) if total_invested else 0
    alloc = {"Equity": 0.0, "Debt": 0.0, "Hybrid": 0.0}
    for h in holdings:
        alloc[h["category"]] = alloc.get(h["category"], 0) + h["current_value"]
    sips = query_all(
        """
        SELECT sip.*, s.name AS scheme_name, a.name AS amc_name
        FROM sip
        JOIN mutual_fund_scheme s ON s.scheme_id = sip.scheme_id
        JOIN amc a ON a.amc_id = s.amc_id
        WHERE sip.investor_id=%s
        """,
        (investor_id,),
    )
    active = [s for s in sips if s["status"] == "Active"]
    monthly = sum(float(monthly_equivalent(s["amount"], s["frequency"])) for s in active)
    goals = query_all(
        """
        SELECT g.*,
               GROUP_CONCAT(CONCAT(s.name, ' (', gs.allocation_pct, '%)')
                            ORDER BY gs.allocation_pct DESC SEPARATOR ', ') AS linked
        FROM goal g
        LEFT JOIN goal_scheme gs ON gs.goal_id = g.goal_id
        LEFT JOIN mutual_fund_scheme s ON s.scheme_id = gs.scheme_id
        WHERE g.investor_id=%s
        GROUP BY g.goal_id
        ORDER BY g.target_date
        """,
        (investor_id,),
    )
    for g in goals:
        g["progress"] = progress_pct(g["current_amount"], g["target_amount"])
        g["current_fmt"] = format_inr(g["current_amount"])
        g["target_fmt"] = format_inr(g["target_amount"])
    txns = query_all(
        """
        SELECT t.*, s.name AS scheme_name
        FROM transaction t
        JOIN mutual_fund_scheme s ON s.scheme_id = t.scheme_id
        WHERE t.investor_id=%s
        ORDER BY t.txn_date DESC, t.transaction_id DESC
        LIMIT 8
        """,
        (investor_id,),
    )
    for t in txns:
        t["amount_fmt"] = format_inr(t["amount"])
        t["nav_fmt"] = format_inr(t["nav"])
        t["date_fmt"] = t["txn_date"].strftime("%d %b %Y")
        t["units"] = float(t["units"])
    return {
        "kpis": {
            "portfolio_value": total_value,
            "portfolio_value_fmt": format_inr(total_value),
            "invested": total_invested,
            "invested_fmt": format_inr(total_invested),
            "profit": profit,
            "profit_fmt": format_inr(profit),
            "return_pct": ret,
            "return_fmt": format_pct(ret),
            "active_sips": len(active),
            "monthly_sip": monthly,
            "monthly_sip_fmt": format_inr(monthly),
            "holding_count": len(holdings),
        },
        "allocation": {k: v for k, v in alloc.items()},
        "goals": goals,
        "holdings": holdings,
        "transactions": txns,
        "performance": {
            "invested": total_invested,
            "current": total_value,
        },
    }


def page_context(page_name):
    investors = []
    investor = None
    db_ok = True
    db_error = None
    try:
        investors = query_all("SELECT investor_id, name, pan FROM investor ORDER BY investor_id")
        investor = get_investor()
    except DatabaseUnavailable as exc:
        db_ok = False
        db_error = str(exc)
    return {
        "page": page_name,
        "investor": investor,
        "investors": investors,
        "db_ok": db_ok,
        "db_error": db_error,
        "format_inr": format_inr,
        "format_pct": format_pct,
    }


@app.context_processor
def inject_helpers():
    return {"format_inr": format_inr, "format_pct": format_pct}


@app.errorhandler(DatabaseUnavailable)
def handle_db_down(exc):
    logger.exception("Database unavailable")
    if request.path.startswith("/api/"):
        return json_error(str(exc), 503)
    return render_template(
        "error.html",
        message="Database connection unavailable. Please check the server configuration.",
        **page_context("error"),
    ), 503


@app.errorhandler(Exception)
def handle_any(exc):
    if isinstance(exc, DatabaseUnavailable):
        return handle_db_down(exc)
    logger.exception("Unhandled error")
    if request.path.startswith("/api/"):
        return json_error("Something went wrong. Please try again.", 500)
    ctx = page_context("error")
    return render_template(
        "error.html",
        message="An unexpected error occurred. Please try again.",
        **ctx,
    ), 500


@app.route("/")
def home():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html", **page_context("dashboard"))


@app.route("/portfolio")
def portfolio():
    return render_template("portfolio.html", **page_context("portfolio"))


@app.route("/sips")
def sips():
    return render_template("sips.html", **page_context("sips"))


@app.route("/transactions")
def transactions():
    return render_template("transactions.html", **page_context("transactions"))


@app.route("/goals")
def goals():
    return render_template("goals.html", **page_context("goals"))


@app.route("/schemes")
def schemes():
    return render_template("schemes.html", **page_context("schemes"))


@app.route("/amcs")
def amcs():
    return render_template("amcs.html", **page_context("amcs"))


@app.route("/nav")
def nav_page():
    return render_template("nav.html", **page_context("nav"))


@app.route("/investor")
def investor_page():
    return render_template("investor.html", **page_context("investor"))


@app.post("/api/session/investor")
@api_handler
def api_set_investor():
    data = request.get_json(force=True)
    investor = require_existing("investor", "investor_id", int(data["investor_id"]), "Investor")
    session["investor_id"] = investor["investor_id"]
    return jsonify({"ok": True, "investor": investor})


@app.get("/api/lookups")
@api_handler
def api_lookups():
    return jsonify(
        {
            "ok": True,
            "investors": query_all("SELECT investor_id, name, pan FROM investor ORDER BY name"),
            "amcs": query_all("SELECT amc_id, name FROM amc ORDER BY name"),
            "schemes": query_all(
                """
                SELECT s.scheme_id, s.name, s.category, s.status, a.name AS amc_name, a.amc_id
                FROM mutual_fund_scheme s
                JOIN amc a ON a.amc_id = s.amc_id
                ORDER BY s.name
                """
            ),
            "sips": query_all(
                """
                SELECT sip.sip_id, sip.investor_id, sip.scheme_id, sip.amount, sip.status,
                       s.name AS scheme_name
                FROM sip
                JOIN mutual_fund_scheme s ON s.scheme_id = sip.scheme_id
                ORDER BY sip.sip_id
                """
            ),
        }
    )


@app.get("/api/dashboard")
@api_handler
def api_dashboard():
    iid = current_investor_id()
    return jsonify({"ok": True, **dashboard_payload(iid)})


@app.get("/api/portfolio")
@api_handler
def api_portfolio():
    iid = current_investor_id()
    return jsonify({"ok": True, "holdings": get_holdings(iid)})


@app.get("/api/sips")
@api_handler
def api_sips():
    iid = current_investor_id()
    rows = query_all(
        """
        SELECT sip.*, s.name AS scheme_name, a.name AS amc_name, i.name AS investor_name
        FROM sip
        JOIN mutual_fund_scheme s ON s.scheme_id = sip.scheme_id
        JOIN amc a ON a.amc_id = s.amc_id
        JOIN investor i ON i.investor_id = sip.investor_id
        WHERE sip.investor_id=%s
        ORDER BY FIELD(sip.status, 'Active', 'Paused', 'Stopped'), sip.sip_id
        """,
        (iid,),
    )
    for r in rows:
        r["amount_fmt"] = format_inr(r["amount"])
        r["start_fmt"] = r["start_date"].strftime("%d %b %Y")
        r["next_fmt"] = r["next_due_date"].strftime("%d %b %Y") if r["next_due_date"] else "—"
        r["amount"] = float(r["amount"])
    return jsonify({"ok": True, "sips": rows})


def sip_from_payload(data, existing=None):
    investor_id = int(data.get("investor_id") or current_investor_id())
    scheme_id = int(data.get("scheme_id") or 0)
    require_existing("investor", "investor_id", investor_id, "Investor")
    scheme = require_existing("mutual_fund_scheme", "scheme_id", scheme_id, "Scheme")
    if scheme["status"] != "Active":
        raise ValueError("Scheme is not active.")
    amount = parse_decimal(data.get("amount"), "Amount")
    frequency = data.get("frequency") or "Monthly"
    if frequency not in ("Weekly", "Monthly", "Quarterly"):
        raise ValueError("Frequency is invalid.")
    start_date = parse_date(data.get("start_date"), "Start date")
    status = data.get("status") or (existing["status"] if existing else "Active")
    if status not in ("Active", "Paused", "Stopped"):
        raise ValueError("Status is invalid.")
    next_due = start_date if status == "Active" else (existing["next_due_date"] if existing else None)
    if existing and status == "Active" and existing.get("next_due_date"):
        next_due = existing["next_due_date"]
    return investor_id, scheme_id, amount, frequency, start_date, next_due, status


@app.post("/sips/add")
@app.post("/api/sips")
@api_handler
def api_add_sip():
    data = request.get_json(force=True)
    investor_id, scheme_id, amount, frequency, start_date, next_due, status = sip_from_payload(data)
    sip_id = execute(
        """
        INSERT INTO sip (investor_id, scheme_id, amount, frequency, start_date, next_due_date, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (investor_id, scheme_id, amount, frequency, start_date, next_due, status),
    )
    return jsonify({"ok": True, "message": "SIP created successfully", "sip_id": sip_id})


@app.post("/sips/<int:sip_id>/edit")
@app.post("/api/sips/<int:sip_id>")
@api_handler
def api_edit_sip(sip_id):
    existing = require_existing("sip", "sip_id", sip_id, "SIP")
    data = request.get_json(force=True)
    investor_id, scheme_id, amount, frequency, start_date, next_due, status = sip_from_payload(data, existing)
    execute(
        """
        UPDATE sip
        SET investor_id=%s, scheme_id=%s, amount=%s, frequency=%s, start_date=%s,
            next_due_date=%s, status=%s
        WHERE sip_id=%s
        """,
        (investor_id, scheme_id, amount, frequency, start_date, next_due, status, sip_id),
    )
    return jsonify({"ok": True, "message": "SIP updated successfully"})


def set_sip_status(sip_id, new_status):
    sip = require_existing("sip", "sip_id", sip_id, "SIP")
    execute("UPDATE sip SET status=%s WHERE sip_id=%s", (new_status, sip_id))
    return sip


@app.post("/sips/<int:sip_id>/pause")
@api_handler
def api_pause_sip(sip_id):
    sip = require_existing("sip", "sip_id", sip_id, "SIP")
    if sip["status"] != "Active":
        raise ValueError("Only an active SIP can be paused.")
    set_sip_status(sip_id, "Paused")
    return jsonify({"ok": True, "message": "SIP paused successfully"})


@app.post("/sips/<int:sip_id>/resume")
@api_handler
def api_resume_sip(sip_id):
    sip = require_existing("sip", "sip_id", sip_id, "SIP")
    if sip["status"] != "Paused":
        raise ValueError("Only a paused SIP can be resumed.")
    nxt = sip["next_due_date"] or date.today()
    execute("UPDATE sip SET status='Active', next_due_date=%s WHERE sip_id=%s", (nxt, sip_id))
    return jsonify({"ok": True, "message": "SIP resumed successfully"})


@app.post("/sips/<int:sip_id>/stop")
@api_handler
def api_stop_sip(sip_id):
    sip = require_existing("sip", "sip_id", sip_id, "SIP")
    if sip["status"] == "Stopped":
        raise ValueError("SIP is already stopped.")
    execute(
        "UPDATE sip SET status='Stopped', next_due_date=NULL WHERE sip_id=%s",
        (sip_id,),
    )
    return jsonify({"ok": True, "message": "SIP stopped successfully"})


@app.post("/api/sips/<int:sip_id>/installment")
@api_handler
def api_sip_installment(sip_id):
    data = request.get_json(force=True) or {}
    with get_conn() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM sip WHERE sip_id=%s FOR UPDATE", (sip_id,))
        sip = cur.fetchone()
        if not sip:
            raise ValueError("SIP does not exist.")
        if sip["status"] != "Active":
            raise ValueError("Installments can be recorded only for active SIPs.")
        nav_row = None
        if data.get("nav"):
            nav = parse_decimal(data.get("nav"), "NAV")
        else:
            nav_row = latest_nav(sip["scheme_id"], conn)
            if not nav_row:
                raise ValueError("No NAV found for this scheme. Add a NAV record first.")
            nav = Decimal(str(nav_row["nav_value"]))
        txn_date = parse_date(data.get("txn_date"), "Date") if data.get("txn_date") else date.today()
        amount = Decimal(str(sip["amount"]))
        units = calc_units(amount, nav)
        insert_transaction(
            conn,
            sip["investor_id"],
            sip["scheme_id"],
            sip_id,
            "SIP",
            txn_date,
            amount,
            nav,
            units,
        )
        upsert_holding(conn, sip["investor_id"], sip["scheme_id"], "SIP", amount, units)
        nxt = next_due_after(sip["next_due_date"] or txn_date, sip["frequency"])
        cur.execute("UPDATE sip SET next_due_date=%s WHERE sip_id=%s", (nxt, sip_id))
        cur.close()
    return jsonify({"ok": True, "message": "Transaction recorded. Portfolio updated."})


@app.get("/api/transactions")
@api_handler
def api_transactions():
    iid = current_investor_id()
    rows = query_all(
        """
        SELECT t.*, s.name AS scheme_name, a.name AS amc_name
        FROM transaction t
        JOIN mutual_fund_scheme s ON s.scheme_id = t.scheme_id
        JOIN amc a ON a.amc_id = s.amc_id
        WHERE t.investor_id=%s
        ORDER BY t.txn_date DESC, t.transaction_id DESC
        """,
        (iid,),
    )
    for r in rows:
        r["amount_fmt"] = format_inr(r["amount"])
        r["nav_fmt"] = format_inr(r["nav"])
        r["date_fmt"] = r["txn_date"].strftime("%d %b %Y")
        r["amount"] = float(r["amount"])
        r["nav"] = float(r["nav"])
        r["units"] = float(r["units"])
    return jsonify({"ok": True, "transactions": rows})


def record_txn(data, force_type=None):
    investor_id = int(data.get("investor_id") or current_investor_id())
    scheme_id = int(data.get("scheme_id") or 0)
    require_existing("investor", "investor_id", investor_id, "Investor")
    require_existing("mutual_fund_scheme", "scheme_id", scheme_id, "Scheme")
    txn_type = force_type or data.get("txn_type")
    if txn_type not in ("SIP", "Purchase", "Redemption"):
        raise ValueError("Transaction type is invalid.")
    txn_date = parse_date(data.get("txn_date"), "Date")
    amount = parse_decimal(data.get("amount"), "Amount")
    nav = parse_decimal(data.get("nav"), "NAV")
    units = calc_units(amount, nav)
    sip_id = data.get("sip_id") or None
    if sip_id in ("", None):
        sip_id = None
    else:
        sip_id = int(sip_id)
        require_existing("sip", "sip_id", sip_id, "SIP")

    with get_conn() as conn:
        if txn_type == "Redemption":
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """
                SELECT units FROM portfolio_holding
                WHERE investor_id=%s AND scheme_id=%s
                FOR UPDATE
                """,
                (investor_id, scheme_id),
            )
            holding = cur.fetchone()
            available = Decimal(str(holding["units"])) if holding else Decimal("0")
            if units > available:
                raise ValueError(
                    f"Insufficient units for redemption.\nAvailable: {available:.2f}\nRequested: {units:.2f}"
                )
            cur.close()
        insert_transaction(conn, investor_id, scheme_id, sip_id, txn_type, txn_date, amount, nav, units)
        upsert_holding(conn, investor_id, scheme_id, txn_type, amount, units)
        if txn_type == "SIP" and sip_id:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT * FROM sip WHERE sip_id=%s FOR UPDATE", (sip_id,))
            sip = cur.fetchone()
            if sip and sip["status"] == "Active":
                nxt = next_due_after(sip["next_due_date"] or txn_date, sip["frequency"])
                cur.execute("UPDATE sip SET next_due_date=%s WHERE sip_id=%s", (nxt, sip_id))
            cur.close()
    return units


@app.post("/transactions/add")
@app.post("/api/transactions")
@api_handler
def api_add_transaction():
    data = request.get_json(force=True)
    units = record_txn(data)
    return jsonify({"ok": True, "message": "Transaction recorded", "units": float(units)})


@app.post("/transactions/redeem")
@app.post("/api/transactions/redeem")
@api_handler
def api_redeem():
    data = request.get_json(force=True)
    units = record_txn(data, force_type="Redemption")
    return jsonify({"ok": True, "message": "Redemption processed. Portfolio updated.", "units": float(units)})


@app.get("/api/goals")
@api_handler
def api_goals():
    iid = current_investor_id()
    goals_rows = query_all(
        """
        SELECT g.*, i.name AS investor_name
        FROM goal g
        JOIN investor i ON i.investor_id = g.investor_id
        WHERE g.investor_id=%s
        ORDER BY g.target_date
        """,
        (iid,),
    )
    links = query_all(
        """
        SELECT gs.*, s.name AS scheme_name
        FROM goal_scheme gs
        JOIN mutual_fund_scheme s ON s.scheme_id = gs.scheme_id
        JOIN goal g ON g.goal_id = gs.goal_id
        WHERE g.investor_id=%s
        """,
        (iid,),
    )
    by_goal = {}
    for link in links:
        by_goal.setdefault(link["goal_id"], []).append(
            {
                "scheme_id": link["scheme_id"],
                "scheme_name": link["scheme_name"],
                "allocation_pct": float(link["allocation_pct"]),
            }
        )
    out = []
    for g in goals_rows:
        pct = progress_pct(g["current_amount"], g["target_amount"])
        out.append(
            {
                **g,
                "target_amount": float(g["target_amount"]),
                "current_amount": float(g["current_amount"]),
                "target_fmt": format_inr(g["target_amount"]),
                "current_fmt": format_inr(g["current_amount"]),
                "date_fmt": g["target_date"].strftime("%d %b %Y"),
                "progress": pct,
                "schemes": by_goal.get(g["goal_id"], []),
            }
        )
    return jsonify({"ok": True, "goals": out})


def save_goal_links(conn, goal_id, schemes):
    cur = conn.cursor()
    cur.execute("DELETE FROM goal_scheme WHERE goal_id=%s", (goal_id,))
    total = Decimal("0")
    for item in schemes or []:
        sid = int(item["scheme_id"])
        pct = parse_decimal(item.get("allocation_pct"), "Allocation")
        require_existing("mutual_fund_scheme", "scheme_id", sid, "Scheme")
        cur.execute(
            "INSERT INTO goal_scheme (goal_id, scheme_id, allocation_pct) VALUES (%s, %s, %s)",
            (goal_id, sid, pct),
        )
        total += pct
    cur.close()
    if schemes and (total < Decimal("99.99") or total > Decimal("100.01")):
        raise ValueError("Linked scheme allocations must add up to 100%.")


@app.post("/goals/add")
@app.post("/api/goals")
@api_handler
def api_add_goal():
    data = request.get_json(force=True)
    investor_id = int(data.get("investor_id") or current_investor_id())
    require_existing("investor", "investor_id", investor_id, "Investor")
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Goal name is required.")
    target = parse_decimal(data.get("target_amount"), "Target amount")
    current = parse_decimal(data.get("current_amount") or 0, "Current amount", positive=False)
    target_date = parse_date(data.get("target_date"), "Target date")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO goal (investor_id, name, target_amount, current_amount, target_date)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (investor_id, name, target, current, target_date),
        )
        goal_id = cur.lastrowid
        cur.close()
        save_goal_links(conn, goal_id, data.get("schemes"))
    return jsonify({"ok": True, "message": "Goal created successfully", "goal_id": goal_id})


@app.post("/api/goals/<int:goal_id>")
@api_handler
def api_edit_goal(goal_id):
    require_existing("goal", "goal_id", goal_id, "Goal")
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Goal name is required.")
    target = parse_decimal(data.get("target_amount"), "Target amount")
    current = parse_decimal(data.get("current_amount") or 0, "Current amount", positive=False)
    target_date = parse_date(data.get("target_date"), "Target date")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE goal
            SET name=%s, target_amount=%s, current_amount=%s, target_date=%s
            WHERE goal_id=%s
            """,
            (name, target, current, target_date, goal_id),
        )
        cur.close()
        save_goal_links(conn, goal_id, data.get("schemes"))
    return jsonify({"ok": True, "message": "Goal updated successfully"})


@app.post("/api/goals/<int:goal_id>/delete")
@api_handler
def api_delete_goal(goal_id):
    require_existing("goal", "goal_id", goal_id, "Goal")
    execute("DELETE FROM goal WHERE goal_id=%s", (goal_id,))
    return jsonify({"ok": True, "message": "Goal deleted successfully"})


@app.get("/api/schemes")
@api_handler
def api_schemes():
    rows = query_all(
        """
        SELECT s.*, a.name AS amc_name,
               (SELECT n.nav_value FROM nav_history n
                WHERE n.scheme_id = s.scheme_id
                ORDER BY n.nav_date DESC, n.nav_id DESC LIMIT 1) AS latest_nav
        FROM mutual_fund_scheme s
        JOIN amc a ON a.amc_id = s.amc_id
        ORDER BY s.name
        """
    )
    for r in rows:
        r["min_investment"] = float(r["min_investment"])
        r["latest_nav"] = float(r["latest_nav"]) if r["latest_nav"] is not None else None
        r["latest_nav_fmt"] = format_inr(r["latest_nav"]) if r["latest_nav"] is not None else "—"
        r["min_fmt"] = format_inr(r["min_investment"])
    return jsonify({"ok": True, "schemes": rows})


def scheme_payload(data):
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Scheme name is required.")
    amc_id = int(data.get("amc_id") or 0)
    require_existing("amc", "amc_id", amc_id, "AMC")
    category = data.get("category")
    if category not in ("Equity", "Debt", "Hybrid"):
        raise ValueError("Category is invalid.")
    risk = data.get("risk_level")
    if risk not in ("Low", "Moderate", "High", "Very High"):
        raise ValueError("Risk level is invalid.")
    minimum = parse_decimal(data.get("min_investment"), "Minimum investment")
    status = data.get("status") or "Active"
    if status not in ("Active", "Inactive"):
        raise ValueError("Status is invalid.")
    return name, amc_id, category, risk, minimum, status


@app.post("/schemes/add")
@app.post("/api/schemes")
@api_handler
def api_add_scheme():
    data = request.get_json(force=True)
    name, amc_id, category, risk, minimum, status = scheme_payload(data)
    sid = execute(
        """
        INSERT INTO mutual_fund_scheme (amc_id, name, category, risk_level, min_investment, status)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (amc_id, name, category, risk, minimum, status),
    )
    return jsonify({"ok": True, "message": "Scheme added successfully", "scheme_id": sid})


@app.post("/schemes/edit")
@app.post("/api/schemes/<int:scheme_id>")
@api_handler
def api_edit_scheme(scheme_id=None):
    data = request.get_json(force=True)
    scheme_id = scheme_id or int(data.get("scheme_id") or 0)
    require_existing("mutual_fund_scheme", "scheme_id", scheme_id, "Scheme")
    name, amc_id, category, risk, minimum, status = scheme_payload(data)
    execute(
        """
        UPDATE mutual_fund_scheme
        SET amc_id=%s, name=%s, category=%s, risk_level=%s, min_investment=%s, status=%s
        WHERE scheme_id=%s
        """,
        (amc_id, name, category, risk, minimum, status, scheme_id),
    )
    return jsonify({"ok": True, "message": "Scheme updated successfully"})


@app.post("/api/schemes/<int:scheme_id>/deactivate")
@api_handler
def api_deactivate_scheme(scheme_id):
    require_existing("mutual_fund_scheme", "scheme_id", scheme_id, "Scheme")
    used = query_one(
        """
        SELECT
          (SELECT COUNT(*) FROM sip WHERE scheme_id=%s) +
          (SELECT COUNT(*) FROM transaction WHERE scheme_id=%s) +
          (SELECT COUNT(*) FROM portfolio_holding WHERE scheme_id=%s) AS used
        """,
        (scheme_id, scheme_id, scheme_id),
    )
    if used and used["used"] > 0:
        execute("UPDATE mutual_fund_scheme SET status='Inactive' WHERE scheme_id=%s", (scheme_id,))
        return jsonify({"ok": True, "message": "Scheme deactivated because it is in use."})
    execute("DELETE FROM nav_history WHERE scheme_id=%s", (scheme_id,))
    execute("DELETE FROM goal_scheme WHERE scheme_id=%s", (scheme_id,))
    execute("DELETE FROM mutual_fund_scheme WHERE scheme_id=%s", (scheme_id,))
    return jsonify({"ok": True, "message": "Scheme deleted successfully"})


@app.get("/api/amcs")
@api_handler
def api_amcs():
    rows = query_all(
        """
        SELECT a.*,
               (SELECT COUNT(*) FROM mutual_fund_scheme s WHERE s.amc_id = a.amc_id) AS scheme_count
        FROM amc a
        ORDER BY a.name
        """
    )
    return jsonify({"ok": True, "amcs": rows})


@app.post("/api/amcs")
@api_handler
def api_add_amc():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    headquarters = (data.get("headquarters") or "").strip()
    email = (data.get("contact_email") or "").strip()
    if not name:
        raise ValueError("AMC name is required.")
    if not headquarters:
        raise ValueError("Headquarters is required.")
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a valid contact email.")
    amc_id = execute(
        "INSERT INTO amc (name, headquarters, contact_email) VALUES (%s, %s, %s)",
        (name, headquarters, email),
    )
    return jsonify({"ok": True, "message": "AMC added successfully", "amc_id": amc_id})


@app.get("/api/nav/<int:scheme_id>")
@api_handler
def api_nav(scheme_id):
    require_existing("mutual_fund_scheme", "scheme_id", scheme_id, "Scheme")
    rows = query_all(
        """
        SELECT n.*, s.name AS scheme_name
        FROM nav_history n
        JOIN mutual_fund_scheme s ON s.scheme_id = n.scheme_id
        WHERE n.scheme_id=%s
        ORDER BY n.nav_date
        """,
        (scheme_id,),
    )
    for r in rows:
        r["nav_value"] = float(r["nav_value"])
        r["nav_fmt"] = format_inr(r["nav_value"])
        r["date_fmt"] = r["nav_date"].strftime("%d %b %Y")
    return jsonify({"ok": True, "history": rows})


@app.get("/api/nav")
@api_handler
def api_nav_all():
    rows = query_all(
        """
        SELECT n.*, s.name AS scheme_name, a.name AS amc_name
        FROM nav_history n
        JOIN mutual_fund_scheme s ON s.scheme_id = n.scheme_id
        JOIN amc a ON a.amc_id = s.amc_id
        ORDER BY n.nav_date DESC, n.nav_id DESC
        LIMIT 200
        """
    )
    for r in rows:
        r["nav_value"] = float(r["nav_value"])
        r["nav_fmt"] = format_inr(r["nav_value"])
        r["date_fmt"] = r["nav_date"].strftime("%d %b %Y")
    return jsonify({"ok": True, "history": rows})


@app.post("/nav/add")
@app.post("/api/nav")
@api_handler
def api_add_nav():
    data = request.get_json(force=True)
    scheme_id = int(data.get("scheme_id") or 0)
    require_existing("mutual_fund_scheme", "scheme_id", scheme_id, "Scheme")
    nav_date = parse_date(data.get("nav_date"), "NAV date")
    nav = parse_decimal(data.get("nav_value"), "NAV")
    existing = query_one(
        "SELECT nav_id FROM nav_history WHERE scheme_id=%s AND nav_date=%s",
        (scheme_id, nav_date),
    )
    if existing:
        execute(
            "UPDATE nav_history SET nav_value=%s WHERE nav_id=%s",
            (nav, existing["nav_id"]),
        )
        return jsonify({"ok": True, "message": "NAV updated successfully"})
    execute(
        "INSERT INTO nav_history (scheme_id, nav_date, nav_value) VALUES (%s, %s, %s)",
        (scheme_id, nav_date, nav),
    )
    return jsonify({"ok": True, "message": "NAV added successfully"})


@app.get("/api/investor")
@api_handler
def api_investor():
    inv = get_investor()
    if not inv:
        raise ValueError("Investor not found.")
    inv["dob_fmt"] = inv["date_of_birth"].strftime("%d %b %Y")
    return jsonify({"ok": True, "investor": inv})


@app.post("/investor/edit")
@app.post("/api/investor")
@api_handler
def api_edit_investor():
    inv = get_investor()
    if not inv:
        raise ValueError("Investor not found.")
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    pan = (data.get("pan") or "").strip().upper()
    address = (data.get("address") or "").strip()
    kyc = data.get("kyc_status") or inv["kyc_status"]
    dob = parse_date(data.get("date_of_birth"), "Date of birth")
    if not name:
        raise ValueError("Name is required.")
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    if not PHONE_RE.match(phone):
        raise ValueError("Enter a valid 10-digit Indian mobile number.")
    if not PAN_RE.match(pan):
        raise ValueError("Enter a valid PAN (for example ABCDE1234F).")
    if kyc not in ("Pending", "Verified", "Rejected"):
        raise ValueError("KYC status is invalid.")
    if not address:
        raise ValueError("Address is required.")
    execute(
        """
        UPDATE investor
        SET name=%s, email=%s, phone=%s, pan=%s, date_of_birth=%s, address=%s, kyc_status=%s
        WHERE investor_id=%s
        """,
        (name, email, phone, pan, dob, address, kyc, inv["investor_id"]),
    )
    return jsonify({"ok": True, "message": "Investor profile updated"})


@app.route("/sips/add", methods=["GET"])
def sips_add_page():
    return redirect(url_for("sips"))


@app.route("/goals/add", methods=["GET"])
def goals_add_page():
    return redirect(url_for("goals"))


@app.route("/schemes/add", methods=["GET"])
def schemes_add_page():
    return redirect(url_for("schemes"))


@app.route("/nav/add", methods=["GET"])
def nav_add_page():
    return redirect(url_for("nav_page"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
