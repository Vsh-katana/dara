"""Password authentication and isolated tenant database routing for the server build."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from .database import database_scope, init_database

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
SESSION_COOKIE = "dara_session"
SESSION_DAYS = 14
PBKDF2_ROUNDS = 600_000
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE,
 password_hash TEXT NOT NULL, password_salt TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
 FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS rate_limits(
 rate_key TEXT PRIMARY KEY, window_started INTEGER NOT NULL, attempts INTEGER NOT NULL
);
"""


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def data_root() -> Path:
    return Path(os.environ.get("DARA_DATA_DIR", "/var/lib/dara")).resolve()


def auth_db_path() -> Path:
    return data_root() / "auth.sqlite3"


def _session_secret() -> str:
    secret_file = os.environ.get("DARA_SESSION_SECRET_FILE")
    if secret_file:
        try:
            return Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.environ.get("DARA_SESSION_SECRET", "")


def tenant_db_path(user_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(user_id))
    except (ValueError, TypeError, AttributeError):
        raise AuthError(401, "Authentication required.") from None
    return data_root() / "tenants" / normalized / "finance.sqlite3"


def _connect() -> sqlite3.Connection:
    path = auth_db_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def initialize_auth() -> None:
    secret = _session_secret()
    if len(secret.encode("utf-8")) < 32:
        raise RuntimeError("DARA_SESSION_SECRET must contain at least 32 bytes.")
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        with conn:
            now = int(time.time())
            conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            conn.execute("DELETE FROM rate_limits WHERE window_started<?", (now-86400,))
    finally:
        conn.close()
    try:
        os.chmod(auth_db_path(), 0o600)
    except OSError:
        pass


def allow_signup() -> bool:
    return os.environ.get("ALLOW_SIGNUP", "1").strip().lower() not in {"0", "false", "no"}


def max_accounts() -> int:
    try:
        return max(0, int(os.environ.get("MAX_ACCOUNTS", "500")))
    except ValueError:
        return 500


def cookie_secure() -> bool:
    return os.environ.get("COOKIE_SECURE", "1").strip().lower() not in {"0", "false", "no"}


def csrf_value(session_token: str) -> str:
    secret = _session_secret()
    return hmac.new(secret.encode("utf-8"), session_token.encode("ascii"), hashlib.sha256).hexdigest()


def _rate_key(scope: str, client_ip: str) -> str:
    return hashlib.sha256(f"{scope}\0{client_ip}".encode("utf-8")).hexdigest()


def _consume_rate(conn: sqlite3.Connection, key: str, maximum: int, window: int) -> bool:
    now = int(time.time())
    if now % 3600 == 0:
        conn.execute("DELETE FROM rate_limits WHERE window_started<?", (now-86400,))
    row = conn.execute("SELECT window_started,attempts FROM rate_limits WHERE rate_key=?", (key,)).fetchone()
    if row is None or now - row["window_started"] >= window:
        conn.execute("INSERT INTO rate_limits(rate_key,window_started,attempts) VALUES(?,?,1) "
                     "ON CONFLICT(rate_key) DO UPDATE SET window_started=excluded.window_started,attempts=1", (key, now))
        return False
    if row["attempts"] >= maximum:
        return True
    conn.execute("UPDATE rate_limits SET attempts=attempts+1 WHERE rate_key=?", (key,))
    return False


def _password_hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()


def _new_session(conn: sqlite3.Connection, user_id: str) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    expires = now + SESSION_DAYS * 86400
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    conn.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                 (token_hash, user_id, expires, now))
    return token, expires


def _user_result(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "username": row["username"]}


