# Improve Digital Security Perimeter

## Overview

The Improve Digital adapter books Classic (direct) campaigns against the
360Yield Marketplace API. This document outlines its credential handling and
tenant-isolation boundaries.

## Credential Storage

- The OAuth2 pair lives in `AdapterConfig.config_json`; `client_secret` is
  **Fernet-encrypted at rest** by the connection schema's field serializer
  (`ENCRYPTION_KEY` environment variable). Decryption happens only via the
  schema's field validator on attribute access — `model_dump()` re-encrypts,
  so plaintext never round-trips through serialized configs.
- Bearer tokens are minted on demand (`POST /oauth/token`, HTTP Basic auth)
  and cached **in memory only** with TTL tracking — never persisted. There
  is no refresh token; expiry triggers a fresh mint.
- Credentials travel exclusively in the `Authorization` header — never in
  URLs or query strings.

## Admin-surface protections

- The generic adapter-config save endpoint **rejects submitted ciphertext**
  on secret fields (a tenant admin must not be able to replay another
  tenant's leaked DB-row ciphertext) and **preserves the stored secret**
  when the form leaves it blank.
- Settings pages receive secrets reduced to booleans — ciphertext never
  reaches page source.
- Upstream response bodies are passed through a redacting excerpt helper
  before logging (`access_token`/`api_token`/`password`/`Authorization`
  markers), and sync error messages are secret-pattern-scrubbed and
  length-bounded before persisting to `sync_jobs.error_message`.

## Principal Mapping

Each AdCP principal may map to a numeric 360Yield advertiser:

```python
{
    "principal_id": "publisher_xyz",
    "name": "Publisher XYZ",
    "platform_mappings": {
        "improvedigital": {
            "advertiser_id": "5001"
        }
    }
}
```

The mapping is optional — the Classic campaign API accepts campaigns without
an advertiser; `default_advertiser_id` on the tenant connection config backs
it, and non-numeric values are dropped rather than sent (the
`CampaignDto.advertiserId` field is an integer).

## Tenant Isolation

- All local caches (`improvedigital_inventory`,
  `improvedigital_line_item_stats`) and sync jobs are tenant-scoped by
  construction — every repository query filters by `tenant_id`, and both
  cache tables FK to `tenants` with `ON DELETE CASCADE`.
- Campaign-ID resolution goes through the tenant-scoped
  `MediaBuyRepository`, so delivery reads and creative binding can never
  cross tenants.
- Admin endpoints are guarded by `@require_tenant_access`.
