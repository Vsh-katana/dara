"""SQLite persistence, schema creation, and workbook history seeding."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance.db"
_DB_OVERRIDE: ContextVar[Path | None] = ContextVar("dara_database_override", default=None)

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'cash', currency TEXT NOT NULL DEFAULT '',
 opening_balance TEXT NOT NULL DEFAULT '0', archived INTEGER NOT NULL DEFAULT 0,
 note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS categories(
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('income','expense')), name TEXT NOT NULL,
 parent_id INTEGER, color TEXT NOT NULL DEFAULT '#f59e0b', icon TEXT NOT NULL DEFAULT 'circle',
 enabled INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(parent_id) REFERENCES categories(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_categories_kind ON categories(kind, enabled, sort_order);
CREATE TABLE IF NOT EXISTS transactions(
 id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
 amount TEXT NOT NULL, raw_amount TEXT NOT NULL, date TEXT NOT NULL, time TEXT, title TEXT NOT NULL DEFAULT '',
 category_id INTEGER, category_text TEXT NOT NULL DEFAULT '', subcategory TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
 account_id INTEGER, tags_json TEXT NOT NULL DEFAULT '[]', inclusion_json TEXT NOT NULL DEFAULT '{}',
 attachment_name TEXT, attachment_path TEXT, source_sheet TEXT, source_row TEXT, import_row INTEGER,
 locked INTEGER NOT NULL DEFAULT 0, deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_kind_date ON transactions(kind, date);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id, kind);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id, date);
CREATE TABLE IF NOT EXISTS transfers(
 id INTEGER PRIMARY KEY, amount TEXT NOT NULL, date TEXT NOT NULL, time TEXT, from_account_id INTEGER NOT NULL,
 to_account_id INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', deleted_at TEXT, created_at TEXT NOT NULL,
 FOREIGN KEY(from_account_id) REFERENCES accounts(id), FOREIGN KEY(to_account_id) REFERENCES accounts(id)
);
CREATE TABLE IF NOT EXISTS budgets(
 id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', category_id INTEGER, category_name TEXT NOT NULL DEFAULT '',
 amount TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, threshold_json TEXT NOT NULL DEFAULT '[70,90,100]',
 enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_budgets_dates ON budgets(start_date, end_date, enabled);
CREATE TABLE IF NOT EXISTS recurring(
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
 amount TEXT NOT NULL, category_id INTEGER, category_name TEXT NOT NULL DEFAULT '', account_id INTEGER,
 cadence TEXT NOT NULL, interval_n INTEGER NOT NULL DEFAULT 1, start_date TEXT NOT NULL, next_date TEXT NOT NULL,
 end_date TEXT, time TEXT, note TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
 last_generated_date TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recurring_runs(
 id INTEGER PRIMARY KEY, recurring_id INTEGER NOT NULL, due_date TEXT NOT NULL, transaction_id INTEGER NOT NULL,
 UNIQUE(recurring_id, due_date), FOREIGN KEY(recurring_id) REFERENCES recurring(id),
 FOREIGN KEY(transaction_id) REFERENCES transactions(id)
);
CREATE TABLE IF NOT EXISTS debts(
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('payable','receivable')), party TEXT NOT NULL,
 title TEXT NOT NULL DEFAULT '', original_amount TEXT NOT NULL, due_date TEXT, notes TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS debt_payments(
 id INTEGER PRIMARY KEY, debt_id INTEGER NOT NULL, amount TEXT NOT NULL, date TEXT NOT NULL,
 account_id INTEGER, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 FOREIGN KEY(debt_id) REFERENCES debts(id), FOREIGN KEY(account_id) REFERENCES accounts(id)
);
CREATE TABLE IF NOT EXISTS goals(
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, target_amount TEXT NOT NULL, target_date TEXT,
 color TEXT NOT NULL DEFAULT '#f59e0b', status TEXT NOT NULL DEFAULT 'active', notes TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_movements(
 id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL, amount TEXT NOT NULL, date TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('contribution','withdrawal')), account_id INTEGER,
 note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 FOREIGN KEY(goal_id) REFERENCES goals(id), FOREIGN KEY(account_id) REFERENCES accounts(id)
);
CREATE TABLE IF NOT EXISTS saved_filters(
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, filters_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications(
 id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, title TEXT NOT NULL,
 message TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT
);
CREATE TABLE IF NOT EXISTS notification_settings(
 key TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, threshold INTEGER
);
CREATE TABLE IF NOT EXISTS audit_log(
 id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, action TEXT NOT NULL,
 before_json TEXT, after_json TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_path() -> Path:
    override = _DB_OVERRIDE.get()
    return Path(override if override is not None else os.environ.get("FINANCE_DB", str(DEFAULT_DB))).resolve()


def data_dir() -> Path:
    override = _DB_OVERRIDE.get()
    if override is not None:
        return Path(override).resolve().parent
    return ROOT / "data"


@contextmanager
def database_scope(path: str | Path):
    """Route one request to its isolated per-user SQLite database."""
    token = _DB_OVERRIDE.set(Path(path).resolve())
    try:
        yield
    finally:
        _DB_OVERRIDE.reset(token)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path or db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _excel_time(raw) -> str | None:
    if raw in (None, ""):
        return None
    seconds = int((Decimal(str(raw)) * Decimal(86400)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) % 86400
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _audit(conn, entity_type, entity_id, action, before=None, after=None):
    conn.execute(
        "INSERT INTO audit_log(entity_type,entity_id,action,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
        (entity_type, str(entity_id), action,
         json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
         json.dumps(after, ensure_ascii=False, default=str) if after is not None else None, now_iso()),
    )


def init_database(path: str | Path | None = None, *, seed: bool | None = None) -> None:
    if seed is None:
        seed = os.environ.get("FINANCE_SEED_DEFAULT", "1").strip().lower() not in {"0", "false", "no"}
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        stamp = now_iso()
        with transaction(conn):
            row = conn.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO accounts(name,kind,currency,opening_balance,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    ("حساب پیش‌فرض", "cash", "", "0", "حساب آغازین برای ثبت تراکنش‌ها؛ موجودی آغازین را در تنظیمات حساب وارد کنید.", stamp, stamp),
                )
                default_account = cur.lastrowid
            else:
                default_account = row["id"]
            for key, value in (("language", "fa"), ("calendar", "jalali"), ("currency_unit", ""), ("theme", "dark"), ("accent", "teal")):
                conn.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)", (key, value, stamp))
            for key, threshold in (("budget_alerts", 70), ("debt_alerts", None), ("recurring_alerts", None), ("unusual_spending", None), ("income_change", None)):
                conn.execute("INSERT OR IGNORE INTO notification_settings(key,enabled,threshold) VALUES(?,?,?)", (key, 1, threshold))
            seeded = conn.execute("SELECT value FROM app_meta WHERE key='legacy_seeded'").fetchone()
            if seed and not seeded:
                seed_file = ROOT / "data" / "seed.json"
                rows = json.loads(seed_file.read_text(encoding="utf-8")) if seed_file.exists() else []
                for record in rows:
                    kind, name = record["type"], record.get("category", "")
                    category = conn.execute("SELECT id FROM categories WHERE kind=? AND name=? LIMIT 1", (kind, name)).fetchone()
                    if category is None and name:
                        cat_id = conn.execute(
                            "INSERT INTO categories(kind,name,color,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                            (kind, name, "#f59e0b" if kind == "expense" else "#25c6a2", len(rows), stamp, stamp),
                        ).lastrowid
                    else:
                        cat_id = category["id"] if category else None
                    conn.execute(
                        """INSERT OR IGNORE INTO transactions
                        (uid,kind,amount,raw_amount,date,time,title,category_id,category_text,note,account_id,source_sheet,source_row,import_row,locked,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (record["id"], kind, str(record["amountRaw"]), str(record["amountRaw"]), record["date"],
                         _excel_time(record.get("timeExcel")), name, cat_id, name, record.get("note", ""),
                         default_account, record.get("sourceSheet"), record.get("sourceRow"), record.get("importRow"),
                         0, stamp, stamp),
                    )
                conn.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('legacy_seeded','1')")
                _audit(conn, "workbook", "source", "imported", after={"records": len(rows), "source": "data/seed.json"})
            unlocked = conn.execute("SELECT value FROM app_meta WHERE key='historical_editing_enabled'").fetchone()
            if not unlocked:
                cur = conn.execute("UPDATE transactions SET locked=0 WHERE locked<>0")
                conn.execute("INSERT INTO app_meta(key,value) VALUES('historical_editing_enabled','1')")
                if cur.rowcount:
                    _audit(conn, "workbook", "source", "editing_enabled", after={"records": cur.rowcount})
            conn.execute("INSERT OR IGNORE INTO app_meta(key,value) VALUES('schema_version','1')")
    finally:
        conn.close()


def audit(conn, entity_type, entity_id, action, before=None, after=None):
    _audit(conn, entity_type, entity_id, action, before, after)
