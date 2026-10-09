# Procure Radar via GitHub Actions

## What works now

The included workflow `.github/workflows/procure-radar-sync.yml` runs hourly and:

1. restores the SQLite snapshot from the previous successful workflow run;
2. pulls recent 44-FZ purchases for Bashkortostan (`region=2`) via Gosplan API;
3. fetches recent full cards once to obtain both party identity and structured city evidence;
4. recognizes Sterlitamak by verified customer identity **or** structured purchase fields such as GAR delivery address / OKTMO;
5. ranks concrete deals and builds sourcing dossiers;
6. stores the updated SQLite database and analyst artifacts as a GitHub Actions artifact.

## Gosplan access

Default: `https://v2test.gosplan.info` (no API key, 10 requests/minute).

For production, add repository secrets:

- `GOSPLAN_BASE_URL=https://v2.gosplan.info`
- `GOSPLAN_API_KEY=<key>`

The code passes the key in the `apikey` header.

## Important limitation: Sterlitamak vs Bashkortostan

The current live collector filters by Gosplan/KLADR region code. `2` means Bashkortostan. It does not yet directly filter the API query to Sterlitamak.

Recommended next step:

- ingest region 2;
- resolve customer INNs into the local `organizations` table;
- enrich organizations from FNS/MSP data, including `address`;
- build a Sterlitamak customer-INN allowlist and filter purchases locally.

This is more robust than string-matching purchase descriptions for the city name.

## State storage

GitHub-hosted runners are ephemeral. The workflow restores the previous SQLite snapshot from the latest successful run artifact, then uploads the new snapshot.

For large history backfills, use either:

- a self-hosted runner with a DB path outside the checkout directory, or
- external object/database storage.

Do not rely on GitHub cache as the only durable copy of the procurement database.


## Sterlitamak locality filter

The workflow now builds an evidence-backed Sterlitamak customer allowlist after each regional sync.

1. `locality-enrich-identities` fetches up to 50 recent full purchase cards once, regardless of whether a regional buyer already has a legal address.
2. Full EIS/Gosplan party dictionaries are mined only when an address is colocated with the same INN.
3. `locality-refresh` classifies customer INNs by the canonical organization address. Name-only matches are stored with confidence 0.65 and excluded from the default export.
4. `config/sterlitamak-customers.txt` is a trusted manual bootstrap/fallback for verified INNs.
5. `locality-purchases` writes `data/sterlitamak-purchases.json` and `.csv`.

This makes the collector converge over time without repeatedly spending API calls on identities already checked.


## Individual deal screening

The sync workflow now also runs `locality-shortlist` after the Sterlitamak feed is built.
This is deliberately a *first-money triage*, not a profitability claim. Each concrete
purchase receives an explainable 0..100 score from:

- ticket-size fit for a small operator;
- deadline runway;
- procurement-method accessibility;
- commercial tractability of the dominant OKPD2/KTRU segment;
- line-item data clarity;
- number-of-lines complexity;
- strength of locality evidence.

Hard rejects include completed/cancelled procedures, passed deadlines and procurement
methods that are not open competitive entry points. Regulated medical/pharma categories
and construction works are risk-capped so they cannot outrank simple commodity supply
without a qualified partner.

Artifacts:

- `sterlitamak-deal-shortlist.json` — full explainable records;
- `sterlitamak-deal-shortlist.csv` — analyst-friendly flat export;
- `sterlitamak-deal-shortlist.md` — top candidates, also written to the GitHub run summary.

The shortlist answers only **what should a human inspect next**. Supplier prices, payment
terms, certificates, guarantees, delivery feasibility and actual gross margin remain
mandatory before bidding.


## Purchase-level locality evidence

A city procurement is not the same thing as a city-registered buyer. Regional buyers can deliver to Sterlitamak while their legal address is elsewhere. The pipeline therefore also stores `purchase_localities` evidence from semantically strong fields in the full EIS card: GAR/delivery addresses, OKTMO/budget jurisdiction and explicit customer names. Attachment filenames and arbitrary free text are not accepted as strong evidence.
