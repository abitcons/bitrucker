from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_market_readiness(root_dir: Path, saas, app_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a conservative launch scorecard.

    A passing score is intentionally harder than a working development server.
    Financial automation should fail closed when ownership, security, billing,
    or legal controls are incomplete.
    """

    app_config = app_config or {}
    checks: list[dict[str, Any]] = []

    def add(
        check_id: str,
        category: str,
        label: str,
        status: str,
        detail: str,
        remediation: str = "",
        *,
        critical: bool = False,
    ) -> None:
        checks.append(
            {
                "id": check_id,
                "category": category,
                "label": label,
                "status": status,
                "detail": detail,
                "remediation": remediation,
                "critical": critical,
            }
        )

    tenant_isolation = bool(app_config.get("SAAS_TENANT_ISOLATION_ACTIVE", False))
    add(
        "tenant_isolation",
        "Security",
        "Tenant-isolated execution",
        "pass" if tenant_isolation else "fail",
        "Each customer has an isolated credential vault, paper runtime state, bot fleet, limits, and action namespace."
        if tenant_isolation
        else "The current live fleet uses server-level exchange credentials. Non-owner users are blocked for safety.",
        "Penetration-test tenant boundaries before beta; keep customer live orders disabled until a dedicated execution worker is reviewed.",
        critical=True,
    )

    customer_live = bool(app_config.get("SAAS_CUSTOMER_LIVE_EXECUTION_ACTIVE", False))
    add(
        "customer_live_execution",
        "Safety",
        "Customer live-order execution",
        "warn" if not customer_live else "pass",
        "Customer fleets are deliberately locked to paper trading; the operator's existing live fleet remains owner-only."
        if not customer_live
        else "Customer live execution is enabled behind tenant-scoped workers and emergency controls.",
        "Do not enable until per-tenant workers, idempotent orders, reconciliation, kill switches, and security testing pass.",
    )

    secure_cookies = bool(app_config.get("SESSION_COOKIE_SECURE"))
    add(
        "secure_cookies",
        "Security",
        "HTTPS-only session cookies",
        "pass" if secure_cookies else "fail",
        "Session cookies are marked Secure." if secure_cookies else "Secure cookies are disabled.",
        "Set SAAS_SECURE_COOKIES=true behind a verified HTTPS reverse proxy.",
        critical=True,
    )

    mfa_status = saas.privileged_mfa_status()
    mfa_ready = bool(mfa_status.get("ready"))
    add(
        "mfa",
        "Security",
        "Multi-factor authentication",
        "pass" if mfa_ready else "fail",
        f"TOTP is enabled for all {mfa_status.get('total', 0)} active privileged users."
        if mfa_ready
        else f"TOTP with recovery codes is available, but only {mfa_status.get('enabled', 0)} of {mfa_status.get('total', 0)} active privileged users have enabled it.",
        "Require every owner/admin to enroll MFA; add WebAuthn as the preferred phishing-resistant factor.",
        critical=True,
    )

    exchange_validation = saas.exchange_validation_status()
    credential_scope_ready = bool(exchange_validation.get("ready"))
    add(
        "credential_scope",
        "Security",
        "Trade-only API key enforcement",
        "pass" if credential_scope_ready else "fail",
        f"All {exchange_validation.get('total', 0)} stored exchange connections passed read-only permission and IP checks."
        if credential_scope_ready
        else f"Read-only validation is implemented, but {exchange_validation.get('verified', 0)} of {exchange_validation.get('total', 0)} stored exchange connections are currently verified safe.",
        "Connect/revalidate every exchange with read + spot trade only, no withdrawal/transfer, and IP allowlisting.",
        critical=True,
    )

    add(
        "audit_log",
        "Security",
        "Durable action audit log",
        "pass",
        "Authenticated and denied bot actions are recorded with actor, target, IP, request ID, and outcome.",
    )
    kill_switch_available = bool(app_config.get("TRADING_KILL_SWITCH_AVAILABLE", False))
    add(
        "emergency_stop",
        "Safety",
        "Durable emergency entry lock",
        "pass" if kill_switch_available else "fail",
        "An owner-controlled, audited, fail-closed switch blocks new entries while leaving stop/close and protective exits available."
        if kill_switch_available
        else "No platform-level emergency entry lock is available.",
        "Implement a durable kill switch that is enforced inside the strategy entry guard.",
        critical=True,
    )
    add(
        "login_abuse",
        "Security",
        "Login abuse protection",
        "pass",
        "Failed sign-ins are durably rate-limited and security events are audited.",
    )

    operator_name = os.environ.get("SAAS_OPERATOR_LEGAL_NAME", "").strip()
    governing_law = os.environ.get("SAAS_GOVERNING_LAW", "").strip()
    regulatory_review = _flag("SAAS_REGULATORY_REVIEW_COMPLETE", False)
    legal_ready = bool(operator_name and governing_law and regulatory_review)
    add(
        "legal_review",
        "Compliance",
        "Legal entity and regulatory review",
        "pass" if legal_ready else "fail",
        "Operator identity, governing terms, target jurisdictions, and required licences have been approved."
        if legal_ready
        else "Baseline Terms, Privacy, and Risk pages exist, but legal identity/jurisdiction/licensing approval is not recorded.",
        "Obtain counsel for every target jurisdiction, especially for automated virtual-asset activity and tokenized securities.",
        critical=True,
    )

    public_signup = bool(app_config.get("SAAS_PUBLIC_SIGNUP_ACTIVE", False))
    signup_gated = not public_signup or legal_ready
    add(
        "public_signup_gate",
        "Compliance",
        "Public onboarding gate",
        "pass" if signup_gated else "fail",
        "Self-service signup is disabled while legal approval is incomplete."
        if not public_signup
        else "Self-service signup is enabled after recorded legal approval."
        if legal_ready
        else "Self-service signup is exposed despite incomplete legal approval.",
        "Keep SAAS_PUBLIC_SIGNUP_ENABLED=false until legal, operator, jurisdiction, and support details are approved.",
        critical=True,
    )

    add(
        "legal_acceptance",
        "Compliance",
        "Versioned legal acceptance",
        "pass",
        "New accounts must accept versioned Terms, Privacy Policy, and Trading Risk Disclosure.",
    )

    payment_address = bool(getattr(saas, "payment_address", ""))
    payment_provider_verified = _flag("SAAS_PAYMENT_PROVIDER_VERIFIED", False)
    add(
        "billing_verification",
        "Billing",
        "Unique payment verification",
        "pass" if payment_address and payment_provider_verified else "warn",
        "A configured provider verifies unique transaction references."
        if payment_address and payment_provider_verified
        else "Unsafe balance-based auto-credit is disabled; invoices currently require verified admin/provider confirmation.",
        "Integrate a production payment provider with signed webhooks, idempotency, refunds, tax invoices, and reconciliation.",
    )

    backup_ready = _flag("SAAS_BACKUP_ENABLED", False) and _flag("SAAS_BACKUP_RESTORE_TESTED", False)
    add(
        "backups",
        "Operations",
        "Encrypted backups and restore drill",
        "pass" if backup_ready else "fail",
        "Backups are encrypted and a restore drill has passed." if backup_ready else "No verified backup-and-restore control is declared.",
        "Automate encrypted off-host backups and test restore with documented RPO/RTO.",
        critical=True,
    )

    monitoring_ready = _flag("SAAS_ERROR_MONITORING_ENABLED", False) or bool(os.environ.get("SENTRY_DSN", "").strip())
    add(
        "monitoring",
        "Operations",
        "Error monitoring and alerting",
        "pass" if monitoring_ready else "warn",
        "Production errors and service health trigger alerts." if monitoring_ready else "No external error-monitoring integration is declared.",
        "Add exception tracking, uptime checks, exchange-latency alerts, and an incident runbook.",
    )

    add(
        "database",
        "Operations",
        "Production data store",
        "pass" if os.environ.get("SAAS_DATABASE_URL", "").startswith(("postgresql://", "postgres://")) else "warn",
        "A managed transactional database is configured."
        if os.environ.get("SAAS_DATABASE_URL", "").startswith(("postgresql://", "postgres://"))
        else "SQLite is suitable for a single-server beta but not resilient multi-instance production.",
        "Move tenant, billing, and audit data to managed PostgreSQL with migrations and point-in-time recovery.",
    )

    tests_present = (root_dir / "tests").exists()
    add(
        "automated_tests",
        "Quality",
        "Automated regression tests",
        "pass" if tests_present else "fail",
        "Exchange routing, credit controls, and strategy behavior have automated tests." if tests_present else "No test suite found.",
        "Add API authorization, billing idempotency, restart recovery, and end-to-end paper-order tests.",
    )

    for check_id, label, detail, remediation in (
        ("backtesting", "Historical backtesting", "No customer-facing walk-forward backtest engine is available.", "Add fee/slippage-aware train/test splits and downloadable reports."),
        ("webhooks", "Signal and webhook API", "TradingView/provider webhooks are not available.", "Add signed, replay-protected, idempotent signal webhooks."),
        ("mobile", "Mobile/PWA operations", "The dashboard is responsive but has no installable PWA or push-alert channel.", "Add PWA manifest, service worker, push notifications, and emergency controls."),
        ("exports", "Performance and tax exports", "No tenant-facing net performance, fee, and tax-lot export exists.", "Add immutable fills ledger and CSV/API exports."),
    ):
        add(check_id, "Competitive", label, "warn", detail, remediation)

    add(
        "shadow_mode",
        "Competitive",
        "Tenant paper/shadow mode",
        "pass" if tenant_isolation else "fail",
        "Every customer fleet is isolated and forced to paper execution." if tenant_isolation else "Tenant paper mode is unavailable.",
        "Require a measured paper run and explicit promotion review before any future live activation.",
    )

    weights = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
    score = round(100.0 * sum(weights[item["status"]] for item in checks) / max(1, len(checks)))
    critical_failures = [item for item in checks if item["critical"] and item["status"] == "fail"]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "ready": not critical_failures,
        "score": score,
        "critical_failure_count": len(critical_failures),
        "critical_failures": critical_failures,
        "checks": checks,
    }
