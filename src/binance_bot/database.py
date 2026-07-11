from __future__ import annotations

import importlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class DatabaseConfig:
    """Database location shared by SaaS and billing persistence."""

    backend: str
    sqlite_path: Path | None = None
    url: str | None = None

    @classmethod
    def from_root(cls, root_dir: Path, database_url: str | None = None) -> "DatabaseConfig":
        configured_url = (database_url if database_url is not None else os.environ.get("DATABASE_URL", "")).strip()
        if not configured_url:
            return cls(backend="sqlite", sqlite_path=root_dir / "runtime" / "saas.sqlite3")

        parsed = urlparse(configured_url)
        scheme = parsed.scheme.lower()
        if scheme in {"postgres", "postgresql"}:
            return cls(backend="postgresql", url=configured_url)
        if scheme == "sqlite":
            raw_path = parsed.path
            if parsed.netloc:
                raw_path = f"//{parsed.netloc}{parsed.path}"
            if not raw_path:
                raise ValueError("SQLite DATABASE_URL must include a database path")
            return cls(backend="sqlite", sqlite_path=Path(raw_path))

        raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme or 'missing'}")


def connect_sqlite(config: DatabaseConfig) -> sqlite3.Connection:
    """Open the SQLite backend with consistent safety settings."""

    if config.backend != "sqlite" or config.sqlite_path is None:
        raise ValueError("SQLite connection requires a SQLite database configuration")

    config.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.sqlite_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect_postgresql(config: DatabaseConfig) -> Any:
    """Lazily import psycopg and open a PostgreSQL connection."""

    if config.backend != "postgresql" or not config.url:
        raise ValueError("PostgreSQL connection requires a PostgreSQL database configuration")

    try:
        psycopg = importlib.import_module("psycopg")
        rows = importlib.import_module("psycopg.rows")
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "PostgreSQL support requires the 'psycopg[binary]' dependency"
        ) from exc

    try:
        return psycopg.connect(
            config.url,
            autocommit=False,
            row_factory=rows.dict_row,
        )
    except Exception as exc:
        raise RuntimeError(
            "Unable to connect to the configured PostgreSQL database"
        ) from exc


def connect_database(config: DatabaseConfig) -> Any:
    """Open the configured provider without changing the default provider."""

    if config.backend == "sqlite":
        return connect_sqlite(config)
    if config.backend == "postgresql":
        return connect_postgresql(config)
    raise ValueError(f"Unsupported database backend: {config.backend}")
