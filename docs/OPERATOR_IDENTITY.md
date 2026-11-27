# Managed operator identities

Local development and isolated preview retain the existing configured demo login. Production now requires PostgreSQL and MANAGED_IDENTITY=true; the old shared operator login cannot be used in production. Apply migrations before starting. Account creation is performed by a trusted deployment administrator using an interactive terminal (no public account registration).

    docker compose exec api python -m scripts.manage_operators create alice --role admin
    docker compose exec api python -m scripts.manage_operators create bob --role support
    docker compose exec api python -m scripts.manage_operators create carol --role reviewer
    docker compose exec api python -m scripts.manage_operators create dana --role auditor

Use unique strong passwords. Names are stable audit identifiers and cannot be reused. The CLI supports password, role, disable, enable and revoke; all account changes revoke existing sessions. Logout revokes the server-side session. Sessions expire after eight hours; tokens are random and only SHA-256 digests are stored. Passwords are stored using salted PBKDF2-HMAC-SHA256 (600,000 iterations). Login failure budgets are persisted and serialized across PostgreSQL processes; the local preview uses its existing behavior. A legitimate user may be temporarily blocked after repeated failures; investigate abuse before changing limits. Deploy a gateway with request timeouts, global and per-client traffic limits and trusted proxy configuration; these database-backed budgets are not a complete edge protection solution.

Roles: admin can perform every existing operation; support can create inquiries and retry support triage; reviewer can approve/reject, refresh, reconcile and retry approved actions; auditor is read-only. All four can view single-store operational data. No tenant isolation or field-level privacy segmentation is claimed. Role changes take effect on subsequent requests; already-running requests are not cancelled. The deployment CLI is privileged and its events identify deployment_admin, not a verified OS account. Use separately controlled deployment access and audit logs. MFA/SSO, independent review and managed identity-provider integration remain production gates if required by the merchant.

For production use COOKIE_SECURE=true, MANAGED_IDENTITY=true, PostgreSQL, unique secrets and protected HTTPS ingress. A configured Shopify secret also requires a store domain allowlist. External AI in production requires ALLOW_EXTERNAL_AI=true after the data-processing review; mock remains the default. Never expose the n8n editor or internal API publicly.

The existing approval actors and historical records are preserved. New human support intake records the verified operator username; integration intake identifies the shared integration principal, not an individual. Tests cover unauthorized role mutations, logout cookie replay, role revocation, expiry and persistent login budgets. Real PostgreSQL concurrent login, manual role-based browser evaluation, MFA/SSO and edge controls remain separate verification requirements.
