# Sales Agent Admin UI — Plain-Language Guide

*(with GAM explained, and what Improve Digital needs on every screen)*

This guide walks through every part of the tenant admin UI — the top tabs, the
Settings side tabs, and the Configure dropdown — and answers four questions for
each screen:

1. **What is it?**
2. **Why does it exist?** (with a small example where it helps)
3. **How does GAM use it today?** (what all the GAM words mean)
4. **What does Improve Digital need here?**

Screens were reviewed on a live, fully-working GAM tenant
(`Azerion Gaming` on sales-agent.yieldpro.improvedigital.com). The engineering
checklist lives in [INTEGRATION_PLAN.md](./INTEGRATION_PLAN.md) — this doc is
the "understand it first" companion.

---

## 0. The big picture (read this first)

The **sales agent** is an automated salesperson for a publisher's ad space.
AI **buyer agents** (for example, a brand's media-buying AI) talk to it over
the **AdCP protocol** (via MCP or A2A — two "phone lines", same conversation).
The buyer can ask *"what can I buy?"*, *"buy this"*, *"how is my campaign
doing?"* — and the sales agent answers automatically.

The sales agent doesn't serve ads itself. It takes the buyer's order and
creates it inside a real **ad server**. Which ad server? That's what the
**adapter** decides. Today the tenant we looked at uses **Google Ad Manager
(GAM)**. Our project adds a second option: **Improve Digital** (Azerion's
360 Polaris marketplace).

One full order, end to end:

> A buyer agent calls `get_products` → sees "Gaming audience, €5 CPM, NL".
> It calls `create_media_buy` with a €100 budget and two weeks of flight
> dates. The sales agent's **GAM adapter** logs into GAM and creates an
> **Order** (the contract) with a **Line Item** (the delivery instructions:
> where, when, how much). The buyer sends banner images (`sync_creatives`);
> a human approves them in the **Creatives** tab; the adapter uploads them to
> GAM and attaches them to the line item. GAM starts serving. Every hour the
> sales agent pulls delivery numbers from GAM, so when the buyer asks
> `get_media_buy_delivery`, it can answer "34,000 impressions, €170 spent."

Every screen below exists to set up, watch, or repair some part of that story.

**The one Improve Digital difference to keep in mind:** GAM is an *ad server*
(it renders the ad itself). Improve Digital is a *marketplace/SSP*: for its
main "Universal Deal" flow, we create a **Campaign + Line Item (a deal)** that
a buyer's DSP then bids into — the creatives usually live on the buyer's side,
not ours. Same shape of story, slightly different casting.

---

## 1. Top tabs (day-to-day work)

### 1.1 Dashboard

- **What:** The home page. Revenue for the last 30 days, deals waiting on you
  ("Incoming"), deals currently delivering ("Running"), alerts ("8 creatives
  need approval", "deal pacing under"), and a ledger of recent activity.
- **Why:** So an operator can answer "is anything on fire?" in ten seconds.
  Example: the Azerion Gaming dashboard flagged a running deal at *"0%
  delivered · should be 90%"* — that's a paused or broken line item someone
  should look at.
- **GAM today:** The money and pacing numbers come from delivery stats that
  the GAM sync writes into the local database (impressions/spend per line
  item). Everything else (media buys, creatives, audit log) is our own data.
- **Improve Digital needs:** Nothing UI-wise. Once our **reporting sync**
  (Improve's Report API → local stats cache) works, the dashboard fills in by
  itself. Accuracy depends on us mapping Improve's `advertiser_payout` to
  spend and its line-item state to "delivering / not delivering".

### 1.2 Media Buys

- **What:** Every order buyers have placed, as a pipeline: *needs creatives →
  needs approval → ready → live → completed* (plus *failed*). Each row shows
  budget, flight dates, and per-package readiness; broken rows show the error
  ("Media buy creation failed", "No creatives uploaded").
- **Why:** This is the order book. Example: a buy stuck in "needs approval"
  means a human must approve it (see Workflows); one stuck in "needs
  creatives" means the buyer never sent images.
- **GAM today:** A media buy = a GAM **Order**; each package inside it = a GAM
  **Line Item**. Rows named `Order-4098644343` are special: they were
  *imported from GAM* (orders that already existed in the ad server), via a
  GAM-only "projection" layer.
- **Improve Digital needs:** Our adapter's `create_media_buy` (creates an
  Improve **Unified Deal** = campaign + line item in one API call),
  `check_media_buy_status` (maps Improve's deal states onto the pipeline),
  and `update_media_buy` (pause/resume/budget changes). The "imported orders"
  trick is GAM-only; an Improve equivalent (showing pre-existing marketplace
  deals) is optional follow-up work (plan item **H5**).

### 1.3 Products

- **What:** The catalog you show buyers. Each product = a name + price (e.g.
  CPM €5.00) + countries + a slice of inventory it sells.
- **Why:** Buyers don't browse your raw ad server; they browse *products*.
  Example: "Gaming audience — €5 CPM — NL" packages up a set of gaming-site ad
  slots so a buyer's `get_products` call returns something sellable.
- **GAM today:** When you create/edit a product, an adapter-specific form
  section asks *which GAM ad units and placements* the product targets —
  pickers filled from synced GAM inventory.
- **Improve Digital needs:** Our own product-config form
  (`product_config.html`) with **placement / package / size pickers** filled
  from our synced Improve inventory, plus `get_supported_pricing_models` (CPM
  to start) so the pricing dropdown offers the right options.

### 1.4 Inventory (Inventory bundles)

- **What:** "Bundles" — named bags of ad slots ("spel.nl 728x90" = 2 ad
  units). A coverage meter shows how much of your synced inventory is bundled
  (Azerion Gaming: 5 of 22 ad units).
- **Why:** Raw ad-server inventory is messy; bundles are the tidy shapes you
  compose products from. Example: bundle all leaderboard slots on spel.nl
  once, then reuse that bundle in three different products.
- **GAM today:** The page literally says *"Authored against Google Ad
  Manager"* and counts GAM entity types (**ad units** — the slot tree in GAM —
  and **placements** — GAM's own named groups of ad units).
- **Improve Digital needs:** Bundles must count and pick from **Improve
  placements and packages** instead. Our adapter declares
  `inventory_entity_label="placement"`; the page must respect adapter entity
  types rather than assuming GAM's (plan item **H4**).

### 1.5 Signals

- **What:** Audience/targeting data buyers can ask for by name. The page
  lists **audience segments** (e.g. "sports fans") to map to buyer-facing
  signals, and **custom targeting keys** (key→value labels on ad requests).
- **Why:** A buyer agent may ask "target sports fans" — signals are how the
  sales agent knows what that means in your ad server. Example: map the
  buyer-visible signal "Gaming enthusiasts" to GAM audience segment #12345,
  and deals created for that signal get that segment targeting.
- **GAM today:** Fed entirely by the GAM inventory sync: 10 audience segments
  + 240 custom targeting keys (things like `AGE`, `cat`, `amznbid` — labels
  GAM publishers attach to ad requests).
- **Improve Digital needs:** Improve's equivalents synced as extra inventory
  types: **DMP segments**, **contextual segments** (DoubleVerify, Captify,
  Azerion Intelligence), and **key-value targeting keys/values**. Not
  order-blocking — planned as a follow-up (plan item **H3**).

### 1.6 Creatives

- **What:** The review queue for ad images/videos buyers upload — approve or
  reject each (Azerion Gaming: 72 approved).
- **Why:** Publishers don't want unreviewed ads on their sites. Example: a
  buyer syncs a 300x250 banner; it sits "pending" here until a human (or an
  auto-approval rule, see Policies) passes it; only then does it go to the ad
  server.
- **GAM today:** The queue itself is our own database. After approval, the
  GAM adapter uploads the file to GAM and binds it to the right line items
  (GAM calls that binding a "LICA").
- **Improve Digital needs:** The queue works unchanged. The upload step is
  the known gap **G1**: Improve's v3 API has *no creative-upload endpoint*
  (creatives on "Classic" campaigns are made in their UI; on Universal Deals
  the buyer's DSP holds the creatives). Start = Universal Deals, where no
  upload is needed; ask Improve if a programmatic upload API exists for
  Classic.

### 1.7 Workflows

- **What:** The task inbox: active media buys, pending human tasks, total
  active spend.
- **Why:** Some steps deliberately wait for a human — e.g. "Require manual
  approval for order activation" (see Policies) parks each new buy here until
  someone clicks approve.
- **GAM today / Improve Digital needs:** Fully generic. Our adapter just
  creates the same kind of approval tasks where needed (e.g. for anything the
  Improve API can't do automatically).

### 1.8 Reports

- **What:** Delivery analytics: impressions, spend, CPM, by advertiser /
  country / ad unit / order / line item, with export.
- **Why:** Publishers reconcile revenue and debug delivery here. Example:
  "which country did that campaign actually deliver in?"
- **GAM today:** ⚠️ The page is titled **"GAM Reporting"** and is built
  directly on GAM's report service — GAM-only dimensions, GAM-only backend.
- **Improve Digital needs:** On an Improve tenant this tab would be
  broken/empty. We must build an Improve reporting view (Improve's Report API
  gives campaign / line item / placement / country / day / hour dimensions and
  impressions / clicks / spend / video metrics) or generalize the tab. Biggest
  UI work item (plan item **H1**).

---

## 2. Settings side tabs (one-time setup & access)

### 2.1 Account 🏢

- **What:** Organization name, fixed subdomain, favicon/branding, optional
  custom domain, and access control (which email domains/addresses may log
  in).
- **Why:** Identity and door policy. The custom domain matters more than it
  looks: it becomes your **public agent URL** — the address publishers must
  list to authorize you (see Publishers, §3.1).
- **GAM / Improve Digital:** Fully adapter-agnostic. Nothing to build.

### 2.2 Setup Checklist ✅

- **What:** A guided to-do list (Critical / Recommended / Optional) with a
  progress bar. "You're Ready to Take Orders!" appears when critical items are
  green.
- **Why:** It encodes the dependency chain for going live: connect ad server →
  sync inventory → create products → set a default advertiser → get a
  publisher to authorize you.
- **GAM today:** Includes GAM-specific items: **"GAM Default Advertiser"**
  (where unmatched buyer traffic books to) and **"GAM Advertiser Create
  Permission"** (proof the credential can create advertiser records).
- **Improve Digital needs:** Equivalent checklist entries for the Improve
  flavor — e.g. "default buying entity (DSP) + seat configured" instead of
  "default GAM advertiser" (part of plan item **H2**).

### 2.3 Users & Access 👥

- **What:** SSO configuration (Google/Microsoft/custom OIDC), allowed email
  domains, individual users, and a "Setup Mode" switch that permits test
  credentials until SSO is ready.
- **Why:** Controls who can administer the tenant.
- **GAM / Improve Digital:** Fully adapter-agnostic.

### 2.4 Ad Server 🖥️ — *the adapter's home*

- **What:** Pick your ad serving platform (cards: Mock, **Google Ad Manager**,
  Broadstreet, FreeWheel, SpringServe) and fill in its connection details.
- **Why:** This is the single decision that wires a tenant to a real ad
  platform. Everything downstream (inventory, products, orders, reports)
  flows through this connection.
- **GAM today, decoded:**
  - **Service account** (recommended): the platform creates a robot Google
    identity (e.g. `adcp-sales-tenant-azerion-gami@…iam.gserviceaccount.com`).
    A GAM admin adds that email inside GAM (*Admin → Access & authorization →
    Users*) with the **Trafficker** role — "allowed to create and edit
    orders". No passwords are exchanged; GAM simply trusts that email.
  - **Network code** (e.g. `1015413`): the ID of which GAM network (which
    publisher account) to talk to.
  - **Test Connection**: does a harmless read to prove the setup works.
- **Improve Digital needs:** A new **"Improve Digital" card** plus a much
  simpler form — Improve uses **OAuth2 client credentials**: a Client ID +
  Client Secret (issued by Improve's team) that we exchange for a short-lived
  token on every session. Form fields: client ID, client secret, default
  currency/timezone, default buying entity (DSP) + seat. Plus a
  test-connection endpoint. (Plan Phases 4–6.)

### 2.5 Buyer Agents 👥

- **What:** The registered buyer principals — each row = one buyer agent with
  an ID, an **access token** (its password for calling our MCP/A2A API), and a
  **platform mapping**.
- **Why:** Two jobs: authentication (the token) and identity translation (the
  mapping). Example: Azerion Gaming has one principal, "Improve Digital
  Marketplace", mapped to `GAM: 5104261184` — every order that buyer places is
  booked in GAM under advertiser company #5104261184, so GAM's own reports
  group its spend correctly.
- **GAM today:** Mapping = a GAM **advertiser (company) ID**. Fine-grained
  routing has moved to the Buyer Routing page (§3.5).
- **Improve Digital needs:** An `improvedigital` mapping type holding the
  marketplace buyer identity: **buying entity ID (the DSP)** + **seat IDs** +
  **advertiser UUID** — the fields our adapter writes into each Unified Deal.
  Principal create/edit forms need these fields (plan item **H2**).

### 2.6 Danger Zone ⚠️

- **What:** "Deactivate Sales Agent" — stops all buys and API access, hides
  the tenant, preserves data (support can reactivate). Type-to-confirm.
- **GAM / Improve Digital:** Adapter-agnostic. Nothing to build.

---

## 3. Configure dropdown (operator tools)

### 3.1 Publishers

- **What:** Your publisher partnerships. Shows your **agent URL** and the list
  of publisher sites, each verified against the publisher's `adagents.json`.
- **Why:** Anti-impersonation. Anyone could claim "I sell spel.nl inventory" —
  so the *publisher* proves it by hosting a tiny public file
  (`adagents.json`) on their site listing which sales-agent URLs may sell
  their inventory. Buyers check that file before trusting you. Example:
  spel.nl shows "Pending" with a *"Send AAO link to publisher"* action — the
  publisher hasn't added the file yet.
- **GAM / Improve Digital:** Fully adapter-agnostic (it's an AdCP-protocol
  thing, not an ad-server thing).

### 3.2 Browse inventory

- **What:** Titled **"GAM Inventory Browser"** — search and inspect everything
  the sync pulled from GAM: the **ad-unit tree** (the folder-like hierarchy of
  slots), placements, labels; with sync status and Sync All / Selective
  buttons.
- **Why:** Operators need to see what's actually available before building
  bundles/products, and to check "did the sync work?".
- **Improve Digital needs:** An Improve browse view (or a generalized page)
  showing our synced **placements** (with publisher, size, placement type,
  seller type) and **packages**. GAM-hardcoded today (plan item **H4**).

### 3.3 Targeting criteria

- **What:** A browser for synced GAM targeting data: **custom targeting keys**
  (labels like `AGE`, `cat` that publishers attach to ad requests), **audience
  segments**, **labels** — plus the **AXE key mapping** (choose which three GAM
  keys carry AdCP audience-targeting include/exclude/macro values).
- **Why:** So orders can carry audience targeting. Example: if AXE's
  "include" key is mapped to GAM key `axe_inc`, a buyer's audience choice
  becomes `axe_inc=segment123` targeting on the GAM line item.
- **Improve Digital needs:** The concepts map to Improve's **key-value
  targeting** (searchable keys/values per publisher) and **segments**
  (DMP/contextual). An Improve version of this browser is follow-up scope
  (plan items **H3/H4**).

### 3.4 Sync inventory

- **What:** The sync control room: **Incremental Sync** (fetch changes),
  **Full Reset** (wipe and re-fetch), **Sync Targeting Data** (keys, segments,
  labels *from GAM*), with status and quick stats.
- **Why:** The local inventory cache is what powers every picker and bundle
  page; this is where you refresh it or fix it when it's stale.
- **Improve Digital needs:** Our `run_inventory_sync` (placements/packages/
  sizes via Improve's placement-search API) runs automatically on the shared
  scheduler; this page's buttons should trigger it too — via the
  `sync-inventory` admin endpoint from plan Phase 6. Page copy/wiring is
  GAM-specific today (plan item **H4**).

### 3.5 Buyer routing

- **What:** Rules deciding **which advertiser account each buyer's order books
  under**. A default ("Default GAM advertiser") catches everything unmatched;
  rules match (agent, operator, brand) triples, most-specific wins; an
  advertiser table syncs "from GAM"; "Recent activity" shows real traffic with
  a **Promote ↑** button to turn a seen combination into a rule.
- **Why:** One buyer agent often carries many brands. Booking Nike's and
  Adidas's orders under the right advertiser records keeps the publisher's
  ad-server reporting and billing clean. Example: everything defaults to
  advertiser `5104261184`; when Nike traffic appears in Recent activity, you
  "promote" it to a rule pointing at a dedicated Nike advertiser record.
- **GAM today:** Entirely GAM-worded and GAM-backed (advertiser list synced
  from GAM's company records; auto-creation of missing advertisers is also
  GAM-only).
- **Improve Digital needs:** The same *idea* re-targeted: route each (agent,
  operator, brand) to an Improve **buying entity (DSP) + seat + advertiser
  UUID**, with a default for unmatched traffic. This is the core of plan item
  **H2**.

### 3.6 Policies & Workflows

- **What:** House rules: **Budget controls** per currency (min/max per
  package), **brand manifest policy**, **naming conventions** (templates like
  `Nike - PO-12345 - Oct 7-14, 2025` for order/line-item names), **measurement
  providers**, **approval workflow** (manual approval on/off), **creative
  review mode**, **advertising policy** (AI brief screening), **AI product
  ranking**.
- **Why:** Guardrails. Example: "Max daily package spend €10" stops a buyer
  from accidentally booking €10,000/day; "require manual approval" routes
  every new order through the Workflows inbox first.
- **GAM today:** Mostly generic, with two GAM touches: the currency list is
  "enabled in your **GAM network**", and the naming templates are written to
  GAM order/line-item names.
- **Improve Digital needs:** Currencies validate against Improve's supported
  set (EUR, USD, GBP + 12 more); naming templates apply to Improve
  campaign/line-item names. Small wiring, no new UI.

### 3.7 Integrations

- **What:** Slack webhooks (task + audit notifications), AI services (API key,
  model choice, optional Logfire), Creative Agents, Signals Discovery Agents.
- **GAM / Improve Digital:** Fully adapter-agnostic.

### 3.8 Signing keys

- **What:** Generate an Ed25519 keypair; publish the public half at
  `https://<your-domain>/.well-known/jwks.json` so receivers can verify your
  **webhook signatures** (proof that "campaign delivered" pings really came
  from you).
- **GAM / Improve Digital:** Fully adapter-agnostic.

### 3.9 Tenant Settings

Shortcut back to the Settings side tabs (§2).

---

## 4. Cheat sheet — where the Improve Digital work is

| Surface | Verdict | Work |
|---|---|---|
| Dashboard, Workflows, Creatives queue, Publishers, Integrations, Signing keys, Account, Users, Danger Zone | ✅ Generic | None — works once adapter data flows |
| Ad Server tab | 🔨 Core build | Improve card + client-ID/secret form + test connection (Phases 4–6) |
| Products | 🔨 Core build | Product-config form with placement/package pickers (Phase 5) |
| Media Buys | 🔨 Core build | Deal create/status/update via Unified Deals API (Phase 1) |
| Inventory bundles / Browse / Sync / Targeting criteria | ⚠️ GAM-hardcoded | Improve views or generalization (**H3/H4**) |
| Reports | ⚠️ GAM-hardcoded | Improve reporting view from Report API (**H1**) |
| Buyer Agents + Buyer routing + checklist advertiser items | ⚠️ GAM-hardcoded | Buying-entity/seat/advertiser routing for Improve (**H2**) |
| GAM order import (`Order-…` rows) | 💡 Optional | Improve deal-import projection if wanted (**H5**) |

**Also needed, not on any screen:** OAuth2 client credentials from the Improve
Digital team (blocker **B2**) — without them nothing can be tested.
