from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives import serialization

import base58

from binance_bot.database import DatabaseConfig, connect_sqlite


LOGGER = logging.getLogger(__name__)

TRONGRID_API = "https://api.trongrid.io"
USDT_CONTRACT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
MONTHLY_PRICE_USDT = 10

NETWORKS = [
    {"id": "TRC20", "label": "TRC20 (Tron)"},
    {"id": "ERC20", "label": "ERC20 (Ethereum)"},
    {"id": "BEP20", "label": "BEP20 (BNB Smart Chain)"},
    {"id": "POLYGON", "label": "Polygon"},
    {"id": "SOLANA", "label": "Solana"},
]


def _keccak256(data: bytes) -> bytes:
    return hashlib.sha3_256(data).digest()


def _double_sha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _derive_secp256k1_public(private_key_bytes: bytes) -> bytes:
    pk = ec.derive_private_key(int.from_bytes(private_key_bytes, "big"), ec.SECP256K1())
    return pk.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )


def _raw_pub(private_key_bytes: bytes) -> bytes:
    pub = _derive_secp256k1_public(private_key_bytes)
    return pub[1:] if pub[0] == 4 and len(pub) == 65 else pub


def generate_all_addresses() -> dict[str, Any]:
    """Generate one secp256k1 keypair + one Ed25519 keypair and derive
    addresses for TRC20, ERC20, BEP20, Polygon, and Solana."""
    key_secp = secrets.token_bytes(32)
    sk_hex = key_secp.hex()
    raw = _raw_pub(key_secp)
    keccak = _keccak256(raw)
    evm = keccak[-20:]
    tron_raw = b"\x41" + evm

    addr = {}

    # TRC20 â€“ TRON base58
    ck = _double_sha256(tron_raw)[:4]
    addr["TRC20"] = base58.b58encode(tron_raw + ck).decode("utf-8")

    # EVM chains â€“ hex address
    evm_hex = "0x" + evm.hex()
    addr["ERC20"] = evm_hex
    addr["BEP20"] = evm_hex
    addr["POLYGON"] = evm_hex

    # Solana â€“ Ed25519
    sol_pk = ed25519.Ed25519PrivateKey.generate().public_key()
    sol_raw = sol_pk.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    addr["SOLANA"] = base58.b58encode(sol_raw).decode("utf-8")

    return {"private_key": sk_hex, "addresses": addr}


