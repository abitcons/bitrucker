from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import struct
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from binance_bot.database import DatabaseConfig, connect_sqlite

from werkzeug.security import check_password_hash, generate_password_hash


TRIAL_DAYS = 3
MONTHLY_PRICE_USDT = 10
LEGAL_VERSION = "2026-07-11"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class SaaSStore:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.runtime_dir = root_dir / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.database_config = DatabaseConfig.from_root(root_dir)
        self.db_path = self.database_config.sqlite_path
        self.session_secret_path = self.runtime_dir / "saas_session_secret.txt"
        self.credential_key_path = self.runtime_dir / "saas_credentials.key"
        self._init_db()

    @property
    def session_secret(self) -> str:
        configured = os.environ.get("SAAS_SESSION_SECRET", "").strip()
        if configured:
            return configured
        if not self.session_secret_path.exists():
            self.session_secret_path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        return self.session_secret_path.read_text(encoding="utf-8").strip()

    @property
    def payment_address(self) -> str:
        return os.environ.get("SAAS_USDT_PAYMENT_ADDRESS", "").strip() or self.get_setting("payment_address")

    @property
    def payment_network(self) -> str:
        return os.environ.get("SAAS_USDT_PAYMENT_NETWORK", "").strip() or self.get_setting("payment_network") or "TRC20"

    @contextmanager
    def connect(self):
        conn = connect_sqlite(self.database_config)
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    status TEXT NOT NULL DEFAULT 'active',
                    is_owner INTEGER NOT NULL DEFAULT 0,
                    trial_started_at TEXT,
                    trial_ends_at TEXT,
                    subscription_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS binance_credentials (
                    user_id INTEGER PRIMARY KEY,
                    api_key_encrypted TEXT NOT NULL,
                    api_secret_encrypted TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT 'Binance Spot',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS payment_invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    reference TEXT NOT NULL UNIQUE,
                    amount_usdt TEXT NOT NULL,
                    network TEXT NOT NULL,
                    deposit_address TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    tx_hash TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    paid_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS exchange_credentials (
                    user_id INTEGER NOT NULL,
                    exchange TEXT NOT NULL,
                    api_key_encrypted TEXT NOT NULL,
                    api_secret_encrypted TEXT NOT NULL,
                    passphrase_encrypted TEXT,
                    label TEXT NOT NULL,
                    validation_status TEXT NOT NULL DEFAULT 'unverified',
                    validation_json TEXT NOT NULL DEFAULT '{}',
                    last_validated_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, exchange),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS auth_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    login TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    success INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_user_id INTEGER,
                    event_type TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    ip_address TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT '',
                    request_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS mfa_recovery_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    code_hash TEXT NOT NULL,
                    used_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_auth_attempts_lookup
                    ON auth_attempts(login, ip_address, created_at);
                CREATE INDEX IF NOT EXISTS idx_audit_events_created
                    ON audit_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_audit_events_actor
                    ON audit_events(actor_user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_mfa_recovery_user
                    ON mfa_recovery_codes(user_id, used_at);
                """
            )
            user_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            for column_name, declaration in (
                ("accepted_terms_at", "TEXT"),
                ("accepted_terms_version", "TEXT"),
                ("risk_acknowledged_at", "TEXT"),
                ("last_login_at", "TEXT"),
                ("mfa_secret_encrypted", "TEXT"),
                ("mfa_enabled_at", "TEXT"),
            ):
                if column_name not in user_columns:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {column_name} {declaration}")

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def get_setting(self, key: str) -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else ""

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value.strip(), iso_now()),
            )

    def set_payment_settings(self, address: str, network: str) -> None:
        address = address.strip()
        network = network.strip().upper() or "TRC20"
        if not address:
            raise ValueError("USDT payment address is required")
        if network not in {"TRC20", "ERC20", "BEP20", "POLYGON", "SOLANA"}:
            raise ValueError("Unsupported USDT network")
        self.set_setting("payment_address", address)
        self.set_setting("payment_network", network)

    def get_user(self, user_id: int | None) -> dict[str, Any] | None:
        if not user_id:
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_dict(row)

    def get_user_by_login(self, login: str) -> dict[str, Any] | None:
        normalized = login.strip().lower()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE lower(email) = ?", (normalized,)).fetchone()
        return self._row_to_dict(row)

    def has_admin(self) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        return row is not None

    def authenticate(self, login: str, password: str) -> dict[str, Any] | None:
        user = self.get_user_by_login(login)
        if not user or user.get("status") != "active":
            return None
        if not check_password_hash(user["password_hash"], password):
            return None
        with self.connect() as conn:
            conn.execute("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?", (iso_now(), iso_now(), user["id"]))
        return user

    def verify_password(self, user_id: int, password: str) -> bool:
        user = self.get_user(user_id)
        return bool(user and user.get("status") == "active" and check_password_hash(user["password_hash"], password))

    @staticmethod
    def _totp_code(secret: str, timestamp: float | None = None, period_seconds: int = 30) -> str:
        normalized = secret.strip().replace(" ", "").upper()
        padding = "=" * ((8 - len(normalized) % 8) % 8)
        key = base64.b32decode(normalized + padding, casefold=True)
        counter = int((timestamp if timestamp is not None else time.time()) // period_seconds)
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        return f"{value % 1_000_000:06d}"

    @classmethod
    def verify_totp(cls, secret: str, code: str, timestamp: float | None = None, window: int = 1) -> bool:
        normalized_code = "".join(character for character in code if character.isdigit())
        if len(normalized_code) != 6:
            return False
        now = timestamp if timestamp is not None else time.time()
        for offset in range(-max(0, window), max(0, window) + 1):
            if hmac.compare_digest(cls._totp_code(secret, now + offset * 30), normalized_code):
                return True
        return False

    def begin_mfa_setup(self, user_id: int) -> dict[str, str]:
        user = self.get_user(user_id)
        if not user:
            raise ValueError("User not found")
        if user.get("mfa_enabled_at"):
            raise ValueError("MFA is already enabled")
        secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET mfa_secret_encrypted = ?, updated_at = ? WHERE id = ?",
                (self.encrypt_text(secret), iso_now(), user_id),
            )
        issuer = os.environ.get("SAAS_MFA_ISSUER", "BitRucker").strip() or "BitRucker"
        account = str(user.get("email") or user_id)
        label = urllib.parse.quote(f"{issuer}:{account}")
        query = urllib.parse.urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": 6, "period": 30})
        return {"secret": secret, "otpauth_uri": f"otpauth://totp/{label}?{query}"}

    def get_pending_mfa_setup(self, user_id: int) -> dict[str, str] | None:
        user = self.get_user(user_id)
        encrypted = str(user.get("mfa_secret_encrypted") or "") if user else ""
        if not encrypted or user.get("mfa_enabled_at"):
            return None
        secret = self.decrypt_text(encrypted)
        issuer = os.environ.get("SAAS_MFA_ISSUER", "BitRucker").strip() or "BitRucker"
        label = urllib.parse.quote(f"{issuer}:{user.get('email') or user_id}")
        query = urllib.parse.urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": 6, "period": 30})
        return {"secret": secret, "otpauth_uri": f"otpauth://totp/{label}?{query}"}

    def enable_mfa(self, user_id: int, code: str) -> list[str]:
        setup = self.get_pending_mfa_setup(user_id)
        if not setup:
            raise ValueError("Start MFA setup first")
        if not self.verify_totp(setup["secret"], code):
            raise ValueError("Invalid authenticator code")
        recovery_codes = [f"{secrets.token_hex(3).upper()}-{secrets.token_hex(3).upper()}" for _ in range(10)]
        now = iso_now()
        with self.connect() as conn:
            conn.execute("UPDATE users SET mfa_enabled_at = ?, updated_at = ? WHERE id = ?", (now, now, user_id))
            conn.execute("DELETE FROM mfa_recovery_codes WHERE user_id = ?", (user_id,))
            conn.executemany(
                "INSERT INTO mfa_recovery_codes (user_id, code_hash, created_at) VALUES (?, ?, ?)",
                [(user_id, generate_password_hash(recovery_code), now) for recovery_code in recovery_codes],
            )
        return recovery_codes

    def verify_user_mfa(self, user_id: int, code: str) -> tuple[bool, str]:
        user = self.get_user(user_id)
        if not user or not user.get("mfa_enabled_at") or not user.get("mfa_secret_encrypted"):
            return False, "not_enabled"
        try:
            secret = self.decrypt_text(str(user["mfa_secret_encrypted"]))
        except Exception:
            return False, "secret_error"
        if self.verify_totp(secret, code):
            return True, "totp"
        normalized_code = code.strip().upper()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, code_hash FROM mfa_recovery_codes WHERE user_id = ? AND used_at IS NULL ORDER BY id",
                (user_id,),
            ).fetchall()
            for row in rows:
                if check_password_hash(row["code_hash"], normalized_code):
                    conn.execute("UPDATE mfa_recovery_codes SET used_at = ? WHERE id = ?", (iso_now(), row["id"]))
                    return True, "recovery_code"
        return False, "invalid"

    def disable_mfa(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET mfa_secret_encrypted = NULL, mfa_enabled_at = NULL, updated_at = ? WHERE id = ?",
                (iso_now(), user_id),
            )
            conn.execute("DELETE FROM mfa_recovery_codes WHERE user_id = ?", (user_id,))

    def privileged_mfa_status(self) -> dict[str, int | bool]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN mfa_enabled_at IS NOT NULL THEN 1 ELSE 0 END) AS enabled
                FROM users WHERE status = 'active' AND (is_owner = 1 OR role = 'admin')
                """
            ).fetchone()
        total = int(row["total"] or 0) if row else 0
        enabled = int(row["enabled"] or 0) if row else 0
        return {"total": total, "enabled": enabled, "ready": total > 0 and total == enabled}

    def exchange_validation_status(self) -> dict[str, int | bool]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN validation_status = 'verified_safe' THEN 1 ELSE 0 END) AS verified
                FROM exchange_credentials
                """
            ).fetchone()
        total = int(row["total"] or 0) if row else 0
        verified = int(row["verified"] or 0) if row else 0
        return {"total": total, "verified": verified, "ready": total > 0 and total == verified}

    def create_user(
        self,
        email: str,
        password: str,
        role: str = "user",
        is_owner: bool = False,
        accepted_terms_version: str | None = None,
    ) -> dict[str, Any]:
        normalized = email.strip().lower()
        if not normalized:
            raise ValueError("Email is required")
        if len(password) < 10:
            raise ValueError("Password must be at least 10 characters")
        now = utc_now()
        trial_started_at = now.isoformat()
        trial_ends_at = (now + timedelta(days=TRIAL_DAYS)).isoformat()
        subscription_expires_at = "2099-12-31T23:59:59+00:00" if is_owner or role == "admin" else None
        with self.connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO users (
                        email, password_hash, role, status, is_owner, trial_started_at,
                        trial_ends_at, subscription_expires_at, accepted_terms_at,
                        accepted_terms_version, risk_acknowledged_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized,
                        generate_password_hash(password),
                        role,
                        1 if is_owner else 0,
                        trial_started_at,
                        trial_ends_at,
                        subscription_expires_at,
                        now.isoformat() if accepted_terms_version else None,
                        accepted_terms_version,
                        now.isoformat() if accepted_terms_version else None,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("An account already exists for that email") from exc
            user_id = int(cursor.lastrowid)
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("Unable to create user")
        return user

    def upsert_user(self, email: str, password: str, role: str = "user", is_owner: bool = False) -> dict[str, Any]:
        existing = self.get_user_by_login(email)
        if existing:
            with self.connect() as conn:
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, role = ?, is_owner = ?, status = 'active',
                        subscription_expires_at = COALESCE(subscription_expires_at, ?), updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        generate_password_hash(password),
                        role,
                        1 if is_owner else 0,
                        "2099-12-31T23:59:59+00:00" if is_owner or role == "admin" else None,
                        iso_now(),
                        existing["id"],
                    ),
                )
            return self.get_user(existing["id"]) or existing
        return self.create_user(
            email,
            password,
            role=role,
            is_owner=is_owner,
            accepted_terms_version=LEGAL_VERSION if is_owner or role == "admin" else None,
        )

    def login_allowed(self, login: str, ip_address: str, limit: int = 5, window_minutes: int = 15) -> tuple[bool, int]:
        normalized_login = login.strip().lower()
        normalized_ip = ip_address.strip() or "unknown"
        cutoff = (utc_now() - timedelta(minutes=max(1, window_minutes))).isoformat()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS failures
                FROM auth_attempts
                WHERE success = 0 AND created_at >= ? AND (login = ? OR ip_address = ?)
                """,
                (cutoff, normalized_login, normalized_ip),
            ).fetchone()
        failures = int(row["failures"] or 0) if row else 0
        return failures < max(1, limit), max(0, max(1, limit) - failures)

    def record_login_attempt(self, login: str, ip_address: str, success: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO auth_attempts (login, ip_address, success, created_at) VALUES (?, ?, ?, ?)",
                (login.strip().lower(), ip_address.strip() or "unknown", 1 if success else 0, iso_now()),
            )
            cutoff = (utc_now() - timedelta(days=7)).isoformat()
            conn.execute("DELETE FROM auth_attempts WHERE created_at < ?", (cutoff,))

    def record_audit_event(
        self,
        event_type: str,
        outcome: str,
        *,
        actor_user_id: int | None = None,
        target: str = "",
        details: dict[str, Any] | None = None,
        ip_address: str = "",
        user_agent: str = "",
        request_id: str = "",
    ) -> None:
        safe_details = details or {}
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (
                    actor_user_id, event_type, outcome, target, details_json,
                    ip_address, user_agent, request_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_user_id,
                    event_type[:120],
                    outcome[:40],
                    target[:160],
                    json.dumps(safe_details, separators=(",", ":"), default=str)[:4000],
                    ip_address[:80],
                    user_agent[:300],
                    request_id[:100],
                    iso_now(),
                ),
            )

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT audit_events.*, users.email
                FROM audit_events
                LEFT JOIN users ON users.id = audit_events.actor_user_id
                ORDER BY audit_events.id DESC LIMIT ?
                """,
                (max(1, min(500, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def bootstrap_owner_and_admin(
        self,
        owner_email: str,
        owner_password: str,
        admin_login: str,
        admin_password: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.has_admin():
            raise ValueError("Setup is already complete")
        owner = self.upsert_user(owner_email, owner_password, role="owner", is_owner=True)
        admin = self.upsert_user(admin_login, admin_password, role="admin", is_owner=True)
        return owner, admin

    def subscription_summary(self, user: dict[str, Any] | None) -> dict[str, Any]:
        if not user:
            return {"state": "anonymous", "can_trade": False, "label": "Sign in required"}
        if user.get("role") == "admin" or user.get("is_owner"):
            return {
                "state": "active",
                "can_trade": True,
                "label": "Owner account",
                "days_left": 9999,
                "trial_ends_at": user.get("trial_ends_at"),
                "subscription_expires_at": user.get("subscription_expires_at"),
            }
        now = utc_now()
        trial_ends = parse_iso(user.get("trial_ends_at"))
        subscription_ends = parse_iso(user.get("subscription_expires_at"))
        if subscription_ends and subscription_ends > now:
            days_left = max(0, (subscription_ends - now).days + 1)
            return {
                "state": "active",
                "can_trade": True,
                "label": f"Active subscription ({days_left} days left)",
                "days_left": days_left,
                "trial_ends_at": user.get("trial_ends_at"),
                "subscription_expires_at": user.get("subscription_expires_at"),
            }
        if trial_ends and trial_ends > now:
            days_left = max(0, (trial_ends - now).days + 1)
            return {
                "state": "trial",
                "can_trade": True,
                "label": f"Trial active ({days_left} days left)",
                "days_left": days_left,
                "trial_ends_at": user.get("trial_ends_at"),
                "subscription_expires_at": user.get("subscription_expires_at"),
            }
        return {
            "state": "past_due",
            "can_trade": False,
            "label": "Subscription required",
            "days_left": 0,
            "trial_ends_at": user.get("trial_ends_at"),
            "subscription_expires_at": user.get("subscription_expires_at"),
        }

    def allows_new_trades(self, user: dict[str, Any] | None) -> bool:
        return bool(self.subscription_summary(user).get("can_trade"))

    def _fernet(self):
        from cryptography.fernet import Fernet

        if not self.credential_key_path.exists():
            self.credential_key_path.write_bytes(Fernet.generate_key())
        return Fernet(self.credential_key_path.read_bytes())

    def encrypt_text(self, value: str) -> str:
        return self._fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt_text(self, value: str) -> str:
        return self._fernet().decrypt(value.encode("ascii")).decode("utf-8")

    def store_exchange_credentials(
        self,
        user_id: int,
        exchange: str,
        api_key: str,
        api_secret: str,
        *,
        passphrase: str = "",
        label: str = "",
        validation_status: str = "unverified",
        validation: dict[str, Any] | None = None,
    ) -> None:
        normalized_exchange = exchange.strip().lower()
        if normalized_exchange not in {"binance", "bitget"}:
            raise ValueError("Unsupported exchange")
        if not api_key.strip() or not api_secret.strip():
            raise ValueError("API key and secret are required")
        if normalized_exchange == "bitget" and not passphrase.strip():
            raise ValueError("Bitget passphrase is required")
        now = iso_now()
        safe_validation = validation or {}
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO exchange_credentials (
                    user_id, exchange, api_key_encrypted, api_secret_encrypted,
                    passphrase_encrypted, label, validation_status, validation_json,
                    last_validated_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, exchange) DO UPDATE SET
                    api_key_encrypted = excluded.api_key_encrypted,
                    api_secret_encrypted = excluded.api_secret_encrypted,
                    passphrase_encrypted = excluded.passphrase_encrypted,
                    label = excluded.label,
                    validation_status = excluded.validation_status,
                    validation_json = excluded.validation_json,
                    last_validated_at = excluded.last_validated_at,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    normalized_exchange,
                    self.encrypt_text(api_key.strip()),
                    self.encrypt_text(api_secret.strip()),
                    self.encrypt_text(passphrase.strip()) if passphrase.strip() else None,
                    label.strip() or f"{normalized_exchange.title()} Spot",
                    validation_status[:40],
                    json.dumps(safe_validation, separators=(",", ":"), default=str)[:8000],
                    now if validation_status != "unverified" else None,
                    now,
                    now,
                ),
            )

    def get_exchange_credentials(self, user_id: int, exchange: str) -> dict[str, Any] | None:
        normalized_exchange = exchange.strip().lower()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM exchange_credentials WHERE user_id = ? AND exchange = ?",
                (user_id, normalized_exchange),
            ).fetchone()
        if row is None:
            return None
        validation: dict[str, Any] = {}
        try:
            validation = json.loads(row["validation_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            pass
        return {
            "exchange": normalized_exchange,
            "api_key": self.decrypt_text(row["api_key_encrypted"]),
            "api_secret": self.decrypt_text(row["api_secret_encrypted"]),
            "passphrase": self.decrypt_text(row["passphrase_encrypted"]) if row["passphrase_encrypted"] else "",
            "label": row["label"],
            "validation_status": row["validation_status"],
            "validation": validation,
            "last_validated_at": row["last_validated_at"],
            "updated_at": row["updated_at"],
        }

    def list_exchange_connections(self, user_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT exchange, label, validation_status, validation_json,
                       last_validated_at, updated_at, api_key_encrypted
                FROM exchange_credentials WHERE user_id = ? ORDER BY exchange
                """,
                (user_id,),
            ).fetchall()
        connections: list[dict[str, Any]] = []
        for row in rows:
            try:
                api_key = self.decrypt_text(row["api_key_encrypted"])
            except Exception:
                api_key = ""
            try:
                validation = json.loads(row["validation_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                validation = {}
            connections.append(
                {
                    "exchange": row["exchange"],
                    "label": row["label"],
                    "masked_key": f"{api_key[:6]}…{api_key[-4:]}" if len(api_key) > 12 else "configured",
                    "validation_status": row["validation_status"],
                    "validation": validation,
                    "last_validated_at": row["last_validated_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return connections

    def delete_exchange_credentials(self, user_id: int, exchange: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM exchange_credentials WHERE user_id = ? AND exchange = ?",
                (user_id, exchange.strip().lower()),
            )

    def has_exchange_credentials(self, user_id: int, exchange: str | None = None) -> bool:
        with self.connect() as conn:
            if exchange:
                row = conn.execute(
                    "SELECT 1 FROM exchange_credentials WHERE user_id = ? AND exchange = ?",
                    (user_id, exchange.strip().lower()),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM exchange_credentials WHERE user_id = ? LIMIT 1",
                    (user_id,),
                ).fetchone()
        return row is not None

    def store_binance_credentials(self, user_id: int, api_key: str, api_secret: str, label: str = "Binance Spot") -> None:
        self.store_exchange_credentials(user_id, "binance", api_key, api_secret, label=label)

    def get_binance_credentials(self, user_id: int) -> dict[str, str] | None:
        current = self.get_exchange_credentials(user_id, "binance")
        if current:
            return current
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM binance_credentials WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        legacy = {
            "api_key": self.decrypt_text(row["api_key_encrypted"]),
            "api_secret": self.decrypt_text(row["api_secret_encrypted"]),
            "label": row["label"],
            "updated_at": row["updated_at"],
        }
        self.store_exchange_credentials(
            user_id,
            "binance",
            legacy["api_key"],
            legacy["api_secret"],
            label=legacy["label"],
        )
        return self.get_exchange_credentials(user_id, "binance")

    def has_binance_credentials(self, user_id: int) -> bool:
        with self.connect() as conn:
            current = conn.execute(
                "SELECT 1 FROM exchange_credentials WHERE user_id = ? AND exchange = 'binance'",
                (user_id,),
            ).fetchone()
        if current is not None:
            return True
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM binance_credentials WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None

    def create_invoice(self, user_id: int) -> dict[str, Any]:
        if not self.payment_address:
            raise ValueError("USDT payment address is not configured. Set SAAS_USDT_PAYMENT_ADDRESS before accepting payments.")
        latest = self.latest_invoice(user_id)
        if latest and latest.get("status") == "pending":
            expires_at = parse_iso(latest.get("expires_at"))
            if expires_at and expires_at > utc_now():
                return latest
        now = utc_now()
        reference = "BT-" + base64.b32encode(secrets.token_bytes(8)).decode("ascii").rstrip("=")
        expires_at = (now + timedelta(hours=6)).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO payment_invoices (user_id, reference, amount_usdt, network, deposit_address, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (user_id, reference, f"{MONTHLY_PRICE_USDT:.2f}", self.payment_network, self.payment_address, now.isoformat(), expires_at),
            )
            invoice_id = int(cursor.lastrowid)
        return self.get_invoice(invoice_id) or {"id": invoice_id, "reference": reference}

    def get_invoice(self, invoice_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM payment_invoices WHERE id = ?", (invoice_id,)).fetchone()
        return self._row_to_dict(row)

    def latest_invoice(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM payment_invoices WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def approve_invoice(self, invoice_id: int, tx_hash: str = "manual-admin") -> None:
        invoice = self.get_invoice(invoice_id)
        if not invoice:
            raise ValueError("Invoice not found")
        if invoice.get("status") == "paid":
            raise ValueError("Invoice is already paid")
        normalized_tx_hash = tx_hash.strip()
        if len(normalized_tx_hash) < 8:
            raise ValueError("A verifiable payment transaction hash or provider reference is required")
        with self.connect() as conn:
            duplicate = conn.execute(
                "SELECT id FROM payment_invoices WHERE tx_hash = ? AND id != ?",
                (normalized_tx_hash, invoice_id),
            ).fetchone()
        if duplicate:
            raise ValueError("This payment transaction is already assigned to another invoice")
        now = utc_now()
        user = self.get_user(int(invoice["user_id"]))
        current_expiry = parse_iso(user.get("subscription_expires_at") if user else None)
        subscription_start = current_expiry if current_expiry and current_expiry > now else now
        subscription_ends = subscription_start + timedelta(days=30)
        with self.connect() as conn:
            conn.execute(
                "UPDATE payment_invoices SET status = 'paid', tx_hash = ?, paid_at = ? WHERE id = ?",
                (normalized_tx_hash, now.isoformat(), invoice_id),
            )
            conn.execute(
                "UPDATE users SET subscription_expires_at = ?, updated_at = ? WHERE id = ?",
                (subscription_ends.isoformat(), now.isoformat(), invoice["user_id"]),
            )

    def list_users_with_status(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            users = [dict(row) for row in conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()]
        for user in users:
            user["subscription"] = self.subscription_summary(user)
            user["has_credentials"] = self.has_exchange_credentials(int(user["id"])) or self.has_binance_credentials(int(user["id"]))
        return users

    def list_recent_invoices(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT payment_invoices.*, users.email
                    FROM payment_invoices
                    JOIN users ON users.id = payment_invoices.user_id
                    ORDER BY payment_invoices.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]

    def get_outbound_ip(self) -> str:
        configured = os.environ.get("SAAS_OUTBOUND_IP", "").strip()
        if configured:
            return configured
        cache_file = self.runtime_dir / "outbound_ip_cache.json"
        try:
            if cache_file.exists():
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                cached_at = parse_iso(payload.get("cached_at"))
                if cached_at and utc_now() - cached_at < timedelta(hours=12) and payload.get("ip"):
                    return str(payload["ip"])
        except Exception:
            pass
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=8) as response:
                ip = response.read().decode("utf-8").strip()
            cache_file.write_text(json.dumps({"ip": ip, "cached_at": iso_now()}), encoding="utf-8")
            return ip
        except Exception:
            return "Unable to detect. Set SAAS_OUTBOUND_IP on the server."
