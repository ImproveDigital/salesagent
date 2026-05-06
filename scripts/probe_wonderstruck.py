"""Probe Wonderstruck GAM network for advertiser + ad unit IDs we can use in
the real-GAM lifecycle test.

Reads WONDERSTRUCK_SERVICE_KEY_FILE + WONDERSTRUCK_NETWORK_CODE from .env.
Prints up to 5 advertisers and up to 10 active ad units.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load .env (no python-dotenv; tiny manual parser).
for line in (Path(__file__).resolve().parents[1] / ".env").read_text().splitlines():
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"'))

from src.adapters.gam.client import GAMClientManager  # noqa: E402

SERVICE_KEY_FILE = os.environ["WONDERSTRUCK_SERVICE_KEY_FILE"]
NETWORK_CODE = os.environ["WONDERSTRUCK_NETWORK_CODE"]


def main() -> None:
    config = {"service_account_key_file": SERVICE_KEY_FILE}
    cm = GAMClientManager(config, network_code=NETWORK_CODE)
    client = cm.get_client()

    network = client.GetService("NetworkService").getCurrentNetwork()
    print(f"Network: {network['displayName']} ({network['networkCode']})")
    print(f"Currency: {network['currencyCode']}, TZ: {network['timeZone']}")
    print()

    from googleads import ad_manager

    def _get(zobj, key, default=None):
        """zeep ComplexType doesn't support .get(); fall back to attr access."""
        try:
            return zobj[key]
        except (KeyError, AttributeError):
            return getattr(zobj, key, default)

    # Advertisers (CompanyService, type=ADVERTISER)
    company_svc = client.GetService("CompanyService")
    sb = ad_manager.StatementBuilder()
    sb.Where("type = :t").WithBindVariable("t", "ADVERTISER").Limit(5)
    page = company_svc.getCompaniesByStatement(sb.ToStatement())
    results = _get(page, "results") or []
    total = _get(page, "totalResultSetSize")
    print(f"Advertisers (showing {len(results)} of {total}):")
    for c in results:
        print(f"  id={_get(c, 'id')}  name={_get(c, 'name')}")
    print()

    # Ad Units (active)
    au_svc = client.GetService("InventoryService")
    sb = ad_manager.StatementBuilder()
    sb.Where("status = :s").WithBindVariable("s", "ACTIVE").Limit(10)
    page = au_svc.getAdUnitsByStatement(sb.ToStatement())
    au_results = _get(page, "results") or []
    au_total = _get(page, "totalResultSetSize")
    print(f"Ad units (showing {len(au_results)} of {au_total}):")
    for au in au_results:
        sizes = []
        for s in _get(au, "adUnitSizes") or []:
            sz = _get(s, "size") or {}
            if _get(sz, "width") and _get(sz, "height"):
                sizes.append(f"{_get(sz, 'width')}x{_get(sz, 'height')}")
        print(f"  id={_get(au, 'id')}  name={_get(au, 'name')}  " f"sizes={','.join(sizes) or '(none)'}")


if __name__ == "__main__":
    main()
