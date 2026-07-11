# BitRucker Market Readiness and Competitive Plan

Updated: 2026-07-11

## Product position

BitRucker should not compete by claiming guaranteed profit or by having the largest list of bots. The defensible position is:

> An explainable, safety-gated multi-asset trading agent that shows why it acts, calculates fee/liquidity risk, distinguishes crypto from tokenized equities, and refuses unsafe execution.

“Foolproof trading” does not exist. The product promise should be controlled automation, transparent decisions, and recoverable operations—not guaranteed returns.

## Current competitor benchmark

| Product | Current table stakes | Commercial signal | What BitRucker must learn |
|---|---|---|---|
| [3Commas](https://3commas.io/pricing) | DCA, Grid, Signal, SmartTrade, demo account, webhooks, Pine Script execution, deep backtesting, 15+ exchanges | $20/$50/$140 monthly tiers | Backtesting, strategy templates, multi-account limits, and professional API/security controls are expected. |
| [Bitsgap](https://bitsgap.com/pricing) | Grid/DCA/COMBO bots, AI assistant, portfolio mode, trailing controls, demo bots, 30–365 day backtests | Free demo plus paid tiers | A guided AI setup must be paired with measurable backtests and strong portfolio UX. |
| [Cryptohopper](https://www.cryptohopper.com/features/all-features) | Paper and live bots, strategy designer, marketplace, copy trading, AI, dynamic stops, market making and arbitrage | Subscription per live + paper bot | Beginner onboarding, reusable templates, signals, and a community ecosystem materially reduce adoption friction. |
| [Coinrule](https://cloud.coinrule.com/) | Plain-English/Python/Pine strategy input, historical replay, paper/live agents, multi-market execution, AES-256 key protection | $9.99/$49.99/$975 tiers | Natural-language strategy creation is useful only when users can inspect, edit, validate, and backtest the resulting rules. |
| [Pionex](https://www.pionex.com/en/bot) | Exchange-native Grid, DCA, Rebalancing, Smart Trade and other bots; low-friction setup; tokenized-stock bot coverage | No separate bot subscription; exchange fees | Exchange-native competitors win on setup and cost. An external agent must win on cross-venue control, transparency, and safety. |

Competitors also emphasize trade-only keys, IP restrictions, paper trading, mobile access, alerts, support, and historical analytics. 3Commas now describes isolated signing infrastructure and API permission checks on its [security page](https://3commas.io/security), showing how high the credential-security bar has become.

## BitRucker strengths already present

- Exchange-supported symbol resolution instead of assuming Binance naming.
- Bitget crypto, commodity/metal, and Reality tokenized-stock classification from instrument metadata.
- Plain stock ticker aliases such as `NVDA` mapping to the exchange’s canonical Reality symbol.
- Stock-specific news context; crypto Fear & Greed and futures-liquidation data are excluded from stock decisions.
- Liquidity, spread, depth, fee, RSI, volatility, media, and liquidation-aware candidate ranking.
- Position cost-basis reconstruction and open-order reconciliation.
- Daily drawdown, allocation, loss, target, and software-stop controls.
- Explainable advisor output and rate-limited Codex analysis.
- Security headers, CSRF-protected bot actions, durable audit events, login throttling, versioned legal acceptance, and a fail-closed launch probe.
- Tenant-isolated customer paper fleets with separate credentials, runtime state, pair registries, and explicit prevention of server-key fallback.
- TOTP MFA with one-time recovery codes and step-up protection for trading actions.
- Encrypted Binance/Bitget credential vault with permission and IP-allowlist validation before storage.
- Audited, durable, fail-closed emergency entry lock that preserves stop/close and protective exits.
- Honest public product page with open signup gated off until recorded legal approval; beta access is invite-only.
- Billing no longer grants a new subscription merely because an old wallet balance remains visible.

## Critical launch blockers

The live endpoint `/health/ready` is the source of truth. Public launch must remain blocked while any critical item fails.

1. **Independent tenant-boundary review** — customer paper state is isolated and HTTP-tested, but customer live execution remains disabled until an external penetration test and dedicated per-tenant execution worker are complete.
2. **MFA enrollment and account recovery** — TOTP and one-time recovery codes are implemented, but every owner/admin must enroll; verified email, secure password reset, and WebAuthn are still required.
3. **Production API-key remediation** — validation now rejects withdrawal/transfer-enabled or unrestricted keys. Existing connections must be rotated/revalidated; the currently audited Bitget key still has transfer authority and no IP allowlist.
4. **Legal/regulatory approval** — determine launch entity, customer countries, licensing perimeter, financial-promotion rules, tokenized-security restrictions, privacy obligations, and sanctions/KYC requirements with qualified counsel. Do not infer that “software only” is automatically exempt.
5. **Backups and disaster recovery** — encrypted off-host backups, tested restore, documented RPO/RTO, secret recovery, and emergency exchange procedures.

## Required product roadmap

### P0 — safe private beta

- Promote the isolated paper fleets to dedicated tenant workers and move configuration/audit data from SQLite to managed PostgreSQL.
- Durable job queue with per-exchange rate limiting, idempotency keys, retries, dead-letter handling, and leader election.
- Immutable order/fill ledger; startup reconciliation must prove local state against the exchange before any write.
- Extend the implemented exchange connection validator with key-rotation alerts and periodic revalidation.
- Keep the implemented tenant paper-only lock. Future live activation requires a preflight checklist, risk acknowledgement, maximum allocation, daily loss cap, and step-up authentication.
- Extend the implemented global entry kill switch with tenant live switches, stale-data, exchange-outage, excessive-slippage, abnormal-order-rate, and drawdown circuit breakers.
- Central error monitoring, uptime probes, alert routing, status page, incident runbooks, and customer-visible maintenance state.
- Production payment provider with signed webhooks, idempotency, unique transaction verification, refunds, taxes/invoices, and reconciliation.
- Legal counsel sign-off, jurisdiction gating, complete operator identity, support/SLA policy, and privacy subprocessor list.

### P1 — competitive public product

- Fee-, spread-, slippage-, latency-, and partial-fill-aware backtesting with train/test splits and walk-forward reports.
- Require a minimum measured observation period in the implemented per-user paper/shadow mode before any future live promotion.
- Visual/no-code strategy builder plus inspectable plain-English-to-rule conversion; never let an LLM directly emit an unchecked order.
- Strategy templates for DCA, Grid, TWAP, mean reversion, momentum, rebalance, signal/webhook, and smart exit management.
- Signed TradingView/provider webhooks with nonce/timestamp validation, replay protection, per-user secrets, dry-run preview, and idempotency.
- Net performance analytics: realized/unrealized P&L, fees, spread/slippage, benchmark, drawdown, exposure, win/loss, tax lots, and CSV/API export.
- Installable PWA, push/email/Telegram alerts, emergency pause/close controls, accessibility testing, and mobile-first onboarding.
- Contextual explanations: show data freshness, source, confidence, conflicting signals, exact risk budget, and why a trade was blocked.

### P2 — defensible expansion

- Additional exchanges through a tested capability interface rather than exchange-specific conditionals throughout strategy code.
- Portfolio-level allocation, correlation limits, rebalance policies, and cross-bot capital reservation.
- User-created strategy versioning, approval workflow, canary rollout, rollback, and reproducible performance history.
- Team accounts, RBAC, read-only auditor seats, API access, and enterprise key-management/HSM integration.
- Marketplace/copy trading only after legal review, performance-verification standards, conflict disclosures, and anti-manipulation controls.

## Product rules that should never be relaxed

- Never claim the bot is guaranteed, foolproof, risk-free, or able to manipulate/beat an exchange.
- Never treat a chart move as account profit; show unrealized P&L, realized P&L, fees, spread, and locked/open-order balances separately.
- Never send an order for an instrument before loading current exchange metadata and validating the account capability.
- Never cancel a protective or target order unless replacement capability is known or the user explicitly confirms the exposure.
- Never enable live trading from an LLM response alone. AI may recommend; deterministic policy and risk controls authorize.
- Never store or accept withdrawal-enabled exchange credentials.
- Never advertise a tokenized stock as direct ownership of the underlying share.
- Never auto-credit a payment from a reusable wallet balance without a unique, confirmed, idempotent transaction.

## Verification required before launch

- Unit, integration, contract, restart-recovery, browser, load, penetration, dependency, and disaster-recovery tests in CI.
- Exchange sandbox/testnet tests plus low-value production canaries for every supported order lifecycle.
- Independent security review of authentication, credential storage/signing, tenant boundaries, billing webhooks, and admin controls.
- Legal and privacy review for each customer jurisdiction and each promoted asset class.
- A 30-day controlled beta with incident metrics, reconciliation accuracy, order error rate, support load, churn, and net-of-fee strategy reporting.
