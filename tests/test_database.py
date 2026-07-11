from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from binance_bot.database import (
    DatabaseConfig,
    connect_database,
    connect_postgresql,
    connect_sqlite,
)


class DatabaseConfigTests(unittest.TestCase):
    def test_default_database_preserves_existing_runtime_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            root = Path(temp_dir)
            config = DatabaseConfig.from_root(root)

            self.assertEqual(config.backend, "sqlite")
            self.assertEqual(config.sqlite_path, root / "runtime" / "saas.sqlite3")

            conn = connect_database(config)
            try:
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
                conn.execute(
                    "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO child (parent_id) VALUES (999)")
            finally:
                conn.close()

    def test_postgresql_driver_is_imported_only_when_connecting(self) -> None:
        config = DatabaseConfig.from_root(
            Path("unused"),
            "postgresql://app:password@db.example/bitrucker",
        )

        with patch("binance_bot.database.importlib.import_module") as import_module:
            self.assertEqual(config.backend, "postgresql")
            self.assertIsNone(config.sqlite_path)
            import_module.assert_not_called()

    def test_postgresql_connection_uses_expected_arguments(self) -> None:
        database_url = "postgresql://app:password@db.example/bitrucker"
        config = DatabaseConfig.from_root(Path("unused"), database_url)
        connection = Mock(name="postgresql_connection")
        driver = SimpleNamespace(connect=Mock(return_value=connection))
        dict_row = object()
        rows = SimpleNamespace(dict_row=dict_row)

        with patch(
            "binance_bot.database.importlib.import_module",
            side_effect=[driver, rows],
        ) as import_module:
            result = connect_database(config)

        self.assertIs(result, connection)
        self.assertEqual(
            import_module.call_args_list,
            [call("psycopg"), call("psycopg.rows")],
        )
        driver.connect.assert_called_once_with(
            database_url,
            autocommit=False,
            row_factory=dict_row,
        )

    def test_missing_postgresql_driver_has_clear_error(self) -> None:
        config = DatabaseConfig.from_root(
            Path("unused"),
            "postgresql://app:password@db.example/bitrucker",
        )

        with patch(
            "binance_bot.database.importlib.import_module",
            side_effect=ModuleNotFoundError("No module named psycopg"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"requires the 'psycopg\[binary\]' dependency",
            ):
                connect_postgresql(config)

    def test_postgresql_connection_error_does_not_expose_url(self) -> None:
        database_url = "postgresql://secret-user:secret-password@db.example/bitrucker"
        config = DatabaseConfig.from_root(Path("unused"), database_url)
        driver = SimpleNamespace(connect=Mock(side_effect=Exception(database_url)))
        rows = SimpleNamespace(dict_row=object())

        with patch(
            "binance_bot.database.importlib.import_module",
            side_effect=[driver, rows],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "^Unable to connect to the configured PostgreSQL database$",
            ) as raised:
                connect_postgresql(config)

        self.assertNotIn("secret-user", str(raised.exception))
        self.assertNotIn("secret-password", str(raised.exception))

    def test_wrong_config_for_provider_has_clear_error(self) -> None:
        sqlite_config = DatabaseConfig(backend="sqlite", sqlite_path=Path("test.sqlite3"))
        postgres_config = DatabaseConfig(
            backend="postgresql",
            url="postgresql://db.example/bitrucker",
        )

        with self.assertRaisesRegex(ValueError, "requires a SQLite"):
            connect_sqlite(postgres_config)
        with self.assertRaisesRegex(ValueError, "requires a PostgreSQL"):
            connect_postgresql(sqlite_config)

    def test_unknown_database_scheme_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported DATABASE_URL scheme"):
            DatabaseConfig.from_root(Path("unused"), "mysql://localhost/bitrucker")


if __name__ == "__main__":
    unittest.main()
