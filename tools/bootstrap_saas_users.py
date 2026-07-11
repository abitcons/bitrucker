from __future__ import annotations

import getpass
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from binance_bot.saas import SaaSStore


def prompt_password(label: str) -> str:
    first = getpass.getpass(f"{label} password: ")
    second = getpass.getpass(f"Confirm {label} password: ")
    if first != second:
        raise SystemExit(f"{label} passwords did not match")
    if len(first) < 10:
        raise SystemExit(f"{label} password must be at least 10 characters")
    return first


def main() -> int:
    store = SaaSStore(ROOT)
    owner_email = input("Owner email [jimran@abitcons.com]: ").strip() or "jimran@abitcons.com"
    owner_password = prompt_password("Owner")
    admin_login = input("Admin username [admin]: ").strip() or "admin"
    admin_password = prompt_password("Admin")

    owner = store.upsert_user(owner_email, owner_password, role="owner", is_owner=True)
    admin = store.upsert_user(admin_login, admin_password, role="admin", is_owner=True)

    print(f"Owner account ready: {owner['email']}")
    print(f"Admin account ready: {admin['email']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())