def register(username: str, password: str, client_ip: str) -> tuple[dict, str, int]:
    if not allow_signup():
        raise AuthError(403, "Account registration is currently closed.")
    username = str(username or "").strip()
    key = username.lower()
    password = str(password or "")
    if not USERNAME_RE.fullmatch(key):
        raise AuthError(422, "Username must be 3–32 characters and use letters, numbers, dot, dash, or underscore.")
    if len(password) < 12 or len(password) > 1024:
        raise AuthError(422, "Password must be at least 12 characters.")
    conn = _connect()
    try:
        with conn:
            limited = _consume_rate(conn, _rate_key("register", client_ip), 5, 3600)
            if limited:
                pass
            else:
                cap = max_accounts()
                count = conn.execute("SELECT count(*) FROM users").fetchone()[0]
                if cap and count >= cap:
                    raise AuthError(403, "This server has reached its account limit.")
                user_id = str(uuid.uuid4())
                salt = secrets.token_bytes(16)
                password_digest = _password_hash(password, salt)
                conn.execute("INSERT INTO users(id,username,username_key,password_hash,password_salt,created_at) "
                             "VALUES(?,?,?,?,?,?)", (user_id, username, key, password_digest, salt.hex(), int(time.time())))
                token, expires = _new_session(conn, user_id)
                user = {"id": user_id, "username": username}
        if limited:
            raise AuthError(429, "Too many registration attempts. Try again later.")
    except sqlite3.IntegrityError:
        raise AuthError(409, "That username is already in use.") from None
    finally:
        conn.close()
    tenant_file = tenant_db_path(user_id)
    tenant_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with database_scope(tenant_file):
        init_database(seed=False)
    try:
        os.chmod(tenant_file, 0o600)
    except OSError:
        pass
    return user, token, expires


def login(username: str, password: str, client_ip: str) -> tuple[dict, str, int]:
    key = str(username or "").strip().lower()
    password = str(password or "")
    if len(password) > 1024:
        raise AuthError(422, "Password is too long.")
    conn = _connect()
    try:
        with conn:
            limited = _consume_rate(conn, _rate_key("login", client_ip), 10, 900)
            row = None if limited else conn.execute("SELECT * FROM users WHERE username_key=?", (key,)).fetchone()
            if row is None:
                # Spend comparable time for unknown usernames to reduce account enumeration.
                _password_hash(password, bytes(16))
                valid = False
            else:
                valid = hmac.compare_digest(_password_hash(password, bytes.fromhex(row["password_salt"])), row["password_hash"])
            if not limited and valid:
                token, expires = _new_session(conn, row["id"])
                user = _user_result(row)
        if limited:
            raise AuthError(429, "Too many sign-in attempts. Try again later.")
        if not valid:
            raise AuthError(401, "Username or password is incorrect.")
        return user, token, expires
    finally:
        conn.close()


def resolve_session(token: str | None) -> dict | None:
    if not token or len(token) > 256:
        return None
    token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    now = int(time.time())
    conn = _connect()
    try:
        with conn:
            row = conn.execute("SELECT u.id,u.username,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id "
                               "WHERE s.token_hash=?", (token_hash,)).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
                return None
            return {"user": {"id": row["id"], "username": row["username"]},
                    "tenant_db": tenant_db_path(row["id"]), "csrf_token": csrf_value(token)}
    finally:
        conn.close()


def verify_csrf(session: dict, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(session["csrf_token"], supplied)


def change_password(user_id: str, session_token: str, current_password: str, new_password: str, client_ip: str) -> None:
    new_password = str(new_password or "")
    if len(new_password) < 12 or len(new_password) > 1024:
        raise AuthError(422, "New password must be at least 12 characters.")
    current_password = str(current_password or "")
    if len(current_password) > 1024:
        raise AuthError(422, "Current password is too long.")
    conn = _connect()
    try:
        with conn:
            limited = _consume_rate(conn, _rate_key(f"password:{user_id}", client_ip), 5, 3600)
            row = None if limited else conn.execute("SELECT password_hash,password_salt FROM users WHERE id=?", (user_id,)).fetchone()
            valid = bool(row) and hmac.compare_digest(
                _password_hash(str(current_password or ""), bytes.fromhex(row["password_salt"])), row["password_hash"]
            )
            if valid and not limited:
                salt = secrets.token_bytes(16)
                token_hash = hashlib.sha256(session_token.encode("ascii")).hexdigest()
                conn.execute("UPDATE users SET password_hash=?,password_salt=? WHERE id=?",
                             (_password_hash(new_password, salt), salt.hex(), user_id))
                conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user_id, token_hash))
        if limited:
            raise AuthError(429, "Too many password change attempts. Try again later.")
        if not valid:
            raise AuthError(401, "Current password is incorrect.")
    finally:
        conn.close()


def logout(token: str | None) -> None:
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
    finally:
        conn.close()


@contextmanager
def tenant_scope(session: dict):
    path = session["tenant_db"]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with database_scope(path):
        yield