def check_usdt_balance_tron(address: str) -> float:
    try:
        url = f"{TRONGRID_API}/v1/accounts/{address}/tokens?contract_address={USDT_CONTRACT_TRC20}"
        req = urllib.request.Request(url, headers={"User-Agent": "BitRucker-bot"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for token in data.get("data", []):
            if token.get("contract_address", "").upper() == USDT_CONTRACT_TRC20:
                return float(token.get("balance", "0")) / 1_000_000
        return 0.0
    except Exception as exc:
        LOGGER.warning("TRON usdt check %s: %s", address[:8], exc)
        return 0.0


def get_db(root_dir: Path) -> sqlite3.Connection:
    return connect_sqlite(DatabaseConfig.from_root(root_dir))


def init_wallet_schema(root_dir: Path) -> None:
    with get_db(root_dir) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS billing_wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                private_key_encrypted TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_checked TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS billing_wallet_addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_id INTEGER NOT NULL,
                network TEXT NOT NULL,
                address TEXT NOT NULL,
                UNIQUE(wallet_id, network),
                FOREIGN KEY(wallet_id) REFERENCES billing_wallets(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS billing_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_id INTEGER NOT NULL,
                network TEXT NOT NULL,
                tx_hash TEXT NOT NULL UNIQUE,
                amount_usdt REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                confirmations INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                FOREIGN KEY(wallet_id) REFERENCES billing_wallets(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_btx_wallet ON billing_transactions(wallet_id);
            CREATE INDEX IF NOT EXISTS idx_btx_status ON billing_transactions(status);
        """)


def get_or_create_wallet(user_id: int, root_dir: Path, saas) -> dict[str, Any]:
    init_wallet_schema(root_dir)
    db = get_db(root_dir)
    with db:
        row = db.execute("SELECT * FROM billing_wallets WHERE user_id = ?", (user_id,)).fetchone()

        # Legacy migration: old row has wallet_address but no addresses table entries.
        if row:
            wallet = dict(row)
            addrs = {}
            for ar in db.execute(
                "SELECT network, address FROM billing_wallet_addresses WHERE wallet_id = ?",
                (wallet["id"],),
            ).fetchall():
                addrs[ar["network"]] = {"network": ar["network"], "address": ar["address"]}

            # Migrate legacy wallet_address column to new addresses table.
            if not addrs and wallet.get("wallet_address"):
                legacy_addr = wallet["wallet_address"]
                db.execute(
                    "INSERT OR IGNORE INTO billing_wallet_addresses (wallet_id, network, address) VALUES (?, 'TRC20', ?)",
                    (wallet["id"], legacy_addr),
                )
                addrs["TRC20"] = {"network": "TRC20", "address": legacy_addr}

                # Also derive and store the EVM addresses from the encrypted private key.
                try:
                    pk_encrypted = wallet.get("private_key_encrypted", "")
                    if pk_encrypted:
                        sk = saas.decrypt_text(pk_encrypted)
                        raw = _raw_pub(bytes.fromhex(sk))
                        keccak = _keccak256(raw)
                        evm = "0x" + keccak[-20:].hex()
                        for net in ("ERC20", "BEP20", "POLYGON"):
                            db.execute(
                                "INSERT OR IGNORE INTO billing_wallet_addresses (wallet_id, network, address) VALUES (?, ?, ?)",
                                (wallet["id"], net, evm),
                            )
                            addrs[net] = {"network": net, "address": evm}
                except Exception:
                    pass

            wallet["addresses"] = addrs
            wallet["networks"] = addrs
            return wallet

    data = generate_all_addresses()
    enc = saas.encrypt_text(data["private_key"])
    now = datetime.now(UTC).isoformat()
    with db:
        cur = db.execute(
            "INSERT INTO billing_wallets (user_id, private_key_encrypted, created_at) VALUES (?, ?, ?)",
            (user_id, enc, now),
        )
        wid = int(cur.lastrowid)
        for net, addr in data["addresses"].items():
            db.execute(
                "INSERT INTO billing_wallet_addresses (wallet_id, network, address) VALUES (?, ?, ?)",
                (wid, net, addr),
            )
    return get_or_create_wallet(user_id, root_dir, saas)


def start_monitor(root_dir: Path) -> None:
    init_wallet_schema(root_dir)
    t = threading.Thread(target=_monitor_loop, args=(root_dir,), daemon=True)
    t.start()
    LOGGER.info("Billing monitor started")


def _monitor_loop(root_dir: Path) -> None:
    while True:
        try:
            db = get_db(root_dir)
            with db:
                pending = [
                    dict(r) for r in db.execute(
                        """
                        SELECT bt.*, bw.user_id FROM billing_transactions bt
                        JOIN billing_wallets bw ON bw.id = bt.wallet_id
                        WHERE bt.status = 'pending'
                        ORDER BY bt.created_at ASC
                        """
                    ).fetchall()
                ]
            for tx in pending:
                now_iso = datetime.now(UTC).isoformat()
                expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()
                with db:
                    db.execute(
                        "UPDATE billing_transactions SET status = 'completed', confirmed_at = ? WHERE id = ?",
                        (now_iso, tx["id"]),
                    )
                    db.execute(
                        "UPDATE users SET subscription_expires_at = ?, status = 'active', updated_at = ? WHERE id = ?",
                        (expires, now_iso, tx["user_id"]),
                    )
                LOGGER.info("User %s activated until %s via auto-monitor", tx["user_id"], expires)
            time.sleep(60)
        except Exception as exc:
            LOGGER.warning("Monitor error: %s", exc)
            time.sleep(120)
