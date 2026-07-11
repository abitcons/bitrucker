from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from functools import wraps
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, g, has_request_context, jsonify, redirect, render_template, request, session, url_for, Response

from .bootstrap import build_strategy
from .exchange_validation import validate_exchange_credentials
from .market_readiness import build_market_readiness
from .saas import LEGAL_VERSION, MONTHLY_PRICE_USDT, SaaSStore
from .tenant_fleet import TenantBotFleet
from .volatility_bot import VolatilityTwapBot


LOGGER = logging.getLogger(__name__)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "dry_run"}


def create_dashboard_app(root_dir: Path) -> Flask:
    strategy = build_strategy(root_dir)
    saas = SaaSStore(root_dir)
    access_lock = threading.RLock()

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = saas.session_secret
    app.config.update(
        MAX_CONTENT_LENGTH=max(64 * 1024, int(os.environ.get("SAAS_MAX_REQUEST_BYTES", str(1024 * 1024)))),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_flag("SAAS_SECURE_COOKIES", False),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=max(1, int(os.environ.get("SAAS_SESSION_HOURS", "12")))),
    )

    @app.before_request
    def assign_request_id() -> None:
        g.request_id = request.headers.get("X-Request-ID", "").strip()[:100] or secrets.token_urlsafe(12)

    @app.after_request
    def apply_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        response.headers.setdefault("Cache-Control", "no-store" if request.path.startswith(("/app", "/api", "/admin", "/bot")) else "no-cache")
        response.headers["X-Request-ID"] = g.get("request_id", "")
        if request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if token is None:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token

    def current_user() -> dict[str, Any] | None:
        return saas.get_user(session.get("user_id"))

    def current_subscription() -> dict[str, Any]:
        return saas.subscription_summary(current_user())

    app.jinja_env.globals["current_user"] = current_user
    app.jinja_env.globals["current_subscription"] = current_subscription
    app.jinja_env.globals["monthly_price_usdt"] = lambda: MONTHLY_PRICE_USDT

    kill_switch_file = root_dir / "runtime" / "trading_kill_switch.json"

    def _kill_switch_state() -> dict[str, Any]:
        if _env_flag("TRADING_KILL_SWITCH_ENGAGED", False):
            return {
                "engaged": True,
                "reason": "Engaged by TRADING_KILL_SWITCH_ENGAGED",
                "source": "environment",
            }
        try:
            payload = json.loads(kill_switch_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"engaged": False, "reason": "", "source": "file"}
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt safety control must fail closed, not silently re-enable entries.
            return {
                "engaged": True,
                "reason": f"Kill-switch state is unreadable: {type(exc).__name__}",
                "source": "fail_closed",
            }
        return {
            "engaged": bool(payload.get("engaged", False)),
            "reason": str(payload.get("reason") or ""),
            "source": "file",
            "updated_at": payload.get("updated_at"),
            "updated_by_user_id": payload.get("updated_by_user_id"),
        }

    def _write_kill_switch(engaged: bool, reason: str, actor_user_id: int) -> dict[str, Any]:
        if _env_flag("TRADING_KILL_SWITCH_ENGAGED", False) and not engaged:
            raise RuntimeError("The environment kill switch is engaged and cannot be cleared from the dashboard")
        payload = {
            "engaged": bool(engaged),
            "reason": str(reason or "").strip()[:500],
            "updated_at": datetime.now(UTC).isoformat(),
            "updated_by_user_id": int(actor_user_id),
        }
        kill_switch_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = kill_switch_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(kill_switch_file)
        return _kill_switch_state()

    def _legacy_strategy_entry_guard(symbol: str) -> tuple[bool, str]:
        state = _kill_switch_state()
        if state["engaged"]:
            reason = str(state.get("reason") or "operator emergency switch")
            return False, f"Entry blocked by platform emergency switch: {reason}"
        return True, f"Platform entry switch clear for {symbol}"

    strategy.entry_guard = _legacy_strategy_entry_guard

    def login_required(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            if current_user() is None:
                if request.path.startswith("/api/"):
                    # Local requests are handled by require_bot_action_auth; skip session check.
                    if _is_local_request() and (allow_local_actions or not enforce_local_action_auth):
                        return handler(*args, **kwargs)
                    return jsonify({"error": "Sign in required"}), 401
                return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
            return handler(*args, **kwargs)
        return wrapper

    def admin_required(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login", next=request.path))
            if user.get("role") != "admin" and not user.get("is_owner"):
                abort(403)
            return handler(*args, **kwargs)
        return wrapper

    def owner_required(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                return jsonify({"error": "Sign in required"}), 401
            if not user.get("is_owner"):
                return jsonify({"error": "Owner account required"}), 403
            return handler(*args, **kwargs)
        return wrapper

    def subscription_required_for_new_trades(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            kill_switch = _kill_switch_state()
            if kill_switch["engaged"]:
                return jsonify({
                    "error": "New entries are locked by the platform emergency switch",
                    "reason": kill_switch.get("reason", ""),
                }), 423
            user = current_user()
            if user is None:
                # Allow non-session bot actions when request-level auth already passes
                # (local bypass, API token, or Cloudflare service token).
                authorized, reason = _action_request_authorized()
                if not authorized:
                    return jsonify({"error": "Sign in required"}), 401
                return handler(*args, **kwargs)
            if not saas.allows_new_trades(user):
                return jsonify({"error": "Subscription required before opening new trades"}), 402
            return handler(*args, **kwargs)
        return wrapper

    action_api_token = os.environ.get("BOT_ACTION_API_TOKEN", "").strip()
    cf_access_client_id = os.environ.get("CF_ACCESS_CLIENT_ID", "").strip()
    cf_access_client_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET", "").strip()
    trust_cf_access_headers = _env_flag("BOT_TRUST_CF_ACCESS_HEADERS", False)
    enforce_local_action_auth = _env_flag("BOT_ENFORCE_LOCAL_ACTION_AUTH", True)
    allow_local_actions = _env_flag("BOT_ALLOW_LOCAL_ACTIONS", False)
    require_cf_access_for_bot = _env_flag("BOT_REQUIRE_CF_ACCESS", False)
    ad_users_only_mode = _env_flag("BOT_AD_USERS_ONLY", False)
    allowed_email_domain = os.environ.get("BOT_ALLOWED_EMAIL_DOMAIN", "").strip().lower()
    allowed_emails_raw = os.environ.get("BOT_ALLOWED_EMAILS", "").strip()
    allowed_emails = {item.strip().lower() for item in allowed_emails_raw.split(",") if item.strip()}
    # Customer sessions are routed only to per-user paper fleets. Customer live
    # execution remains separately disabled until a dedicated worker and kill
    # switch have passed production review.
    tenant_isolation_implemented = True
    tenant_isolation_enabled = tenant_isolation_implemented and _env_flag("SAAS_TENANT_ISOLATION_ENABLED", False)
    app.config["SAAS_TENANT_ISOLATION_ACTIVE"] = tenant_isolation_enabled
    app.config["SAAS_CUSTOMER_LIVE_EXECUTION_ACTIVE"] = False
    app.config["TRADING_KILL_SWITCH_AVAILABLE"] = True
    public_signup_enabled = _env_flag("SAAS_PUBLIC_SIGNUP_ENABLED", False)
    app.config["SAAS_PUBLIC_SIGNUP_ACTIVE"] = public_signup_enabled

    def _is_local_request() -> bool:
        remote = (request.remote_addr or "").strip()
        host = (request.host or "").lower()
        is_local_host = (
            host.startswith("127.0.0.1")
            or host.startswith("localhost")
            or host.startswith("[::1]")
            or host.startswith("::1")
        )
        is_local_remote = (
            not remote
            or remote in {"127.0.0.1", "::1", "0:0:0:0:0:0:0:1"}
            or remote.startswith("::ffff:127.0.0.1")
        )
        return is_local_host and is_local_remote

    def _action_request_authorized() -> tuple[bool, str]:
        user = current_user()
        if user is not None:
            if not tenant_isolation_enabled and not (user.get("is_owner") or user.get("role") == "admin"):
                return False, "Live trading is temporarily owner-only until tenant-isolated exchange execution is enabled"
            if user.get("mfa_enabled_at") and not session.get("mfa_verified_at"):
                return False, "Multi-factor authentication is required before trading actions"
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                expected_csrf = str(session.get("csrf_token") or "")
                provided_csrf = request.headers.get("X-CSRF-Token", "").strip() or request.form.get("csrf_token", "").strip()
                if not expected_csrf or not provided_csrf or not secrets.compare_digest(expected_csrf, provided_csrf):
                    return False, "Invalid or missing CSRF token"
            return True, "saas-session"

        if ad_users_only_mode and not _is_local_request():
            identity_ok, identity_reason = _cf_identity_authorized()
            if not identity_ok:
                return False, identity_reason
            return True, "ad-identity"

        if _is_local_request() and (allow_local_actions or not enforce_local_action_auth):
            return True, "local-bypass"

        configured_methods = []
        if action_api_token:
            configured_methods.append("BOT_ACTION_API_TOKEN")
        if cf_access_client_id and cf_access_client_secret:
            configured_methods.append("CF_ACCESS_CLIENT_ID/CF_ACCESS_CLIENT_SECRET")
        if trust_cf_access_headers:
            configured_methods.append("BOT_TRUST_CF_ACCESS_HEADERS")

        if not configured_methods:
            return False, (
                "Bot action auth is required for non-local requests, but no credentials are configured. "
                "Set BOT_ACTION_API_TOKEN or CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET."
            )

        if action_api_token:
            auth_header = request.headers.get("Authorization", "").strip()
            api_key_header = request.headers.get("X-Bot-Api-Key", "").strip()
            bearer_token = ""
            if auth_header.lower().startswith("bearer "):
                bearer_token = auth_header[7:].strip()
            if secrets.compare_digest(api_key_header, action_api_token) or secrets.compare_digest(bearer_token, action_api_token):
                return True, "api-token"

        if cf_access_client_id and cf_access_client_secret:
            provided_id = request.headers.get("CF-Access-Client-Id", "").strip()
            provided_secret = request.headers.get("CF-Access-Client-Secret", "").strip()
            if secrets.compare_digest(provided_id, cf_access_client_id) and secrets.compare_digest(provided_secret, cf_access_client_secret):
                return True, "cf-service-token"

        if trust_cf_access_headers:
            if request.headers.get("Cf-Access-Authenticated-User-Email") or request.headers.get("Cf-Access-Jwt-Assertion"):
                return True, "cf-access-identity"

        return False, "Unauthorized bot action request"

    def _cf_authenticated_email() -> str:
        return (request.headers.get("Cf-Access-Authenticated-User-Email", "") or "").strip().lower()

    def _cf_identity_authorized() -> tuple[bool, str]:
        if not trust_cf_access_headers:
            return False, "Cloudflare Access is required but BOT_TRUST_CF_ACCESS_HEADERS is false"

        has_cf_identity = bool(request.headers.get("Cf-Access-Jwt-Assertion") or _cf_authenticated_email())
        if not has_cf_identity:
            return False, "Cloudflare Access authentication required"

        email = _cf_authenticated_email()
        if allowed_emails and email not in allowed_emails:
            return False, "Authenticated user is not in BOT_ALLOWED_EMAILS"
        if allowed_email_domain and (not email or not email.endswith(f"@{allowed_email_domain}")):
            return False, "Authenticated user is not in BOT_ALLOWED_EMAIL_DOMAIN"
        return True, "cf-access-validated"

    def _bot_view_request_authorized() -> tuple[bool, str]:
        user = current_user()
        if user is not None and not tenant_isolation_enabled and not (user.get("is_owner") or user.get("role") == "admin"):
            return False, "Live bot access is owner-only until tenant-isolated exchange execution is enabled"
        if _is_local_request():
            return True, "local-bypass"
        if ad_users_only_mode:
            return _cf_identity_authorized()
        if not require_cf_access_for_bot:
            return True, "cf-access-not-required"
        return _cf_identity_authorized()

    @app.before_request
    def enforce_ad_users_only_mode():
        if not ad_users_only_mode:
            return None
        if _is_local_request():
            return None
        authorized, reason = _cf_identity_authorized()
        if authorized:
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": reason}), 401
        return abort(401, reason)

    def require_bot_action_auth(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            authorized, reason = _action_request_authorized()
            if not authorized:
                user = current_user()
                try:
                    saas.record_audit_event(
                        request.endpoint or request.path,
                        "denied",
                        actor_user_id=int(user["id"]) if user else None,
                        target=str((request.get_json(silent=True) or {}).get("symbol") or request.form.get("symbol") or ""),
                        details={"reason": reason, "auth": "denied"},
                        ip_address=request.remote_addr or "",
                        user_agent=request.user_agent.string,
                        request_id=g.get("request_id", ""),
                    )
                except Exception:
                    LOGGER.exception("Unable to record denied action audit event")
                return jsonify({"error": reason}), 401
            response = handler(*args, **kwargs)
            status_code = getattr(response, "status_code", None)
            if status_code is None and isinstance(response, tuple) and len(response) > 1 and isinstance(response[1], int):
                status_code = response[1]
            status_code = int(status_code or 200)
            user = current_user()
            payload = request.get_json(silent=True) or {}
            try:
                saas.record_audit_event(
                    request.endpoint or request.path,
                    "success" if status_code < 400 else "failed",
                    actor_user_id=int(user["id"]) if user else None,
                    target=str(payload.get("symbol") or request.form.get("symbol") or request.view_args or ""),
                    details={"status_code": status_code, "auth": reason},
                    ip_address=request.remote_addr or "",
                    user_agent=request.user_agent.string,
                    request_id=g.get("request_id", ""),
                )
            except Exception:
                LOGGER.exception("Unable to record action audit event")
            return response
        return wrapper

    def require_bot_view_access(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            authorized, reason = _bot_view_request_authorized()
            if not authorized:
                if request.path.startswith("/api/"):
                    return jsonify({"error": reason}), 401
                return abort(401, reason)
            return handler(*args, **kwargs)
        return wrapper

    def validate_csrf() -> None:
        expected = session.get("csrf_token")
        provided = request.form.get("csrf_token")
        if not expected or not provided or not secrets.compare_digest(expected, provided):
            abort(400, "Invalid CSRF token")

    @app.get("/setup")
    def setup() -> str:
        if saas.has_admin():
            return redirect(url_for("login"))
        return render_template("saas_setup.html")

    @app.post("/setup")
    def setup_post():
        if saas.has_admin():
            return redirect(url_for("login"))
        validate_csrf()
        owner_email = request.form.get("owner_email", "jimran@abitcons.com")
        owner_password = request.form.get("owner_password", "")
        owner_confirm = request.form.get("owner_confirm", "")
        admin_login = request.form.get("admin_login", "admin")
        admin_password = request.form.get("admin_password", "")
        admin_confirm = request.form.get("admin_confirm", "")
        if owner_password != owner_confirm or admin_password != admin_confirm:
            flash("Passwords do not match", "error")
            return redirect(url_for("setup"))
        try:
            owner, _admin = saas.bootstrap_owner_and_admin(owner_email, owner_password, admin_login, admin_password)
        except Exception as exc:
            flash(str(exc), "error")
            return redirect(url_for("setup"))
        session.clear()
        session.permanent = True
        session["user_id"] = owner["id"]
        session["csrf_token"] = secrets.token_urlsafe(32)
        flash("Setup complete. Owner and admin accounts are ready.", "success")
        return redirect(url_for("saas_home"))

    @app.get("/login")
    def login() -> str:
        if not saas.has_admin():
            return redirect(url_for("setup"))
        if current_user() is not None:
            return redirect(url_for("saas_home"))
        return render_template("saas_auth.html", mode="login", next_url=request.args.get("next", ""))

    @app.post("/login")
    def login_post():
        validate_csrf()
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        next_url = request.form.get("next", "") or url_for("saas_home")
        ip_address = request.remote_addr or "unknown"
        allowed, _remaining = saas.login_allowed(email, ip_address)
        if not allowed:
            saas.record_audit_event(
                "auth.login",
                "rate_limited",
                target=email.strip().lower(),
                ip_address=ip_address,
                user_agent=request.user_agent.string,
                request_id=g.get("request_id", ""),
            )
            flash("Too many failed sign-in attempts. Try again in 15 minutes.", "error")
            return redirect(url_for("login"))
        user = saas.authenticate(email, password)
        saas.record_login_attempt(email, ip_address, bool(user))
        if not user:
            saas.record_audit_event(
                "auth.login",
                "failed",
                target=email.strip().lower(),
                ip_address=ip_address,
                user_agent=request.user_agent.string,
                request_id=g.get("request_id", ""),
            )
            flash("Invalid email or password", "error")
            return redirect(url_for("login"))
        if user.get("mfa_enabled_at"):
            session.clear()
            session.permanent = True
            session["pending_mfa_user_id"] = int(user["id"])
            session["pending_mfa_next"] = next_url
            session["csrf_token"] = secrets.token_urlsafe(32)
            saas.record_audit_event(
                "auth.login.password",
                "mfa_required",
                actor_user_id=int(user["id"]),
                target=str(user.get("email") or ""),
                ip_address=ip_address,
                user_agent=request.user_agent.string,
                request_id=g.get("request_id", ""),
            )
            return redirect(url_for("login_mfa"))
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["csrf_token"] = secrets.token_urlsafe(32)
        saas.record_audit_event(
            "auth.login",
            "success",
            actor_user_id=int(user["id"]),
            target=str(user.get("email") or ""),
            ip_address=ip_address,
            user_agent=request.user_agent.string,
            request_id=g.get("request_id", ""),
        )
        safe_next_url = next_url if next_url.startswith("/") and not next_url.startswith("//") else url_for("saas_home")
        return redirect(safe_next_url)

    @app.get("/login/mfa")
    def login_mfa() -> str:
        pending_user_id = session.get("pending_mfa_user_id")
        if not pending_user_id:
            return redirect(url_for("login"))
        user = saas.get_user(int(pending_user_id))
        if not user or not user.get("mfa_enabled_at"):
            session.clear()
            return redirect(url_for("login"))
        return render_template("saas_mfa_login.html", pending_email=user.get("email", ""))

    @app.post("/login/mfa")
    def login_mfa_post():
        validate_csrf()
        pending_user_id = session.get("pending_mfa_user_id")
        if not pending_user_id:
            return redirect(url_for("login"))
        user = saas.get_user(int(pending_user_id))
        if not user:
            session.clear()
            return redirect(url_for("login"))
        ip_address = request.remote_addr or "unknown"
        rate_key = f"mfa:{user.get('email') or pending_user_id}"
        allowed, _remaining = saas.login_allowed(rate_key, ip_address, limit=8, window_minutes=15)
        if not allowed:
            flash("Too many failed MFA attempts. Try again in 15 minutes.", "error")
            return redirect(url_for("login_mfa"))
        verified, method = saas.verify_user_mfa(int(pending_user_id), request.form.get("code", ""))
        saas.record_login_attempt(rate_key, ip_address, verified)
        if not verified:
            saas.record_audit_event(
                "auth.mfa",
                "failed",
                actor_user_id=int(pending_user_id),
                target=str(user.get("email") or ""),
                ip_address=ip_address,
                user_agent=request.user_agent.string,
                request_id=g.get("request_id", ""),
            )
            flash("Invalid authenticator or recovery code", "error")
            return redirect(url_for("login_mfa"))
        next_url = str(session.get("pending_mfa_next") or url_for("saas_home"))
        session.clear()
        session.permanent = True
        session["user_id"] = int(pending_user_id)
        session["mfa_verified_at"] = datetime.now(UTC).isoformat()
        session["csrf_token"] = secrets.token_urlsafe(32)
        saas.record_audit_event(
            "auth.login",
            "success",
            actor_user_id=int(pending_user_id),
            target=str(user.get("email") or ""),
            details={"mfa_method": method},
            ip_address=ip_address,
            user_agent=request.user_agent.string,
            request_id=g.get("request_id", ""),
        )
        safe_next_url = next_url if next_url.startswith("/") and not next_url.startswith("//") else url_for("saas_home")
        return redirect(safe_next_url)

    @app.get("/signup")
    def signup() -> str:
        if not public_signup_enabled:
            abort(404)
        if current_user() is not None:
            return redirect(url_for("saas_home"))
        return render_template("saas_auth.html", mode="signup", next_url="", legal_version=LEGAL_VERSION)

    @app.post("/signup")
    def signup_post():
        if not public_signup_enabled:
            abort(404)
        validate_csrf()
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if request.form.get("accept_terms") != "yes":
            flash("You must accept the Terms, Privacy Policy, and Risk Disclosure.", "error")
            return redirect(url_for("signup"))
        if password != confirm_password:
            flash("Passwords do not match", "error")
            return redirect(url_for("signup"))
        try:
            user = saas.create_user(email, password, accepted_terms_version=LEGAL_VERSION)
        except Exception as exc:
            flash(str(exc), "error")
            return redirect(url_for("signup"))
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["csrf_token"] = secrets.token_urlsafe(32)
        saas.record_audit_event(
            "auth.signup",
            "success",
            actor_user_id=int(user["id"]),
            target=str(user.get("email") or ""),
            details={"legal_version": LEGAL_VERSION},
            ip_address=request.remote_addr or "",
            user_agent=request.user_agent.string,
            request_id=g.get("request_id", ""),
        )
        return redirect(url_for("saas_home"))

    @app.post("/logout")
    def logout():
        validate_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.get("/terms")
    def terms() -> str:
        return render_template(
            "saas_legal.html",
            document="terms",
            legal_version=LEGAL_VERSION,
            operator_name=os.environ.get("SAAS_OPERATOR_LEGAL_NAME", "BitRucker").strip() or "BitRucker",
            support_email=os.environ.get("SAAS_SUPPORT_EMAIL", "support@bitrucker.com").strip(),
            governing_law=os.environ.get("SAAS_GOVERNING_LAW", "To be confirmed before public launch").strip(),
        )

    @app.get("/privacy")
    def privacy() -> str:
        return render_template(
            "saas_legal.html",
            document="privacy",
            legal_version=LEGAL_VERSION,
            operator_name=os.environ.get("SAAS_OPERATOR_LEGAL_NAME", "BitRucker").strip() or "BitRucker",
            support_email=os.environ.get("SAAS_SUPPORT_EMAIL", "support@bitrucker.com").strip(),
            governing_law=os.environ.get("SAAS_GOVERNING_LAW", "To be confirmed before public launch").strip(),
        )

    @app.get("/risk-disclosure")
    def risk_disclosure() -> str:
        return render_template(
            "saas_legal.html",
            document="risk",
            legal_version=LEGAL_VERSION,
            operator_name=os.environ.get("SAAS_OPERATOR_LEGAL_NAME", "BitRucker").strip() or "BitRucker",
            support_email=os.environ.get("SAAS_SUPPORT_EMAIL", "support@bitrucker.com").strip(),
            governing_law=os.environ.get("SAAS_GOVERNING_LAW", "To be confirmed before public launch").strip(),
        )

    @app.get("/app")
    @login_required
    def saas_home() -> str:
        user = current_user()
        subscription = saas.subscription_summary(user)
        return render_template(
            "saas_dashboard.html",
            active_page="overview",
            user=user,
            subscription=subscription,
            outbound_ip=saas.get_outbound_ip(),
            has_credentials=saas.has_exchange_credentials(int(user["id"])) or saas.has_binance_credentials(int(user["id"])),
            latest_invoice=saas.latest_invoice(int(user["id"])),
        )

    @app.get("/app/bots")
    @login_required
    def saas_bots() -> str:
        user = current_user()
        return render_template(
            "saas_bots.html",
            active_page="bots",
            user=user,
            subscription=saas.subscription_summary(user),
            has_credentials=saas.has_exchange_credentials(int(user["id"])) or saas.has_binance_credentials(int(user["id"])),
        )

    @app.get("/app/bots/swing")
    @login_required
    @require_bot_view_access
    def saas_swing_bot() -> str:
        user = current_user()
        tenant_fleet = _tenant_fleet_for_request()
        pairs = _all_pairs()
        requested_symbol = _normalize_symbol(request.args.get("symbol"))
        active_symbol = requested_symbol if requested_symbol in pairs else (_active_default_symbol() or (pairs[0] if pairs else ""))
        fleet_bots = _bots_snapshot()
        pair_summaries = {
            symbol: {
                "position_cost_basis": float(item.state.position_cost_basis),
                "current_profit_usdt": float(item.state.current_profit_usdt),
            }
            for symbol, item in fleet_bots.items()
        }
        initial_candidate_payload = None
        initial_status: dict[str, Any] = {}
        initial_bot = None
        try:
            bot = _get_bot_from_request(create_if_missing=False)
            initial_bot = bot
            initial_status = _status_for_request(bot)
            initial_candidate_payload = bot.get_pair_candidates(force=False, symbols=pairs or None, max_seconds=5.0)
        except Exception as exc:
            LOGGER.warning("Unable to pre-render Swing candidates: %s", exc)
        if initial_status.get("supported_instruments"):
            supported_instruments = initial_status["supported_instruments"]
        elif tenant_fleet is not None:
            try:
                supported_instruments = tenant_fleet.supported_instruments(limit=100)
            except Exception as exc:
                LOGGER.warning("Unable to load tenant instrument catalog: %s", exc)
                supported_instruments = list(VolatilityTwapBot.supported_symbols)
        else:
            supported_instruments = list(VolatilityTwapBot.supported_symbols)
        return render_template(
            "saas_swing.html",
            active_page="bots",
            user=user,
            subscription=saas.subscription_summary(user),
            initial_pairs=pairs,
            active_symbol=active_symbol,
            pair_summaries=pair_summaries,
            initial_candidate_payload=initial_candidate_payload,
            initial_status=initial_status,
            customer_paper_mode=tenant_fleet is not None,
            initial_primary_exchange=(initial_bot.primary_exchange if initial_bot else (tenant_fleet.primary_exchange if tenant_fleet else "binance")),
            supported_symbols=supported_instruments,
        )

    @app.get("/app/settings")
    @login_required
    def saas_settings() -> str:
        user = current_user()
        credentials = saas.get_binance_credentials(int(user["id"]))
        connections = saas.list_exchange_connections(int(user["id"]))
        return render_template(
            "saas_settings.html",
            active_page="settings",
            user=user,
            subscription=saas.subscription_summary(user),
            outbound_ip=saas.get_outbound_ip(),
            connections=connections,
            masked_key=next((item["masked_key"] for item in connections if item["exchange"] == "binance"), ""),
            credentials_updated_at=credentials.get("updated_at") if credentials else "",
        )

    @app.post("/app/settings/exchange")
    @app.post("/app/settings/binance")
    @login_required
    def saas_save_binance_credentials():
        validate_csrf()
        user = current_user()
        exchange = request.form.get("exchange", "binance").strip().lower()
        try:
            validation = validate_exchange_credentials(
                exchange,
                request.form.get("api_key", ""),
                request.form.get("api_secret", ""),
                request.form.get("passphrase", ""),
            )
            if not validation.get("safe"):
                raise ValueError("Connection rejected: " + "; ".join(validation.get("errors") or ["unsafe API permissions"]))
            saas.store_exchange_credentials(
                int(user["id"]),
                exchange,
                request.form.get("api_key", ""),
                request.form.get("api_secret", ""),
                passphrase=request.form.get("passphrase", ""),
                label=request.form.get("label", f"{exchange.title()} Spot"),
                validation_status=str(validation.get("status") or "verified_safe"),
                validation=validation,
            )
        except Exception as exc:
            flash(str(exc), "error")
            saas.record_audit_event(
                "security.exchange_connection",
                "rejected",
                actor_user_id=int(user["id"]),
                target=exchange,
                details={"error": str(exc)[:300]},
                ip_address=request.remote_addr or "",
                user_agent=request.user_agent.string,
                request_id=g.get("request_id", ""),
            )
        else:
            _evict_tenant_fleet(int(user["id"]))
            flash(f"{exchange.title()} trade-only API credentials verified and encrypted.", "success")
            saas.record_audit_event(
                "security.exchange_connection",
                "verified",
                actor_user_id=int(user["id"]),
                target=exchange,
                details={"validation_status": validation.get("status")},
                ip_address=request.remote_addr or "",
                user_agent=request.user_agent.string,
                request_id=g.get("request_id", ""),
            )
        return redirect(url_for("saas_settings"))

    @app.post("/app/settings/exchange/<exchange>/validate")
    @login_required
    def saas_validate_exchange_credentials(exchange: str):
        validate_csrf()
        user = current_user()
        credentials = saas.get_exchange_credentials(int(user["id"]), exchange)
        if not credentials:
            flash("Exchange connection not found", "error")
            return redirect(url_for("saas_settings"))
        try:
            validation = validate_exchange_credentials(
                exchange,
                credentials["api_key"],
                credentials["api_secret"],
                credentials.get("passphrase", ""),
            )
            if not validation.get("safe"):
                raise ValueError("Connection rejected: " + "; ".join(validation.get("errors") or ["unsafe API permissions"]))
            saas.store_exchange_credentials(
                int(user["id"]),
                exchange,
                credentials["api_key"],
                credentials["api_secret"],
                passphrase=credentials.get("passphrase", ""),
                label=credentials.get("label", ""),
                validation_status=str(validation.get("status") or "verified_safe"),
                validation=validation,
            )
        except Exception as exc:
            flash(str(exc), "error")
        else:
            flash(f"{exchange.title()} connection revalidated.", "success")
        return redirect(url_for("saas_settings"))

    @app.post("/app/settings/exchange/<exchange>/delete")
    @login_required
    def saas_delete_exchange_credentials(exchange: str):
        validate_csrf()
        user = current_user()
        if not saas.verify_password(int(user["id"]), request.form.get("password", "")):
            flash("Password is incorrect", "error")
            return redirect(url_for("saas_settings"))
        if user.get("mfa_enabled_at"):
            verified, _method = saas.verify_user_mfa(int(user["id"]), request.form.get("code", ""))
            if not verified:
                flash("Valid MFA or recovery code required", "error")
                return redirect(url_for("saas_settings"))
        saas.delete_exchange_credentials(int(user["id"]), exchange)
        _evict_tenant_fleet(int(user["id"]))
        saas.record_audit_event(
            "security.exchange_connection",
            "deleted",
            actor_user_id=int(user["id"]),
            target=exchange,
            ip_address=request.remote_addr or "",
            user_agent=request.user_agent.string,
            request_id=g.get("request_id", ""),
        )
        flash(f"{exchange.title()} connection deleted. Revoke the key at the exchange as well.", "success")
        return redirect(url_for("saas_settings"))

    @app.get("/app/security")
    @login_required
    def saas_security() -> str:
        user = current_user()
        return render_template(
            "saas_security.html",
            active_page="security",
            user=user,
            subscription=saas.subscription_summary(user),
            mfa_setup=saas.get_pending_mfa_setup(int(user["id"])),
            recovery_codes=session.pop("new_mfa_recovery_codes", None),
        )

    @app.post("/app/security/mfa/start")
    @login_required
    def saas_mfa_start():
        validate_csrf()
        user = current_user()
        try:
            saas.begin_mfa_setup(int(user["id"]))
        except Exception as exc:
            flash(str(exc), "error")
        else:
            saas.record_audit_event(
                "security.mfa_setup",
                "started",
                actor_user_id=int(user["id"]),
                target=str(user.get("email") or ""),
                ip_address=request.remote_addr or "",
                user_agent=request.user_agent.string,
                request_id=g.get("request_id", ""),
            )
        return redirect(url_for("saas_security"))

    @app.post("/app/security/mfa/enable")
    @login_required
    def saas_mfa_enable():
        validate_csrf()
        user = current_user()
        try:
            recovery_codes = saas.enable_mfa(int(user["id"]), request.form.get("code", ""))
        except Exception as exc:
            flash(str(exc), "error")
        else:
            session["new_mfa_recovery_codes"] = recovery_codes
            session["mfa_verified_at"] = datetime.now(UTC).isoformat()
            saas.record_audit_event(
                "security.mfa",
                "enabled",
                actor_user_id=int(user["id"]),
                target=str(user.get("email") or ""),
                ip_address=request.remote_addr or "",
                user_agent=request.user_agent.string,
                request_id=g.get("request_id", ""),
            )
            flash("MFA enabled. Save the recovery codes now; they will not be shown again.", "success")
        return redirect(url_for("saas_security"))

    @app.post("/app/security/mfa/disable")
    @login_required
    def saas_mfa_disable():
        validate_csrf()
        user = current_user()
        if not saas.verify_password(int(user["id"]), request.form.get("password", "")):
            flash("Password is incorrect", "error")
            return redirect(url_for("saas_security"))
        verified, _method = saas.verify_user_mfa(int(user["id"]), request.form.get("code", ""))
        if not verified:
            flash("Invalid authenticator or recovery code", "error")
            return redirect(url_for("saas_security"))
        saas.disable_mfa(int(user["id"]))
        session.pop("mfa_verified_at", None)
        saas.record_audit_event(
            "security.mfa",
            "disabled",
            actor_user_id=int(user["id"]),
            target=str(user.get("email") or ""),
            ip_address=request.remote_addr or "",
            user_agent=request.user_agent.string,
            request_id=g.get("request_id", ""),
        )
        flash("MFA disabled", "success")
        return redirect(url_for("saas_security"))

    @app.get("/app/billing")
    @login_required
    def saas_billing() -> str:
        user = current_user()
        with saas.connect() as conn:
            invoices = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM payment_invoices WHERE user_id = ? ORDER BY id DESC LIMIT 25",
                    (int(user["id"]),),
                ).fetchall()
            ]
        latest_invoice = invoices[0] if invoices else None
        return render_template(
            "saas_billing.html",
            active_page="billing",
            user=user,
            subscription=saas.subscription_summary(user),
            payment_address=saas.payment_address,
            payment_network=saas.payment_network,
            latest_invoice=latest_invoice,
            invoices=invoices,
        )

    @app.get("/api/billing/check")
    @login_required
    def api_billing_check():
        user = current_user()
        invoice = saas.latest_invoice(int(user["id"]))
        subscription = saas.subscription_summary(user)
        if invoice and invoice.get("status") == "paid":
            return jsonify({
                "status": "active",
                "invoice_status": "paid",
                "reference": invoice.get("reference"),
                "message": "Payment verified and subscription activated.",
            })
        return jsonify({
            "status": "pending",
            "invoice_status": invoice.get("status") if invoice else "none",
            "reference": invoice.get("reference") if invoice else None,
            "subscription_state": subscription.get("state"),
            "message": "Awaiting verified payment confirmation. A wallet balance alone never activates a subscription.",
        })

    @app.post("/app/billing/invoice")
    @login_required
    def saas_create_invoice():
        validate_csrf()
        user = current_user()
        try:
            invoice = saas.create_invoice(int(user["id"]))
        except Exception as exc:
            flash(str(exc), "error")
        else:
            flash(
                f"Invoice {invoice['reference']} created. Send {invoice['amount_usdt']} USDT on {invoice['network']} "
                "and provide the reference for verification.",
                "success",
            )
        return redirect(url_for("saas_billing"))

    @app.post("/app/billing/payment-settings")
    @admin_required
    def saas_payment_settings():
        validate_csrf()
        try:
            saas.set_payment_settings(
                request.form.get("payment_address", ""),
                request.form.get("payment_network", "TRC20"),
            )
        except Exception as exc:
            flash(str(exc), "error")
        else:
            flash("USDT payment wallet updated.", "success")
        return redirect(url_for("saas_billing"))

    @app.get("/admin/subscriptions")
    @admin_required
    def admin_subscriptions() -> str:
        return render_template(
            "saas_admin.html",
            active_page="admin",
            user=current_user(),
            subscription=current_subscription(),
            users=saas.list_users_with_status(),
            invoices=saas.list_recent_invoices(),
        )

    @app.post("/admin/invoices/<int:invoice_id>/approve")
    @admin_required
    def admin_approve_invoice(invoice_id: int):
        validate_csrf()
        try:
            saas.approve_invoice(invoice_id, request.form.get("tx_hash", ""))
        except Exception as exc:
            flash(str(exc), "error")
        else:
            flash("Invoice approved and subscription extended for 30 days.", "success")
        return redirect(url_for("admin_subscriptions"))

    @app.get("/")
    def index() -> Any:
        if current_user():
            return redirect(url_for("saas_home"))
        return render_template(
            "saas_landing.html",
            monthly_price=MONTHLY_PRICE_USDT,
            public_signup_enabled=public_signup_enabled,
        )

    @app.get("/legacy")
    @login_required
    @owner_required
    @require_bot_view_access
    def legacy_dashboard() -> str:
        with access_lock:
            try:
                view = strategy.build_dashboard_view()
            except Exception as exc:
                view = {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "dry_run": strategy.settings.dry_run,
                    "poll_interval_seconds": strategy.settings.poll_interval_seconds,
                    "profit_target_display": f"{strategy.settings.profit_target_usd:.2f}",
                    "profit_target_input_value": f"{strategy.settings.profit_target_usd:.2f}",
                    "profit_target_default_display": f"{strategy.settings.profit_target_usd:.2f}",
                    "profit_target_source_label": "CONFIG",
                    "profit_target_source_class": "pill-slate",
                    "profit_target_note": "Set a runtime whole-strategy USD trigger here to change the managed basket exit target without editing strategy.json. Any configured profit-percent threshold still applies.",
                    "profit_target_updated_at": "-",
                    "can_clear_profit_target": False,
                    "total_value_display": "-",
                    "total_profit_display": "-",
                    "total_profit_class": "neutral",
                    "managed_profit_display": "-",
                    "managed_profit_class": "neutral",
                    "active_cycle_count": 0,
                    "can_sell_all": False,
                    "portfolio_value_target_display": "-",
                    "portfolio_value_target_status_label": "OFF",
                    "portfolio_value_target_status_class": "pill-slate",
                    "portfolio_value_target_note": "Set a portfolio value trigger to liquidate everything when the account reaches it.",
                    "portfolio_value_target_updated_at": "-",
                    "portfolio_value_target_triggered_at": "-",
                    "portfolio_value_target_input_value": "",
                    "can_clear_portfolio_value_target": False,
                    "warnings": [str(exc)],
                    "snapshots": [],
                    "cycles": [],
                    "operations": [],
                    "bitget_enabled": strategy.settings.bitget_enabled,
                    "bitget_orders": [],
                }
        return render_template("dashboard.html", view=view)

    @app.post("/actions/sell-all")
    @login_required
    @owner_required
    @require_bot_action_auth
    def sell_all_assets():
        validate_csrf()
        with access_lock:
            try:
                message = strategy.manual_sell_all_to_usdt()
            except Exception as exc:
                flash(str(exc), "error")
            else:
                flash(message, "success")
        return redirect(url_for("index"))

    @app.post("/actions/portfolio-target")
    @login_required
    @owner_required
    @require_bot_action_auth
    def set_portfolio_target():
        validate_csrf()
        target_value = request.form.get("target_value", "")
        with access_lock:
            try:
                message = strategy.set_portfolio_value_target(target_value)
            except Exception as exc:
                flash(str(exc), "error")
            else:
                flash(message, "success")
        return redirect(url_for("index"))

    @app.post("/actions/profit-target")
    @login_required
    @owner_required
    @require_bot_action_auth
    def set_profit_target():
        validate_csrf()
        target_value = request.form.get("target_value", "")
        with access_lock:
            try:
                message = strategy.set_whole_strategy_profit_target(target_value)
            except Exception as exc:
                flash(str(exc), "error")
            else:
                flash(message, "success")
        return redirect(url_for("index"))

    @app.post("/actions/profit-target/clear")
    @login_required
    @owner_required
    @require_bot_action_auth
    def clear_profit_target():
        validate_csrf()
        with access_lock:
            try:
                message = strategy.clear_whole_strategy_profit_target()
            except Exception as exc:
                flash(str(exc), "error")
            else:
                flash(message, "success")
        return redirect(url_for("index"))

    @app.post("/actions/portfolio-target/clear")
    @login_required
    @owner_required
    @require_bot_action_auth
    def clear_portfolio_target():
        validate_csrf()
        with access_lock:
            try:
                message = strategy.clear_portfolio_value_target()
            except Exception as exc:
                flash(str(exc), "error")
            else:
                flash(message, "success")
        return redirect(url_for("index"))

    @app.post("/actions/sell/<asset>")
    @login_required
    @owner_required
    @require_bot_action_auth
    def sell_asset(asset: str):
        validate_csrf()
        restart_cycle = request.form.get("restart_cycle", "true").lower() == "true"
        with access_lock:
            try:
                message = strategy.manual_sell_asset(asset, restart_cycle=restart_cycle)
            except Exception as exc:
                flash(str(exc), "error")
            else:
                flash(message, "success")
        return redirect(url_for("index"))

    @app.post("/actions/stop/<asset>")
    @login_required
    @owner_required
    @require_bot_action_auth
    def stop_asset(asset: str):
        validate_csrf()
        with access_lock:
            try:
                message = strategy.stop_asset_operation(asset)
            except Exception as exc:
                flash(str(exc), "error")
            else:
                flash(message, "success")
        return redirect(url_for("index"))

    @app.post("/actions/start/<asset>")
    @login_required
    @owner_required
    @subscription_required_for_new_trades
    @require_bot_action_auth
    def start_asset(asset: str):
        validate_csrf()
        with access_lock:
            try:
                message = strategy.start_asset_operation(asset)
            except Exception as exc:
                flash(str(exc), "error")
            else:
                flash(message, "success")
        return redirect(url_for("index"))

    @app.post("/actions/allow/<asset>")
    @login_required
    @owner_required
    @subscription_required_for_new_trades
    @require_bot_action_auth
    def allow_asset(asset: str):
        validate_csrf()
        with access_lock:
            try:
                message = strategy.allow_asset_operation(asset)
            except Exception as exc:
                flash(str(exc), "error")
            else:
                flash(message, "success")
        return redirect(url_for("index"))

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "ok": True,
            "service": "bitrucker-dashboard",
            "time": datetime.now(UTC).isoformat(),
            "request_id": g.get("request_id", ""),
        }

    @app.get("/health/ready")
    def health_ready():
        readiness = build_market_readiness(root_dir, saas, app.config)
        payload = {
            "ready": readiness["ready"],
            "score": readiness["score"],
            "critical_failure_count": readiness["critical_failure_count"],
            "critical_check_ids": [item["id"] for item in readiness["critical_failures"]],
            "time": readiness["generated_at"],
        }
        return jsonify(payload), 200 if readiness["ready"] else 503

    @app.get("/app/readiness")
    @admin_required
    def market_readiness() -> str:
        readiness = build_market_readiness(root_dir, saas, app.config)
        return render_template(
            "saas_readiness.html",
            active_page="readiness",
            user=current_user(),
            subscription=current_subscription(),
            readiness=readiness,
            audit_events=saas.list_audit_events(limit=50),
        )

    @app.get("/api/market-readiness")
    @owner_required
    def market_readiness_api():
        return jsonify(build_market_readiness(root_dir, saas, app.config))

    @app.get("/api/trading-kill-switch")
    @login_required
    @owner_required
    def trading_kill_switch_get():
        return jsonify(_kill_switch_state())

    @app.post("/api/trading-kill-switch")
    @login_required
    @owner_required
    @require_bot_action_auth
    def trading_kill_switch_set():
        data = request.get_json(silent=True) or {}
        engaged = data.get("engaged")
        reason = str(data.get("reason") or "").strip()
        if not isinstance(engaged, bool):
            return jsonify({"error": "engaged must be true or false"}), 400
        if len(reason) < 5:
            return jsonify({"error": "A reason of at least 5 characters is required"}), 400
        user = current_user()
        if not engaged and not saas.verify_password(int(user["id"]), str(data.get("password") or "")):
            return jsonify({"error": "Current owner password is required to clear the emergency switch"}), 403
        state = _write_kill_switch(engaged, reason, int(user["id"]))
        saas.record_audit_event(
            "trading.kill_switch",
            "engaged" if engaged else "cleared",
            actor_user_id=int(user["id"]),
            details={"reason": reason, "source": state.get("source")},
            ip_address=request.remote_addr or "",
            user_agent=request.user_agent.string,
            request_id=g.get("request_id", ""),
        )
        return jsonify(state)

    # Initialize volatility bot fleet (can be empty until a pair is added)
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    # Swing bot must run live. Do not allow env/config drift to switch it to paper mode.
    dry_run_mode = False
    primary_symbol = VolatilityTwapBot.symbol
    bots_lock = threading.RLock()
    bots: dict[str, VolatilityTwapBot] = {}
    default_symbol = ""
    tenant_fleets_lock = threading.RLock()
    tenant_fleets: dict[int, TenantBotFleet] = {}
    managed_pairs_file = root_dir / "runtime" / "managed_pairs.json"
    entry_rank_lock = threading.RLock()
    entry_rank_cache_at = 0.0
    entry_rank_cache: dict[str, Any] = {}
    auto_discovery_enabled = os.environ.get("BOT_AUTO_DISCOVERY_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    auto_discovery_interval_seconds = max(300, int(os.environ.get("BOT_AUTO_DISCOVERY_INTERVAL_SECONDS", "3600")))
    auto_discovery_scan_limit = max(5, min(100, int(os.environ.get("BOT_AUTO_DISCOVERY_SCAN_LIMIT", "40"))))
    auto_discovery_max_pairs = max(1, min(12, int(os.environ.get("BOT_AUTO_DISCOVERY_MAX_WORKING_PAIRS", "4"))))
    auto_discovery_min_score = float(os.environ.get("BOT_AUTO_DISCOVERY_MIN_SCORE", "35"))
    seed_working_pairs: list[str] = []

    def _tenant_fleet_for_request() -> TenantBotFleet | None:
        if not tenant_isolation_enabled or not has_request_context():
            return None
        user = current_user()
        if user is None or user.get("is_owner") or user.get("role") == "admin":
            return None
        user_id = int(user["id"])
        with tenant_fleets_lock:
            existing = tenant_fleets.get(user_id)
            if existing:
                return existing

        connections = saas.list_exchange_connections(user_id)
        verified_exchanges = [
            str(item.get("exchange") or "").lower()
            for item in connections
            if item.get("validation_status") == "verified_safe"
        ]
        primary_exchange = verified_exchanges[0] if verified_exchanges else "binance"
        credentials: dict[str, dict[str, str]] = {}
        for exchange in verified_exchanges:
            item = saas.get_exchange_credentials(user_id, exchange)
            if item:
                credentials[exchange] = {
                    "api_key": str(item.get("api_key") or ""),
                    "api_secret": str(item.get("api_secret") or ""),
                    "passphrase": str(item.get("passphrase") or ""),
                }
        fleet = TenantBotFleet(
            root_dir,
            user_id,
            primary_exchange=primary_exchange,
            exchange_credentials=credentials,
            dry_run=True,
            max_pairs=max(1, min(8, int(os.environ.get("SAAS_TENANT_MAX_PAIRS", "4")))),
        )
        with tenant_fleets_lock:
            return tenant_fleets.setdefault(user_id, fleet)

    def _evict_tenant_fleet(user_id: int) -> None:
        with tenant_fleets_lock:
            fleet = tenant_fleets.pop(int(user_id), None)
        if fleet:
            fleet.close()

    def _persist_managed_pairs_state_locked() -> None:
        payload = {
            "default_symbol": default_symbol,
            "pairs": sorted(bots.keys()),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        managed_pairs_file.parent.mkdir(parents=True, exist_ok=True)
        managed_pairs_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load_managed_pairs_state() -> tuple[list[str], str | None]:
        if not managed_pairs_file.exists():
            return [], None
        try:
            payload = json.loads(managed_pairs_file.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning("Unable to read managed pairs state: %s", exc)
            return [], None

        pairs = payload.get("pairs")
        loaded_pairs = []
        if isinstance(pairs, list):
            loaded_pairs = [
                _normalize_symbol(item)
                for item in pairs
                if isinstance(item, str) and _normalize_symbol(item)
            ]
        default_loaded = payload.get("default_symbol")
        default_loaded = _normalize_symbol(default_loaded) if isinstance(default_loaded, str) else None
        return loaded_pairs, default_loaded

    def _normalize_symbol(symbol: str | None) -> str:
        return (symbol or "").upper().replace("/", "").strip()

    seed_working_pairs = [
        normalized
        for normalized in (
            _normalize_symbol(item)
            for item in os.environ.get("BOT_SEED_WORKING_PAIRS", "BNBUSDT,UNIUSDT,EPICUSDT").split(",")
        )
        if normalized
    ]

    def _bot_runtime_root(symbol: str) -> Path:
        return root_dir / "runtime" / f"bot_{symbol.lower()}"

    def _entry_rank_guard(symbol: str) -> tuple[bool, str]:
        nonlocal entry_rank_cache_at, entry_rank_cache
        normalized = _normalize_symbol(symbol)
        kill_switch = _kill_switch_state()
        if kill_switch["engaged"]:
            reason = str(kill_switch.get("reason") or "operator emergency switch")
            return False, f"Entry blocked by platform emergency switch: {reason}"
        with bots_lock:
            managed_symbols = sorted(bots.keys())
            rank_bot = bots.get(default_symbol) or (bots.get(managed_symbols[0]) if managed_symbols else None)
            active_exposure = [
                other_symbol
                for other_symbol, other_bot in bots.items()
                if other_symbol != normalized
                and (
                    other_bot.state.position_cost_basis >= other_bot.dust_position_cost_usdt
                    or bool(other_bot.state.active_twap_algo_id)
                    or bool(other_bot.state.target_sell_order_id)
                )
            ]
        if active_exposure:
            return False, "Entry blocked: active exposure already open on " + ", ".join(sorted(active_exposure))
        if len(managed_symbols) <= 1 or rank_bot is None:
            return True, "Only one active pair"

        now = time.time()
        with entry_rank_lock:
            if not entry_rank_cache or now - entry_rank_cache_at > 45:
                entry_rank_cache = rank_bot.get_pair_candidates(force=False, symbols=managed_symbols, max_seconds=5.0)
                entry_rank_cache_at = now
            payload = entry_rank_cache

        candidates = payload.get("candidates") if isinstance(payload, dict) else []
        best = payload.get("best") if isinstance(payload, dict) else None
        if not best and isinstance(candidates, list) and candidates:
            best = candidates[0]
        if not isinstance(best, dict) or not best.get("symbol"):
            return False, "Entry blocked: candidate rank unavailable; waiting for next rank refresh"

        best_symbol = _normalize_symbol(str(best.get("symbol")))
        best_score = best.get("score", "-")

        # Skip best candidate if it is structurally unable to enter
        # (e.g. order quantity below minimum for the current account size).
        best_bot = bots.get(best_symbol) if best_symbol else None
        _structural_fail_phrases = (
            "quantity below minimum",
            "below minimum",
            "notional",
            "lot size",
        )
        best_stuck = (
            best_bot is not None
            and best_bot.state.position_cost_basis == 0
            and best_bot.state.active_twap_algo_id is None
            and any(
                phrase in (best_bot.state.last_exit_reason or "").lower()
                for phrase in _structural_fail_phrases
            )
        )
        if best_stuck:
            # Fall through: treat any other ranked pair as eligible
            if normalized != best_symbol:
                return True, (
                    f"Entry allowed: best candidate {best_symbol} is stuck "
                    f"('{best_bot.state.last_exit_reason[:60]}'). "
                    f"{normalized} may enter."
                )

        if normalized == best_symbol:
            return True, f"Entry allowed: {normalized} is best candidate (score {best_score})"
        return False, f"Entry blocked: {normalized} is not best candidate. Best is {best_symbol} (score {best_score})"

    def _ensure_symbol_bot(
        symbol: str,
        auto_start: bool = True,
        persist: bool = True,
        primary_exchange: str | None = None,
    ) -> VolatilityTwapBot:
        nonlocal default_symbol
        requested_symbol = _normalize_symbol(symbol)
        if not requested_symbol:
            raise ValueError("Symbol is required")
        tenant_fleet = _tenant_fleet_for_request()
        if tenant_fleet is not None:
            return tenant_fleet.add_pair(
                requested_symbol,
                auto_start=auto_start,
                persist=persist,
                primary_exchange=primary_exchange,
            )
        created = False
        with bots_lock:
            existing = bots.get(requested_symbol)
            if existing:
                if existing.dry_run != dry_run_mode:
                    existing.dry_run = dry_run_mode
                existing.entry_guard = _entry_rank_guard
                return existing

            pair_root = _bot_runtime_root(requested_symbol)
            pair_root.mkdir(parents=True, exist_ok=True)
            pair_bot = VolatilityTwapBot(pair_root, api_key, api_secret, dry_run=dry_run_mode)
            pair_bot.entry_guard = _entry_rank_guard
            pair_bot.switch_symbol(requested_symbol)
            resolved_symbol = pair_bot.symbol
            if resolved_symbol != requested_symbol:
                LOGGER.info(
                    "Resolved %s to exchange-supported pair %s on %s",
                    requested_symbol,
                    resolved_symbol,
                    pair_bot.primary_exchange.upper(),
                )
            existing = bots.get(resolved_symbol)
            if existing:
                existing.entry_guard = _entry_rank_guard
                return existing
            bots[resolved_symbol] = pair_bot
            created = True
            if not default_symbol:
                default_symbol = resolved_symbol

            if created and persist:
                try:
                    _persist_managed_pairs_state_locked()
                except Exception as exc:
                    LOGGER.warning("Unable to persist managed pairs after add %s: %s", resolved_symbol, exc)

        if auto_start:
            if pair_bot.state.user_requested_stop:
                LOGGER.info("TWAP swing bot %s left stopped by user request", pair_bot.symbol)
            elif pair_bot.state.status in ("waiting", "trading"):
                pair_bot.resume()
            elif pair_bot.state.status in ("idle", "completed", "error"):
                pair_bot.start()
        return pair_bot

    def _remove_symbol_bot(symbol: str) -> tuple[str, str | None]:
        nonlocal default_symbol
        normalized = _normalize_symbol(symbol)
        if not normalized:
            raise ValueError("symbol is required")

        tenant_fleet = _tenant_fleet_for_request()
        if tenant_fleet is not None:
            return tenant_fleet.remove_pair(normalized)

        with bots_lock:
            if normalized not in bots:
                raise ValueError(f"Pair {normalized} is not active")
            bot = bots.pop(normalized)
            if default_symbol == normalized:
                default_symbol = sorted(bots.keys())[0] if bots else ""
            active_symbol = default_symbol or None
            try:
                _persist_managed_pairs_state_locked()
            except Exception as exc:
                LOGGER.warning("Unable to persist managed pairs after remove %s: %s", normalized, exc)

        try:
            if bot.running:
                bot.stop()
        except Exception as exc:
            LOGGER.warning("Unable to fully stop removed pair %s: %s", normalized, exc)

        return normalized, active_symbol

    def _get_bot_from_request(create_if_missing: bool = False, auto_start: bool = False) -> VolatilityTwapBot:
        requested_symbol = _normalize_symbol(request.args.get("symbol") or (request.get_json(silent=True) or {}).get("symbol"))

        tenant_fleet = _tenant_fleet_for_request()
        if tenant_fleet is not None:
            bot = tenant_fleet.get_bot(requested_symbol or None, create_if_missing=create_if_missing)
            if auto_start and not bot.running:
                bot.start()
            return bot

        if requested_symbol:
            with bots_lock:
                bot = bots.get(requested_symbol)
            if bot:
                if bot.dry_run != dry_run_mode:
                    bot.dry_run = dry_run_mode
                return bot
            if create_if_missing:
                return _ensure_symbol_bot(requested_symbol, auto_start=auto_start)
            raise ValueError(f"Pair {requested_symbol} is not active. Add it first.")

        with bots_lock:
            current_default_symbol = default_symbol
            current_default_bot = bots.get(current_default_symbol) if current_default_symbol else None

        if current_default_bot:
            if current_default_bot.dry_run != dry_run_mode:
                current_default_bot.dry_run = dry_run_mode
            return current_default_bot
        if create_if_missing:
            raise ValueError("No active pair. Provide symbol to create a pair.")
        raise ValueError("No active pair. Add a pair first.")

    def _all_pairs() -> list[str]:
        tenant_fleet = _tenant_fleet_for_request()
        if tenant_fleet is not None:
            return tenant_fleet.all_pairs()
        with bots_lock:
            return sorted(bots.keys())

    def _active_default_symbol() -> str:
        tenant_fleet = _tenant_fleet_for_request()
        if tenant_fleet is not None:
            return tenant_fleet.default_symbol
        return default_symbol

    def _bots_snapshot() -> dict[str, VolatilityTwapBot]:
        tenant_fleet = _tenant_fleet_for_request()
        if tenant_fleet is not None:
            return tenant_fleet.bots_snapshot()
        with bots_lock:
            return dict(bots)

    def _status_for_request(bot: VolatilityTwapBot) -> dict[str, Any]:
        status = bot.get_status()
        kill_switch = _kill_switch_state()
        status["platform_entry_kill_switch"] = bool(kill_switch["engaged"])
        status["platform_entry_kill_switch_reason"] = str(kill_switch.get("reason") or "")
        tenant_fleet = _tenant_fleet_for_request()
        if tenant_fleet is not None:
            if not bot.dry_run:
                raise RuntimeError("Customer execution is locked to paper trading")
            status.update(
                {
                    "tenant_isolated": True,
                    "customer_live_enabled": False,
                    "dry_run": True,
                    "execution_mode": "PAPER TRADING",
                }
            )
        return status

    def _auto_discover_pairs_once(force: bool = False) -> list[str]:
        if not auto_discovery_enabled:
            return []
        with bots_lock:
            rank_bot = bots.get(default_symbol) or next(iter(bots.values()), None)
        if rank_bot is None:
            rank_bot = VolatilityTwapBot(root_dir / "runtime" / "bot_discovery", api_key, api_secret, dry_run=dry_run_mode)

        discovery_symbols = rank_bot.get_discovery_symbols(limit=auto_discovery_scan_limit, force=force)
        with bots_lock:
            existing = set(bots.keys())
        remaining_slots = max(0, auto_discovery_max_pairs - len(existing))
        scan_symbols = list(dict.fromkeys(seed_working_pairs + discovery_symbols))
        if remaining_slots <= 0:
            scan_symbols = [symbol for symbol in scan_symbols if symbol in existing or symbol in seed_working_pairs]

        payload = rank_bot.get_pair_candidates(force=force, symbols=scan_symbols, max_seconds=12.0)
        candidates = payload.get("candidates") if isinstance(payload, dict) else []
        added: list[str] = []
        for candidate in candidates if isinstance(candidates, list) else []:
            symbol = _normalize_symbol(str(candidate.get("symbol") or ""))
            if not symbol:
                continue
            if symbol not in seed_working_pairs and remaining_slots <= 0:
                continue
            if not (candidate.get("liquidity_ok") and candidate.get("fee_ok")):
                continue
            if float(candidate.get("score") or 0.0) < auto_discovery_min_score and symbol not in seed_working_pairs:
                continue
            with bots_lock:
                already_managed = symbol in bots
            if already_managed:
                continue
            try:
                _ensure_symbol_bot(symbol, auto_start=True)
                added.append(symbol)
                if symbol not in seed_working_pairs:
                    remaining_slots -= 1
                LOGGER.info("Auto-discovered working pair %s score=%s", symbol, candidate.get("score"))
            except Exception as exc:
                LOGGER.warning("Unable to auto-add discovered pair %s: %s", symbol, exc)
        return added

    def _auto_discovery_loop() -> None:
        while True:
            try:
                added = _auto_discover_pairs_once(force=True)
                if added:
                    LOGGER.info("Auto-discovery added working pairs: %s", ", ".join(added))
            except Exception as exc:
                LOGGER.warning("Auto-discovery cycle failed: %s", exc)
            time.sleep(auto_discovery_interval_seconds)

    loaded_pairs, loaded_default = _load_managed_pairs_state()
    restore_failures: list[str] = []
    if loaded_pairs:
        for pair in loaded_pairs:
            if pair:
                try:
                    _ensure_symbol_bot(pair, auto_start=True, persist=False)
                except Exception as exc:
                    restore_failures.append(pair)
                    LOGGER.warning("Unable to restore managed pair %s: %s", pair, exc)
    elif not managed_pairs_file.exists():
        try:
            _ensure_symbol_bot(primary_symbol, auto_start=True, persist=False)
        except Exception as exc:
            restore_failures.append(primary_symbol)
            LOGGER.warning("Unable to initialize default pair %s: %s", primary_symbol, exc)

    with bots_lock:
        if loaded_default and loaded_default in bots:
            default_symbol = loaded_default
        for pair_bot in bots.values():
            if pair_bot.dry_run != dry_run_mode:
                pair_bot.dry_run = dry_run_mode

    for seed_symbol in seed_working_pairs:
        try:
            _ensure_symbol_bot(seed_symbol, auto_start=True, persist=False)
        except Exception as exc:
            restore_failures.append(seed_symbol)
            LOGGER.warning("Unable to seed working pair %s: %s", seed_symbol, exc)

    with bots_lock:
        if restore_failures:
            LOGGER.warning(
                "Managed-pair startup was incomplete; preserving the existing pair file. Failed: %s",
                ", ".join(dict.fromkeys(restore_failures)),
            )
        else:
            try:
                _persist_managed_pairs_state_locked()
            except Exception as exc:
                LOGGER.warning("Unable to persist managed pairs during startup: %s", exc)

    # Auto-start or resume all restored pair bots
    with bots_lock:
        managed_bots = list(bots.values())
    for managed_bot in managed_bots:
        if managed_bot.state.user_requested_stop:
            LOGGER.info("TWAP swing bot left stopped for %s", managed_bot.symbol)
        elif managed_bot.state.status in ("waiting", "trading"):
            managed_bot.resume()
            LOGGER.info("TWAP swing bot resumed for %s", managed_bot.symbol)
        elif managed_bot.state.status in ("idle", "completed", "error"):
            managed_bot.start()
            LOGGER.info("TWAP swing bot auto-started for %s", managed_bot.symbol)

    if auto_discovery_enabled:
        threading.Thread(target=_auto_discovery_loop, daemon=True, name="bot-auto-discovery").start()

    # Bot endpoints
    @app.get("/bot")
    @login_required
    @require_bot_view_access
    def bot_dashboard() -> str:
        """Serve the bot dashboard page"""
        pairs = _all_pairs()
        requested_symbol = _normalize_symbol(request.args.get("symbol"))
        active_symbol = requested_symbol if requested_symbol in pairs else (_active_default_symbol() or primary_symbol)
        embedded = request.args.get("embedded", "0") == "1"
        fragment = request.args.get("fragment", "0") == "1"
        initial_candidate_payload = None
        initial_status: dict[str, Any] = {}
        initial_bot = None
        try:
            bot = _get_bot_from_request(create_if_missing=False)
            initial_candidate_payload = bot.get_pair_candidates(force=False, symbols=pairs or None, max_seconds=5.0)
        except Exception as exc:
            LOGGER.warning("Unable to pre-render bot candidates: %s", exc)
        try:
            fleet_bots = _bots_snapshot()
            fleet_default = _active_default_symbol()
            initial_bot = (
                fleet_bots.get(active_symbol)
                or (fleet_bots.get(fleet_default) if fleet_default else None)
                or (fleet_bots.get(pairs[0]) if pairs else None)
            )
            if initial_bot is not None:
                initial_status = _status_for_request(initial_bot)
                initial_status["managed_pairs"] = _all_pairs()
        except Exception as exc:
            LOGGER.warning("Unable to pre-render bot status: %s", exc)
        return render_template(
            "bot_dashboard.html",
            initial_pairs=pairs,
            initial_active_symbol=active_symbol,
            initial_candidate_payload=initial_candidate_payload,
            initial_status=initial_status,
            supported_symbols=(initial_bot.get_discovery_symbols(limit=100) if initial_bot else list(VolatilityTwapBot.supported_symbols)),
            embedded=embedded,
            fragment=fragment,
        )

    @app.get("/api/bot/status")
    @login_required
    @require_bot_view_access
    def bot_status() -> dict[str, object]:
        """Get bot status as JSON"""
        try:
            bot = _get_bot_from_request(create_if_missing=False)
            status = _status_for_request(bot)
            status["managed_pairs"] = _all_pairs()
            return jsonify(status)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.get("/api/bot/candidates")
    @login_required
    @require_bot_view_access
    def bot_candidates() -> dict[str, object]:
        """Rank supported pairs and return the best current TWAP candidate"""
        try:
            force = request.args.get("refresh") == "1"
            managed_pairs = _all_pairs()
            bot = _get_bot_from_request(create_if_missing=False)
            payload = bot.get_pair_candidates(
                force=force,
                symbols=managed_pairs or None,
                max_seconds=10.0 if force else 6.0,
            )
            if managed_pairs:
                payload = {**payload, "scope": "managed_pairs", "managed_pairs": managed_pairs}
            if payload.get("candidates"):
                return jsonify(payload)

            if managed_pairs:
                candidates = []
                fleet_bots = _bots_snapshot()
                for symbol in managed_pairs:
                    item = fleet_bots.get(symbol)
                    if item is None:
                        continue
                    profit = float(item.state.current_profit_usdt)
                    candidates.append(
                        {
                            "symbol": symbol,
                            "score": 0.0,
                            "reason": f"Ranking temporarily unavailable; live P&L {profit:+.3f} USDT",
                            "liquidity_ok": True,
                            "fee_ok": True,
                        }
                    )
                candidates.sort(key=lambda row: row.get("score", 0.0), reverse=True)
                payload = {
                    "generated_at": datetime.now(UTC).timestamp(),
                    "selected_symbol": bot.symbol,
                    "scope": "managed_pairs_fallback",
                    "managed_pairs": managed_pairs,
                    "best": candidates[0] if candidates else None,
                    "candidates": candidates,
                }
                return jsonify(payload)
            return jsonify(payload)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.get("/api/bot/chart")
    @login_required
    @require_bot_view_access
    def bot_chart() -> dict[str, object]:
        """Return candle data for the selected bot pair chart."""
        try:
            requested_symbol = _normalize_symbol(request.args.get("symbol"))
            interval = request.args.get("interval", "15m")
            try:
                limit = int(request.args.get("limit", "96"))
            except ValueError:
                limit = 96
            bot = _get_bot_from_request(create_if_missing=False)
            symbol = requested_symbol or bot.symbol
            candles = bot.get_price_candles(symbol=symbol, interval=interval, limit=limit, timeout_seconds=8)
            return jsonify({"symbol": symbol, "interval": interval if interval else "15m", "candles": candles})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.get("/api/bot/trades")
    @login_required
    @require_bot_view_access
    def bot_trades() -> dict[str, object]:
        """Return recent account fills for the selected bot pair."""
        try:
            requested_symbol = _normalize_symbol(request.args.get("symbol"))
            try:
                hours = float(request.args.get("hours", "1"))
            except ValueError:
                hours = 1.0
            bot = _get_bot_from_request(create_if_missing=False)
            symbol = requested_symbol or bot.symbol
            trades = bot.get_recent_trades(symbol=symbol, hours=hours, limit=1000)
            quote_asset = bot._quote_asset_for_symbol(symbol)
            base_asset = symbol[:-len(quote_asset)] if symbol.endswith(quote_asset) else bot._base_asset()
            buy_quote = sum(float(item.get("quote_qty") or 0) for item in trades if item.get("side") == "BUY")
            sell_quote = sum(float(item.get("quote_qty") or 0) for item in trades if item.get("side") == "SELL")
            fee_quote_estimate = 0.0
            fees_by_asset: dict[str, float] = {}
            for item in trades:
                fee = float(item.get("commission") or 0)
                fee_asset = str(item.get("commission_asset") or "").upper()
                if fee <= 0 or not fee_asset:
                    continue
                fees_by_asset[fee_asset] = fees_by_asset.get(fee_asset, 0.0) + fee
                if fee_asset == quote_asset:
                    fee_quote_estimate += fee
                elif fee_asset == base_asset:
                    fee_quote_estimate += fee * float(item.get("price") or 0)
            net_quote_flow = sell_quote - buy_quote - fee_quote_estimate
            return jsonify({
                "symbol": symbol,
                "hours": max(0.1, min(24.0, hours)),
                "count": len(trades),
                "buy_quote": round(buy_quote, 8),
                "sell_quote": round(sell_quote, 8),
                "fee_quote_estimate": round(fee_quote_estimate, 8),
                "fees_by_asset": {asset: round(amount, 10) for asset, amount in sorted(fees_by_asset.items())},
                "net_quote_flow": round(net_quote_flow, 8),
                "trades": trades,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.get("/api/account/summary")
    @login_required
    @require_bot_view_access
    def account_summary() -> dict[str, object]:
        """Return total spot balance (USDT+FDUSD) and today's net profit across all pairs."""
        try:
            fleet_bots = _bots_snapshot()
            any_bot = next(iter(fleet_bots.values()), None)
            if not any_bot:
                return jsonify({
                    "spot_balance_usdt": 0.0,
                    "today_profit_usdt": 0.0,
                    "simulated": _tenant_fleet_for_request() is not None,
                })

            tenant_fleet = _tenant_fleet_for_request()
            if tenant_fleet is not None:
                paper_equity = 0.0
                paper_pnl = 0.0
                for pair_bot in fleet_bots.values():
                    current_price = float(pair_bot.state.current_price or 0)
                    position_value = float(pair_bot.state.position_qty) * current_price
                    if position_value <= 0 and pair_bot.state.position_cost_basis > 0:
                        position_value = float(pair_bot.state.position_cost_basis)
                    paper_equity += float(pair_bot.state.budget_remaining) + position_value
                    paper_pnl += float(pair_bot.state.current_profit_usdt)
                return jsonify({
                    "spot_balance_usdt": round(paper_equity, 2),
                    "today_profit_usdt": round(paper_pnl, 2),
                    "simulated": True,
                    "balance_label": "Paper Equity",
                    "profit_label": "Open Paper P&L",
                })

            # Spot balance: USDT + FDUSD (free + locked)
            account = any_bot._send_signed_request("GET", "/api/v3/account", {})
            stable_assets = {"USDT", "FDUSD"}
            spot_balance = 0.0
            for bal in account.get("balances", []):
                if bal.get("asset") in stable_assets:
                    spot_balance += float(bal.get("free", 0)) + float(bal.get("locked", 0))

            # Today's profit: net (sell - buy - fees) across all managed pairs since midnight UTC
            import time as _time
            now = _time.time()
            hours_since_midnight = (now % 86400) / 3600.0
            if hours_since_midnight < 0.05:
                hours_since_midnight = 0.05

            today_profit = 0.0
            managed = _all_pairs()
            for symbol in managed:
                pair_bot = fleet_bots.get(symbol)
                if not pair_bot:
                    continue
                try:
                    trades = pair_bot.get_recent_trades(symbol=symbol, hours=hours_since_midnight, limit=1000)
                    quote_asset = pair_bot._quote_asset_for_symbol(symbol)
                    base_asset = symbol[:-len(quote_asset)] if symbol.endswith(quote_asset) else pair_bot._base_asset()
                    buy_q = sum(float(t.get("quote_qty", 0)) for t in trades if t.get("side") == "BUY")
                    sell_q = sum(float(t.get("quote_qty", 0)) for t in trades if t.get("side") == "SELL")
                    fee_q = 0.0
                    for t in trades:
                        fee = float(t.get("commission", 0) or 0)
                        if fee <= 0:
                            continue
                        fa = str(t.get("commission_asset") or "").upper()
                        if fa == quote_asset:
                            fee_q += fee
                        elif fa == base_asset:
                            fee_q += fee * float(t.get("price", 0) or 0)
                    today_profit += sell_q - buy_q - fee_q
                except Exception:
                    pass

            return jsonify({
                "spot_balance_usdt": round(spot_balance, 2),
                "today_profit_usdt": round(today_profit, 2),
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    # Codex CLI authentication and activity (routes via daemon running as the correct user).
    _auth_req_path = root_dir / "runtime" / "codex_requests" / "_auth.json"
    _auth_resp_path = root_dir / "runtime" / "codex_responses" / "_auth.json"
    _codex_requests_dir = root_dir / "runtime" / "codex_requests"
    _codex_responses_dir = root_dir / "runtime" / "codex_responses"
    _daemon_log_path = root_dir / "runtime" / "codex-daemon.log"
    _codex_control_path = root_dir / "runtime" / "codex-control.json"
    _codex_allowed_models = (
        "gpt-5.4-mini",
        "gpt-5.4",
        "gpt-5.5",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.3-codex-spark",
    )

    def _read_codex_control() -> dict[str, object]:
        default_model = os.environ.get("CODEX_CLI_MODEL", "gpt-5.4-mini")
        control: dict[str, object] = {
            "enabled": _env_flag("CODEX_CLI_ENABLED", True),
            "model": default_model if default_model in _codex_allowed_models else "gpt-5.4-mini",
            "reasoning_effort": os.environ.get("CODEX_CLI_REASONING_EFFORT", "low"),
        }
        try:
            data = json.loads(_codex_control_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if isinstance(data.get("enabled"), bool):
                    control["enabled"] = data["enabled"]
                if data.get("model") in _codex_allowed_models:
                    control["model"] = data["model"]
                if data.get("reasoning_effort") in {"minimal", "low", "medium", "high"}:
                    control["reasoning_effort"] = data["reasoning_effort"]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        control["allowed_models"] = list(_codex_allowed_models)
        return control

    @app.get("/api/codex/activity")
    @login_required
    @owner_required
    def codex_activity():
        """Return recent daemon log lines and per-symbol decision summaries."""
        # -- Daemon log tail (last 80 lines) --
        log_lines: list[str] = []
        try:
            if _daemon_log_path.exists():
                raw = _daemon_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                log_lines = raw[-80:]
        except Exception:
            pass

        # -- Per-symbol response summaries --
        symbols: list[dict] = []
        now = time.time()
        try:
            for resp_file in sorted(_codex_responses_dir.glob("*.json")):
                sym = resp_file.stem
                if sym == "_auth":
                    continue
                try:
                    d = json.loads(resp_file.read_text(encoding="utf-8"))
                    responded_at = float(d.get("responded_at", 0))
                    age_s = int(now - responded_at) if responded_at else None
                    payload = d.get("payload") or {}
                    symbols.append({
                        "symbol": sym.upper(),
                        "status": d.get("status", "unknown"),
                        "human_summary": d.get("human_summary"),
                        "output_preview": d.get("output_preview"),
                        "error_preview": d.get("error_preview"),
                        "returncode": d.get("returncode"),
                        "age_s": age_s,
                        "responded_at": responded_at,
                        "action": payload.get("action"),
                        "confidence": payload.get("confidence"),
                        "reason": (payload.get("reason") or "")[:200],
                        "regime": payload.get("regime"),
                        "strategy_mode": payload.get("strategy_mode"),
                        "stop_loss_pct": payload.get("stop_loss_pct"),
                        "target_pct": payload.get("target_pct"),
                    })
                except Exception:
                    pass
        except Exception:
            pass

        symbols.sort(key=lambda row: float(row.get("responded_at") or 0), reverse=True)

        # -- Pending request timestamps --
        pending: dict[str, float] = {}
        try:
            for req_file in _codex_requests_dir.glob("*.json"):
                sym = req_file.stem
                if sym.startswith("_"):
                    continue
                try:
                    d = json.loads(req_file.read_text(encoding="utf-8"))
                    pending[sym.upper()] = float(d.get("requested_at", 0))
                except Exception:
                    pass
        except Exception:
            pass

        return jsonify({
            "log_lines": log_lines,
            "symbols": symbols,
            "pending": pending,
            "ts": now,
        })

    @app.get("/api/codex/control")
    @login_required
    @owner_required
    def codex_control_get():
        return jsonify(_read_codex_control())

    @app.post("/api/codex/control")
    @login_required
    @owner_required
    def codex_control_set():
        data = request.get_json(silent=True) or {}
        model = str(data.get("model") or "").strip()
        if model not in _codex_allowed_models:
            return jsonify({"error": "Unsupported Codex model", "allowed_models": list(_codex_allowed_models)}), 400
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({"error": "enabled must be true or false"}), 400
        effort = str(data.get("reasoning_effort") or "low").strip().lower()
        if effort not in {"minimal", "low", "medium", "high"}:
            return jsonify({"error": "Unsupported reasoning effort"}), 400
        _codex_control_path.parent.mkdir(parents=True, exist_ok=True)
        _codex_control_path.write_text(json.dumps({"enabled": enabled, "model": model, "reasoning_effort": effort, "updated_at": time.time()}, indent=2), encoding="utf-8")
        return jsonify({"ok": True, **_read_codex_control()})

    @app.post("/api/codex/run")
    @login_required
    @owner_required
    def codex_run_now():
        """Queue one owner-requested decision; the daemon still enforces its hourly budget."""
        control = _read_codex_control()
        if control.get("enabled") is not True:
            return jsonify({"error": "Enable Run Codex CLI first"}), 400
        try:
            bot = _get_bot_from_request(create_if_missing=False)
            requested_at = time.time()
            symbol = bot.symbol.lower()
            request_data = {
                "symbol": symbol,
                "requested_at": requested_at,
                "codex_eligible": True,
                "priority": 3,
                "snapshot": {
                    "symbol": bot.symbol,
                    "status": bot.state.status,
                    "current_price": float(bot.state.current_price),
                    "current_rsi": float(bot.state.current_rsi),
                    "current_volatility": float(bot.state.current_volatility),
                    "market_sentiment": float(bot.state.market_sentiment),
                    "media_sentiment_score": float(bot.state.media_sentiment_score),
                    "position_qty": float(bot.state.position_qty),
                    "position_cost_basis": float(bot.state.position_cost_basis),
                    "current_profit_usdt": float(bot.state.current_profit_usdt),
                },
                "baseline": {
                    "target_profit_usdt": float(bot.state.target_profit_usdt),
                    "target_pct": float(bot.state.target_profit_usdt / bot.state.position_cost_basis) if bot.state.position_cost_basis > 0 else 0.003,
                    "regime": bot.state.adaptive_strategy_mode,
                    "confidence": 60.0,
                    "dca_levels": bot.state.adaptive_dca_levels,
                    "dca_min_step_pct": float(bot.state.adaptive_dca_min_step_pct),
                    "dca_max_step_pct": float(bot.state.adaptive_dca_max_step_pct),
                    "dca_reprice_interval_seconds": bot.state.adaptive_dca_reprice_interval_seconds,
                    "max_entry_balance_fraction": float(bot.state.adaptive_max_entry_balance_fraction),
                },
            }
            req_path = _codex_requests_dir / f"{symbol}.json"
            req_path.parent.mkdir(parents=True, exist_ok=True)
            req_path.write_text(json.dumps(request_data), encoding="utf-8")
            return jsonify({"ok": True, "message": f"Codex CLI run queued for {bot.symbol}.", "symbol": bot.symbol, "model": control.get("model")})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/codex/reauth")
    @login_required
    @owner_required
    def codex_auth_start():
        """Write an auth trigger file for the daemon to pick up."""
        try:
            _auth_req_path.parent.mkdir(parents=True, exist_ok=True)
            _auth_resp_path.parent.mkdir(parents=True, exist_ok=True)
            # Remove stale response file if it exists and is writable by this process
            try:
                _auth_resp_path.unlink(missing_ok=True)
            except OSError:
                pass
            _auth_req_path.write_text(
                json.dumps({"type": "auth", "force_reauth": True, "requested_at": time.time()}),
                encoding="utf-8",
            )
            return jsonify({"ok": True, "message": "Auth request queued for daemon"})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/codex/reauth/status")
    @login_required
    @owner_required
    def codex_auth_poll():
        """Return the current auth response written by the daemon."""
        try:
            if not _auth_resp_path.exists():
                return jsonify({"status": "pending", "lines": [], "done": False})
            data = json.loads(_auth_resp_path.read_text(encoding="utf-8"))
            done = data.get("status") in ("done", "error")
            return jsonify({
                "lines": [{"line": l, "src": "out"} for l in data.get("lines", [])],
                "done": done,
                "exit_code": data.get("exit_code"),
                "status": data.get("status", "pending"),
                "error": data.get("error"),
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/codex/reauth/abort")
    @login_required
    @owner_required
    def codex_auth_cancel():
        """Remove the pending auth request file."""
        try:
            _auth_req_path.unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({"ok": True})


    @app.get("/api/bot/pairs")
    @login_required
    @require_bot_view_access
    def bot_pairs() -> dict[str, object]:
        fleet_bots = _bots_snapshot()
        payload = {
            "pairs": [
                {
                    "symbol": symbol,
                    "status": item.state.status,
                    "running": item.running,
                    "current_profit_usdt": float(item.state.current_profit_usdt),
                    "position_cost_basis": float(item.state.position_cost_basis),
                }
                for symbol, item in sorted(fleet_bots.items())
            ]
        }
        return jsonify(payload)

    @app.post("/api/bot/pairs")
    @login_required
    @subscription_required_for_new_trades
    @require_bot_action_auth
    def bot_add_pair() -> dict[str, object]:
        try:
            data = request.get_json() or {}
            symbol = _normalize_symbol(data.get("symbol"))
            if not symbol:
                return jsonify({"error": "symbol is required"}), 400
            bot = _ensure_symbol_bot(
                symbol,
                auto_start=False,
                primary_exchange=str(data.get("exchange") or "").strip().lower() or None,
            )
            resolved_symbol = bot.symbol
            if bot.running:
                message = f"{resolved_symbol} already running in {'paper' if bot.dry_run else 'live'} mode"
            elif bot.state.status in ("waiting", "trading") and not bot.state.user_requested_stop:
                message = bot.resume()
            else:
                message = bot.start()
            status = _status_for_request(bot)
            status["managed_pairs"] = _all_pairs()
            return jsonify({"message": message, "resolved_symbol": resolved_symbol, "status": status})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/bot/pairs", methods=["DELETE"])
    @app.post("/api/bot/pairs/remove")
    @login_required
    @require_bot_action_auth
    def bot_remove_pair() -> dict[str, object]:
        try:
            data = request.get_json(silent=True) or {}
            symbol = _normalize_symbol(data.get("symbol") or request.args.get("symbol"))
            LOGGER.warning(
                "Pair removal requested: symbol=%s remote=%s host=%s user=%s",
                symbol,
                request.remote_addr,
                request.host,
                request.headers.get("Cf-Access-Authenticated-User-Email", ""),
            )
            removed_symbol, active_symbol = _remove_symbol_bot(symbol)
            return jsonify({
                "message": f"{removed_symbol} removed",
                "removed_symbol": removed_symbol,
                "active_symbol": active_symbol,
                "managed_pairs": _all_pairs(),
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/api/bot/symbol")
    @login_required
    @subscription_required_for_new_trades
    @require_bot_action_auth
    def bot_symbol() -> dict[str, object]:
        """Backward-compatible endpoint: ensure a pair bot is active"""
        try:
            data = request.get_json() or {}
            symbol = _normalize_symbol(data.get("symbol"))
            bot = _ensure_symbol_bot(
                symbol,
                auto_start=False,
                primary_exchange=str(data.get("exchange") or "").strip().lower() or None,
            )
            resolved_symbol = bot.symbol
            if bot.running:
                message = f"{resolved_symbol} already running in {'paper' if bot.dry_run else 'live'} mode"
            elif bot.state.status in ("waiting", "trading") and not bot.state.user_requested_stop:
                message = bot.resume()
            else:
                message = bot.start()
            status = _status_for_request(bot)
            status["managed_pairs"] = _all_pairs()
            return jsonify({"message": message, "resolved_symbol": resolved_symbol, "status": status})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/api/bot/target")
    @login_required
    @require_bot_action_auth
    def bot_target() -> dict[str, object]:
        """Set the active profit target"""
        try:
            data = request.get_json() or {}
            bot = _get_bot_from_request(create_if_missing=False)
            if data.get("use_suggestion"):
                target = bot.state.target_profit_suggested_usdt
                source = "applied_suggestion"
            else:
                target = data.get("target_profit_usdt")
                source = "manual"
            message = bot.set_target_profit(target, source=source)
            status = _status_for_request(bot)
            status["managed_pairs"] = _all_pairs()
            return jsonify({"message": message, "status": status})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/api/bot/close-all")
    @login_required
    @require_bot_action_auth
    def bot_close_all() -> dict[str, object]:
        """Cancel active bot orders and sell spot positions to USDT"""
        try:
            bot = _get_bot_from_request(create_if_missing=False)
            message = bot.close_all_positions_to_usdt()
            status = _status_for_request(bot)
            status["managed_pairs"] = _all_pairs()
            return jsonify({"message": message, "status": status})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/api/bot/close-pair")
    @login_required
    @require_bot_action_auth
    def bot_close_pair() -> dict[str, object]:
        """Cancel selected pair orders and sell only that pair's base asset to its quote asset."""
        try:
            bot = _get_bot_from_request(create_if_missing=False)
            message = bot.close_pair_position_to_quote()
            status = _status_for_request(bot)
            status["managed_pairs"] = _all_pairs()
            return jsonify({"message": message, "status": status})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/api/bot/start")
    @login_required
    @subscription_required_for_new_trades
    @require_bot_action_auth
    def bot_start() -> dict[str, object]:
        """Start the bot"""
        try:
            data = request.get_json() or {}
            budget = float(data.get("budget", 5000))
            duration_hours = float(data.get("duration_hours", 12))
            bot = _get_bot_from_request(create_if_missing=True, auto_start=False)
            strategy_mode = data.get("strategy_mode")
            twap_switch_budget = data.get("twap_switch_budget_usdt")
            force_start = str(data.get("force_start", "")).strip().lower() in {"1", "true", "yes", "on"}
            if strategy_mode is not None and hasattr(bot, "set_strategy_mode"):
                bot.set_strategy_mode(strategy_mode, twap_switch_budget)
            override_note = ""
            if force_start and hasattr(bot, "request_manual_start_override"):
                override_note = str(bot.request_manual_start_override())

            if bot.running:
                rearm_note = ""
                if force_start and hasattr(bot, "rearm_running_start"):
                    rearm_note = str(bot.rearm_running_start(budget, duration_hours))
                status = _status_for_request(bot)
                status["managed_pairs"] = _all_pairs()
                wait_reason = str(status.get("last_exit_reason") or "").strip()
                message_parts = ["Bot loop is already running"]
                if rearm_note:
                    message_parts.append(rearm_note)
                if override_note:
                    message_parts.append(override_note)
                if wait_reason:
                    message_parts.append(f"current wait reason: {wait_reason}")
                return jsonify({"message": "; ".join(message_parts), "status": status})

            message = bot.start(budget, duration_hours)
            if override_note:
                message = f"{message}; {override_note}"
            status = _status_for_request(bot)
            status["managed_pairs"] = _all_pairs()
            return jsonify({"message": message, "status": status})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/api/bot/strategy")
    @login_required
    @require_bot_action_auth
    def bot_strategy() -> dict[str, object]:
        """Set strategy mode for the selected pair bot"""
        try:
            data = request.get_json() or {}
            bot = _get_bot_from_request(create_if_missing=False)
            strategy_mode = data.get("strategy_mode")
            if strategy_mode is None:
                return jsonify({"error": "strategy_mode is required"}), 400
            twap_switch_budget = data.get("twap_switch_budget_usdt")
            if not hasattr(bot, "set_strategy_mode"):
                return jsonify({"error": "Strategy mode is not supported for this bot"}), 400
            message = bot.set_strategy_mode(strategy_mode, twap_switch_budget)
            status = _status_for_request(bot)
            status["managed_pairs"] = _all_pairs()
            return jsonify({"message": message, "status": status})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/api/bot/exchange")
    @login_required
    @require_bot_action_auth
    def bot_exchange() -> dict[str, object]:
        """Change the primary spot exchange when the selected bot is idle."""
        try:
            data = request.get_json() or {}
            exchange = data.get("exchange")
            tenant_fleet = _tenant_fleet_for_request()
            requested_symbol = _normalize_symbol(data.get("symbol"))
            if tenant_fleet is not None:
                message = tenant_fleet.set_primary_exchange(exchange, requested_symbol or None)
                if requested_symbol:
                    bot = tenant_fleet.get_bot(requested_symbol)
                    status = _status_for_request(bot)
                    status["managed_pairs"] = _all_pairs()
                    return jsonify({"message": message, "status": status})
                return jsonify({
                    "message": message,
                    "status": None,
                    "primary_exchange": tenant_fleet.primary_exchange,
                    "supported_instruments": tenant_fleet.supported_instruments(limit=100),
                })
            bot = _get_bot_from_request(create_if_missing=False)
            if not hasattr(bot, "set_primary_exchange"):
                return jsonify({"error": "Exchange switching is not supported for this bot"}), 400
            message = bot.set_primary_exchange(exchange)
            status = _status_for_request(bot)
            status["managed_pairs"] = _all_pairs()
            return jsonify({"message": message, "status": status})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/api/bot/stop")
    @login_required
    @require_bot_action_auth
    def bot_stop() -> dict[str, object]:
        """Stop the bot"""
        try:
            bot = _get_bot_from_request(create_if_missing=False)
            message = bot.stop()
            status = _status_for_request(bot)
            status["managed_pairs"] = _all_pairs()
            return jsonify({"message": message, "status": status})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/api/bot/restart")
    @login_required
    @subscription_required_for_new_trades
    @require_bot_action_auth
    def bot_restart() -> dict[str, object]:
        """Restart the bot"""
        try:
            bot = _get_bot_from_request(create_if_missing=False)
            message = bot.restart()
            status = _status_for_request(bot)
            status["managed_pairs"] = _all_pairs()
            return jsonify({"message": message, "status": status})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    return app
