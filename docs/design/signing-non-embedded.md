# Signing for Non-Embedded Mode

**Status:** Draft, ready to scaffold
**Author:** Brian O'Kelley
**Last updated:** 2026-05-05 (rev 4: operator-trust model; brand.json on operator domain; bearer pins operator)
**Companion to:** [embedded-mode](./embedded-mode.md)

> **One-line summary.** The AdCP signing chain is *operator-attested*, full stop — the operator's `brand.json` (hosted at the operator's own `house_domain`, not AAO) is the cryptographic root that lists which agents speak for the operator and where their JWKS lives. Our job is to admit operators per tenant, link them to advertisers with billing policy, verify inbound signatures via the `adcp.signing` library, sign outbound with a KMS-backed key whose public half the operator pasted into their own brand.json. AAO is the directory we use to *find* operators; AAO is not on the hot verification path.

## Scope

Non-embedded (open-instance) deployments expose `/mcp/`, `/a2a`, and `/.well-known/agent.json` to the public internet. Today the only authentication is an `x-adcp-auth` bearer token. A leaked token = full impersonation forever, no integrity check on the body, no replay protection on the wire.

This sprint adds the AdCP **request-signing profile** (RFC 9421 / HTTP Message Signatures) on top of bearer:

1. **Operator admission per tenant** — tenants admit one or more *operators* (brand.json-publishing entities) to call their endpoints. Looked up against AAO via `adcp.registry.RegistryClient` for display + canonicalization; admitted by storing their operator-domain `brand_json_url`.
2. **Operator ↔ advertiser link with policy** — a join row says "this operator can transact for this advertiser" plus per-link `billing_mode` (`operator_bills` / `agent_billed` / `disabled`).
3. **Verifier middleware** in front of MCP + A2A — `adcp.signing.verify_starlette_request` backed by `BrandJsonJwksResolver` (operator's brand.json → agents[].jwks_uri → JWK), with `PgReplayStore` for multi-worker replay protection. 401 + `WWW-Authenticate: Signature error="<code>"` on rejection per spec.
4. **Per-tenant signing policy** — master switch + `required_for` operations + RFC 9421 knobs (skew, window, digest policy).
5. **KMS-backed `SigningProvider`** — for *outbound* signing. Local PEM in dev, GCP KMS in prod, AWS KMS when an adapter ships. Sprint 6 webhook signing migrates to this in PR 4.
6. **Operator hosts both brand.json and JWKS** at their own `house_domain`. We do not run any publication endpoint. Admin UI shows the public JWK + kid matching our KMS key so *we* can paste it into *our* brand.json (we are also a sales agent and have to publish exactly like a buyer does).

Out of scope:
- Embedded mode signing. Trust comes from the network + identity headers; signing isn't enforced.
- HMAC backwards-compat for the existing webhook subscribers. PR 4 hard-cuts to RFC 9421; we communicate the change to subscribers out-of-band.
- Buyer-side signing client. Library handles it.
- Agent-level admission as a separate v1 mode. Operator-level admission inherits the operator's full agents list from brand.json. Per-agent revocations are the operator's job, not ours. (We can add per-agent allow/denylists in a later rev if a tenant needs that granularity.)

## Library inventory: what we don't have to build

| Need | Library symbol | Notes |
|---|---|---|
| AAO directory client | `adcp.registry.RegistryClient` | `lookup_brand`, `get_member`, `list_members`, `search_agents`. Returns `ResolvedBrand` with `house_domain` — that's where brand.json lives. |
| Brand.json + JWKS resolver | `adcp.signing.BrandJsonJwksResolver` | Walks `brand_json_url → agents[].jwks_uri → JWK`. The operator-attested trust path. **This is the workhorse for inbound verification.** |
| Verifier shortcut | `adcp.signing.verify_from_agent_url` | Convenience for one-shot tooling; we don't use it in middleware (we want long-lived caches). |
| Starlette middleware | `verify_starlette_request` | Wraps `verify_request_signature`. |
| Verifier checklist | `verify_request_signature` | 14-point pipeline; raises `SignatureVerificationError` with spec error codes. |
| Capability struct | `VerifierCapability` | `request_signing` block on `get_adcp_capabilities`. |
| Options bag | `VerifyOptions` | `jwks_resolver`, `replay_store`, `revocation_checker`, `operation`. |
| 401 header | `unauthorized_response_headers(exc)` | `WWW-Authenticate: Signature error="<code>"`. |
| Replay store (multi-worker) | `PgReplayStore` (in `adcp[pg]` extra) | Postgres-backed. |
| Outbound signing | `sign_request`, `SigningConfig`, `SigningProvider` | KMS abstraction at `SigningProvider`. |
| Outbound signing (dev) | `InMemorySigningProvider` | Loads from PEM bytes. |
| Public-JWK derivation | `pem_to_adcp_jwk`, `generate_signing_keypair` | For local-dev keypair bootstrap; KMS backends derive via cloud SDK (`kms.get_public_key()`). |

> **The lib's own words on the trust root** (`adcp/signing/brand_jwks.py`):
> > The seller's verifier never trusts `agent_url/.well-known/jwks.json` directly — that would let any agent self-attest its own keys. Per ADCP, keys root through the brand: the brand's `/.well-known/brand.json` lists each authorized agent and its `jwks_uri`, **operator-attested**.
>
> Anything we build must respect this. Admitting agents directly (rev 3's design) was wrong — it bypassed the operator-attestation. Rev 4 admits operators; agents are inherited.

## Conceptual model: operator vs agent vs advertiser

| Concept | What it is | Trust role | Where keys live |
|---|---|---|---|
| **Operator** (a.k.a. brand owner, AAO `Member`) | The organization registering with AAO. Owns the trust root: serves `brand.json` at its `house_domain`, lists authorized `agents[]`, points each at a `jwks_uri` it controls. | The cryptographic root of trust. | **Operator's domain.** brand.json + JWKS both at `https://<house_domain>/.well-known/...`. |
| **Agent** | A software endpoint at a canonical `agent_url`, listed in some operator's brand.json `agents[]`. Typed: `brand` / `rights` / `measurement` / `governance` / `creative` / `sales` / `buying` / `signals`. | Holds the signing key. Speaks under the operator's authority. | At the `jwks_uri` declared by the operator's brand.json. |
| **Advertiser** (= our existing `principals` row) | The thing money is spent for. Adapter mappings, currencies, etc. | Authorization target — who is the operator transacting *for*? | None — advertisers don't sign. |
| **Tenant** (= our existing `tenants` row) | The publisher / sales-agent operator. Is itself an operator under AdCP (`BrandAgentType.sales`); has its own brand.json + KMS-backed JWKS for outbound signing. | Both: admits external operators (inbound) AND signs as an operator (outbound). | Outbound: tenant's KMS reference + public JWK at the *salesagent operator's* house_domain. |

In **embedded mode**: a single trusted operator (the host product) is auto-installed at provision; signature verification is bypassed because trust is network-rooted. The admitted-operators table still has a row for audit consistency.

In **non-embedded mode**: tenants admit external AAO-registered operators explicitly. Bearer tokens pin the operator on each request; the signature proves the request was made by an agent that operator authorizes; the request body identifies the advertiser; the operator-advertiser link gates whether that pairing is permitted.

## Trust flow

```
                         operator's
                         own domain
                              │
                              ▼
   buyer agent          ┌────────────────────────────┐
   (one of the          │  https://<operator_house>/  │
   operator's           │      .well-known/brand.json │  ◄─── operator-attested
   authorized           │           agents[] ─────────┼──┐    trust root
   agents)              │              ↓              │  │
        │               │      jwks_uri (also on      │  │
        │ POST + sig    │      operator's domain)     │  │
        │               │              ↓              │  │
        ▼               │           JWK set           │  │
   /mcp/ at salesagent  └────────────────────────────┘  │
        │                                               │
        │   1. bearer  →  (tenant, admitted_operator, advertiser)
        │   2. fetch operator's brand.json (cached, TTL'd)  ◄────┘
        │   3. find agent in brand.json's agents[] matching
        │      the signature's keyid
        │   4. verify signature against that agent's JWKS
        │   5. check operator_advertiser_link.is_active
        │   6. record verified_operator_id + verified_agent_url
        │      + verified_key_id in audit log
        ▼
   _impl(...)  ──  business logic, transport-agnostic

   AAO directory at agenticadvertising.org is consulted
   ONCE at admission time (admin UI search) to populate the
   admitted_operators row's display metadata. Not on the
   hot verify path.
```

The hot path touches only the operator's own domain. AAO is a one-time directory lookup at admission. No SLA dependency on AAO at request time.

## Database schema

### New tables

```sql
-- Operators (brand.json publishers) admitted to a tenant. AAO is consulted at
-- admin-time for display metadata; brand.json is fetched at verify-time from
-- the operator's own house_domain.
CREATE TABLE admitted_operators (
    tenant_id            VARCHAR(50)  NOT NULL,
    operator_id          VARCHAR(50)  NOT NULL,
    brand_json_url       VARCHAR(2048) NOT NULL,         -- operator's own /.well-known/brand.json (canonicalized)
    aao_member_slug      VARCHAR(200) NULL,              -- looked up from RegistryClient.lookup_brand at admit time
    house_domain         VARCHAR(253)  NULL,             -- cached from ResolvedBrand for display + drift detection
    display_name         VARCHAR(200) NOT NULL,          -- cached from AAO Member
    is_trusted           BOOLEAN      NOT NULL DEFAULT FALSE,
                                                          -- true for embedded host's interchange:
                                                          -- skip signature verification entirely
    is_active            BOOLEAN      NOT NULL DEFAULT TRUE,
    last_resolved_at     TIMESTAMPTZ  NULL,              -- last successful brand.json fetch
    last_resolution_error VARCHAR(500) NULL,             -- last BrandJsonResolverErrorCode
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, operator_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    UNIQUE (tenant_id, brand_json_url)
);
CREATE INDEX idx_admitted_operators_active ON admitted_operators (tenant_id, is_active) WHERE is_active;

-- Operator ↔ advertiser link with per-link policy.
CREATE TABLE operator_advertiser_link (
    tenant_id      VARCHAR(50) NOT NULL,
    operator_id    VARCHAR(50) NOT NULL,
    principal_id   VARCHAR(50) NOT NULL,
    billing_mode   VARCHAR(32) NOT NULL DEFAULT 'operator_bills',
                                                  -- 'operator_bills' | 'agent_billed' | 'disabled'
    is_active      BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, operator_id, principal_id),
    FOREIGN KEY (tenant_id, operator_id) REFERENCES admitted_operators(tenant_id, operator_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, principal_id) REFERENCES principals(tenant_id, principal_id) ON DELETE CASCADE
);

-- Bearer token → operator binding. Existing principal access_tokens stay; this
-- adds the operator dimension. A token can name an operator the principal is
-- linked to via operator_advertiser_link; the verifier checks the link is
-- active before verifying signature.
ALTER TABLE principals ADD COLUMN bound_operator_id VARCHAR(50) NULL;
-- bound_operator_id is the operator the bearer attests to. If NULL, the principal
-- is in legacy unsigned mode; signing-required policy will reject. If set, the
-- (tenant, operator_id, principal_id) tuple must exist + be active in
-- operator_advertiser_link or the token is rejected.

-- Per-tenant signing policy.
CREATE TABLE tenant_signing_policy (
    tenant_id            VARCHAR(50) PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    enabled              BOOLEAN NOT NULL DEFAULT FALSE,
    required_for         JSONB   NOT NULL DEFAULT '[]'::jsonb,
    covers_digest_policy VARCHAR(16) NOT NULL DEFAULT 'either',
    max_skew_seconds     INTEGER NOT NULL DEFAULT 60,
    max_window_seconds   INTEGER NOT NULL DEFAULT 300,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Salesagent's own outbound signing credentials. KMS reference + cached public
-- JWK (for the admin UI's "copy to your brand.json" view). The salesagent operator
-- hosts its own brand.json + JWKS at its own house_domain — same as any operator.
CREATE TABLE tenant_signing_credentials (
    tenant_id      VARCHAR(50) NOT NULL,
    purpose        VARCHAR(64) NOT NULL,                  -- 'webhook-signing', 'request-signing-as-buyer', etc.
    backend        VARCHAR(32) NOT NULL,                  -- 'local_pem' | 'gcp_kms' | 'aws_kms' | 'hashicorp_vault'
    backend_ref    VARCHAR(1024) NOT NULL,
    public_jwk     JSONB NOT NULL,                        -- copy-source for our own brand.json hosting; not served from us directly
    key_id         VARCHAR(256) NOT NULL,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_out_at TIMESTAMPTZ NULL,
    PRIMARY KEY (tenant_id, purpose, key_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
);

-- We need to know our OWN brand_json_url (where the salesagent operator
-- publishes its brand.json) so capabilities can advertise it. Lives on Tenant.
ALTER TABLE tenants ADD COLUMN brand_json_url VARCHAR(2048) NULL;
```

### PgReplayStore schema

The library bootstraps `adcp_replay` via `PgReplayStore.create_schema()`. Idempotent at startup.

**Sweep:** `pg_cron` is the default — at startup we check for the extension and install a `DELETE FROM adcp_replay WHERE expires_at <= now()` job at 60s cadence. If `pg_cron` is unavailable (CHECK against `pg_extension`), we fall back to an in-process asyncio task on the same cadence. One config flag `REPLAY_SWEEP_MODE=auto|pg_cron|in_process`; `auto` is the default and does the detection.

## Verifier wiring (Starlette middleware)

```python
# src/core/signing/middleware.py
class SigningVerifyMiddleware:
    """Mounted before MCP and A2A routes.

    Per request:
      1. Resolve (tenant, operator, advertiser) from bearer.
         The bearer's principal must have bound_operator_id set; the
         (tenant, operator_id, principal_id) row in operator_advertiser_link
         must be is_active. Otherwise 403.
      2. If operator.is_trusted → skip verify, succeed (embedded host bypass).
      3. If TenantSigningPolicy(tenant_id).enabled is False → skip verify.
      4. If signature headers absent and operation not in required_for → skip.
      5. Otherwise verify_starlette_request with:
           jwks_resolver = OperatorBrandJsonCache.resolver_for(operator.brand_json_url)
                           # internally: BrandJsonJwksResolver, library-cached
           replay_store  = shared PgReplayStore
           capability    = policy.capability()
      6. On success: record verified_operator_id + verified_agent_url
         (from the matched brand.json agents[] entry) + verified_key_id
         on request.state.
      7. On SignatureVerificationError: 401 + unauthorized_response_headers(exc).
      8. Audit-log on every reject (including the rejected operator/agent
         identity, however incomplete) so the admin UI's
         "unknown operator attempted" surface has data to show.
    """
```

`_impl` functions stay transport-agnostic. `resolve_identity()` reads verified state off `request.state` and folds it into `ResolvedIdentity` (which gains `verified_operator_id` + `verified_agent_url` + `verified_key_id` fields).

## Resolver: one library call per operator

```python
# src/core/signing/operator_brand_json_cache.py
class OperatorBrandJsonCache:
    """Per-process cache of BrandJsonJwksResolver keyed by brand_json_url.

    Library handles SSRF, IP pinning, brand.json shape validation, JWK fetch,
    TTL refresh, and unknown-kid cascade refresh. We just memoize the resolver
    instance so concurrent verifies share one cached brand.json snapshot.
    """
    def resolver_for(self, brand_json_url: str) -> JwksResolver: ...
```

That's the resolver story. No custom dispatch.

## Capability advertisement

`get_adcp_capabilities` gains:

```json
{
  "request_signing": {
    "supported": true,
    "covers_content_digest": "either",
    "required_for": ["create_media_buy", "update_media_buy"],
    "supported_for": []
  },
  "identity": {
    "brand_json_url": "https://<our_house_domain>/.well-known/brand.json"
  }
}
```

The `identity.brand_json_url` is sourced from `Tenant.brand_json_url` (configured at tenant setup). Receivers verifying our outbound signatures resolve through *our* house_domain.

## Admin UI

Three new surfaces:

1. **Admitted operators** at `/admin/tenant/{tid}/operators`
   - List admitted operators: display_name (from AAO), brand_json_url, aao_member_slug, is_trusted, is_active, last_resolved_at, last_resolution_error.
   - **Add operator**: search box that calls `RegistryClient.search_agents` / `lookup_brand` → operator picks from results → row created with metadata cached from AAO and `brand_json_url` derived from `house_domain`. Freeform-URL fallback exists (gated behind a "I know what I'm doing" toggle) for operators not yet in AAO directory.
   - **Test resolution**: button that calls `BrandJsonJwksResolver.fetch(brand_json_url)` synchronously and shows the agents[] list + JWK counts + any `BrandJsonResolverErrorCode`. Doubles as an after-rotation health check.
   - **Disable** action: sets `is_active=false`. Subsequent requests bound to this operator reject at step 1 (403, not 401 — it's authorization, not signature).
   - **Per-operator advertiser links**: nested table showing `operator_advertiser_link` rows for this operator, with billing-mode picker (`operator_bills` / `agent_billed` / `disabled`).
   - **Unknown-operator log**: a panel showing recent verifier rejections where the bearer named an unbound operator (or no operator) — surfaces operators trying to onboard. One-click "Admit operator" action prefills the add-operator form with whatever AAO data we can pull.

2. **Tenant signing policy** at `/admin/tenant/{tid}/settings/signing`
   - Master switch (`enabled`).
   - Multi-select for `required_for` (sourced from the AdCP capabilities catalog).
   - `covers_digest_policy` radio.
   - Skew/window numeric fields.

3. **Tenant outbound signing** at `/admin/tenant/{tid}/settings/signing/outbound`
   - Backend picker → backend-specific `backend_ref` field.
   - **Validate**: calls the backend's `get_public_key`, displays the resulting JWK + kid, saves on confirm.
   - **Brand JSON URL** field: tenant's own `house_domain` brand.json URL. We populate `identity.brand_json_url` on capabilities from this.
   - **Show JWK + kid** view: the operator copies these into their own brand.json `agents[]` entry. We render a brand.json snippet pre-formatted so it's a literal copy-paste.
   - **Rotate** action: walks the operator through generating a new key version + updating their brand.json + updating us; old credential remains active until `rotated_out_at`.
   - **Test sign**: signs a sample blob via `SigningProvider`.

All three pages are platform-managed. On embedded instances, the operators page renders showing only the auto-installed trusted host operator with a banner explaining signing isn't enforced.

## Operator setup flow (per tenant)

Once per tenant, when the operator wants to enable signing:

1. **Generate a keypair in the chosen backend.** Local PEM (dev), GCP KMS (prod), or AWS KMS when the adapter ships. For KMS, this is `gcloud kms keys create ... --purpose=ASYMMETRIC_SIGN`.
2. **Configure salesagent.** Admin UI → Outbound Signing → pick backend, paste `backend_ref`, click **Validate**. We call the backend's `get_public_key` and store the public JWK + kid (private bytes never enter our process for KMS).
3. **Stand up the operator's brand.json + JWKS at their `house_domain`.** The operator hosts:
   - `/.well-known/brand.json` listing their `agents[]` with each agent's `jwks_uri`.
   - A JWKS document at each `jwks_uri` containing the public JWK we showed in step 2.
   The admin UI provides a copy-paste-ready brand.json snippet.
4. **Tell salesagent the brand.json URL.** Admin UI → paste their `https://<house_domain>/.well-known/brand.json` into Tenant.brand_json_url. We expose it on `get_adcp_capabilities → identity.brand_json_url`.

Rotation is the same flow with a new key version and an overlap window (old + new public JWKs both in brand.json; old `rotated_out_at` set on our side).

This is symmetric: the salesagent is itself a sales-type operator and registers with AAO + hosts its own brand.json + JWKS at its house_domain, just like any buyer-side operator does.

## KMS plumbing: `SigningProvider` adapters

```python
# src/core/signing/providers.py
class LocalPemSigningProvider(SigningProvider):
    """Mode-0600 PEM file. Local-dev only; gated on environment != production."""

class GcpKmsSigningProvider(SigningProvider):
    """google.cloud.kms.AsymmetricSign. backend_ref is the full KMS key version
    resource name. Public key derived via kms.get_public_key() at validate time."""
```

Factory dispatches on `tenant_signing_credentials.backend`. AWS KMS + Vault adapters land when needed.

## Phasing

| PR | Scope | Required for ship? |
|---|---|---|
| **PR 1: Foundation** | DB migrations (`admitted_operators`, `operator_advertiser_link`, `tenant_signing_policy`, `tenant_signing_credentials`, `principals.bound_operator_id`, `tenants.brand_json_url`). `adcp[pg]` extra in `pyproject.toml`. `PgReplayStore.create_schema()` at startup with sweep mode detection. Repositories. `OperatorBrandJsonCache` thin wrapper over `BrandJsonJwksResolver`. Embedded-mode tenant provisioning gets the auto-installed trusted-operator row. **No middleware mounted yet.** | yes |
| **PR 2: Verification + capability** | `SigningVerifyMiddleware` mounted in `core.main`. `resolve_identity()` consumes verified state. `get_adcp_capabilities` advertises `request_signing` + `identity.brand_json_url`. Audit log gains `verified_operator_id`, `verified_agent_url`, `verified_key_id`. Integration tests cover: signed accept; missing-when-required reject; bad sig reject; replay reject across two workers; trusted operator bypass; operator_advertiser_link disabled → 403. Sweep job lands. | yes |
| **PR 3: Admin UI + AAO + KMS** | Three admin pages above. AAO `RegistryClient` integration for the operator search picker. `LocalPemSigningProvider` + `GcpKmsSigningProvider`. Per-link billing-mode editing surfaces but does not yet *enforce* `agent_billed` (separate design). Unknown-operator panel powered by audit log. | yes |
| **PR 4: Outbound webhook signing migration** *(fast-follow, hard cut)* | Switch `webhook_delivery._post_signed` from `sign_legacy_webhook` (HMAC-SHA256 shared secret) to `sign_request` using `SigningProvider`. Capability advertisement updated. **No backwards-compat window** — subscribers verify via our `Tenant.brand_json_url`. Operators are responsible for notifying their subscribers. | no — fast-follow |

PRs 1–3 are the inbound path. PR 4 brings outbound to spec compliance.

## Acceptance criteria

**PR 1 — Foundation:**
- [ ] Migrations apply forward and reverse on a populated database.
- [ ] `admitted_operators.brand_json_url` is canonicalized at insert per AdCP URL canonicalization rules.
- [ ] `operator_advertiser_link` cascades on operator delete and on principal delete.
- [ ] Embedded-mode tenant provision creates exactly one `is_trusted=true` operator row with `aao_member_slug` set to the host org and `brand_json_url` to a sentinel value (we never resolve trusted operators).
- [ ] `OperatorBrandJsonCache.resolver_for(url)` returns a callable backed by one `BrandJsonJwksResolver` per URL within TTL.
- [ ] `PgReplayStore.create_schema()` runs at startup, idempotent. Sweep mode detection picks `pg_cron` when extension is present, otherwise `in_process`.
- [ ] No middleware mounted; existing buyer protocol traffic untouched.

**PR 2 — Verification:**
- [ ] Bearer with no `bound_operator_id` against tenant with `enabled=true, required_for=["create_media_buy"]` → 403 `operator_not_bound`.
- [ ] Bearer with `bound_operator_id` set but `operator_advertiser_link` inactive → 403 `operator_link_disabled`.
- [ ] Bearer + signature where the signing kid matches an agent in the operator's brand.json → succeeds; verified state in `ResolvedIdentity`.
- [ ] Bearer + signature where kid is NOT in brand.json's agents[] → 401 `request_signature_key_unknown`.
- [ ] Replay across two workers → `request_signature_replayed` on the second.
- [ ] Trusted operator → bypasses verification entirely.
- [ ] Tenant with `enabled=false` → middleware no-op; bearer auth works.
- [ ] Audit log carries `verified_operator_id`, `verified_agent_url`, `verified_key_id` on success and `attempted_operator_id` (best-effort) on failure.

**PR 3 — Admin UI + AAO + KMS:**
- [ ] AAO operator search via `RegistryClient.lookup_brand` returns expected results in fixture; pick + admit creates a populated `admitted_operators` row.
- [ ] Test-resolution UI walks `BrandJsonJwksResolver.fetch` and surfaces agents[] + JWK counts + error codes.
- [ ] Disable operator; subsequent request → 403 as in PR 2.
- [ ] `local_pem` outbound credential: Validate → Display public JWK + kid → Test sign produces a verifiable signature.
- [ ] `gcp_kms` outbound credential (gated CI test): same end-to-end.
- [ ] Operator pastes `Tenant.brand_json_url`; capabilities reflect it.
- [ ] Per-link billing-mode round-trips correctly; `agent_billed` stored, not enforced (logged as "billing-mode enforcement deferred").
- [ ] Unknown-operator panel populates from rejected requests; click-to-admit prefills the form.

**PR 4 — Webhook signing cutover:**
- [ ] Webhook delivery posts RFC 9421 signed requests; HMAC code path removed.
- [ ] Capability advertisement at our brand.json reflects the active webhook-signing kid.
- [ ] Sample receiver using `verify_from_agent_url(<our agent_url>)` accepts our deliveries end-to-end.

## Risks

- **Adding signing breaks integration buyers** who haven't deployed signing yet. Master `enabled=false` default means no tenant is affected without operator action.
- **PR 4 hard-cut breaks existing webhook subscribers.** No compat window. Operators must notify subscribers out-of-band before flipping. Acceptable per locked decision; we are pre-1.0.
- **Operator hosts brand.json + JWKS — distributed availability risk.** Each operator is responsible for their own publication uptime. Library TTL caching covers transient outages; cold-cache requests during operator outage fail closed (`request_signature_jwks_unavailable`). Operators are accountable for their own infra; we don't own this surface.
- **AAO directory lookup at admission.** One-time, not on hot path. AAO outage stalls new operator admissions but doesn't affect verification of already-admitted operators.
- **PgReplayStore unbounded growth without sweep.** Mitigated by auto-detected sweep mode; structural test confirms either `pg_cron` job or in-process task is running.
- **KMS adapter latency** on signed webhook delivery. Single-digit ms in-region for GCP KMS. Recommend ship `local_pem` first; KMS opt-in per tenant.
- **Library version pin.** `adcp.signing` and `adcp.registry` are moving targets. Pin coherently; structural test against a documented compatibility set.
- **Conceptual drift between "principal" and "operator/agent" in old code.** The PR 1 migration adds the new column on `principals` (`bound_operator_id`) but doesn't refactor existing call sites. PRs 2+ thread the operator through where the verifier needs it; legacy code paths assuming "principal owns the call" continue working with `enabled=false`.

## Open questions

None blocking — all rev 3 questions resolved. Future considerations (do not block this design):

- **AdCP #3690 `authorized_operators[]` delegation.** When the lib ships verifier-side support, we lift the bearer-pins-operator requirement and let the signature itself bind to the operator. Future migration; tracked as a follow-up.
- **Per-agent allow/denylists within an admitted operator.** Some tenants may want to admit operator O but block agent X within O. Defer until concrete demand; the operator-attestation already handles this at the operator's brand.json layer.
- **Multi-operator-per-principal.** Today's schema is one operator per principal (`principals.bound_operator_id`). If a single advertiser is reachable through more than one operator simultaneously, lift `bound_operator_id` into a join. Defer; one-to-one matches the typical reality.

## Decision

**Locked.** Ready to scaffold PR 1.
