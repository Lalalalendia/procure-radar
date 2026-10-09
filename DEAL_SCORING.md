# Individual procurement deal screening

This layer ranks **concrete open purchases**, not market categories.

Its purpose is narrow: identify which Sterlitamak purchase a human should inspect next in the search for first revenue. It does **not** claim that a purchase is profitable or bid-ready.

## Hard rejection

A purchase is not eligible when any of these are true:

- the procedure is completed or cancelled;
- the application deadline has passed;
- the procurement method is not an open competitive entry point supported by the classifier.

Hard-rejected records are capped at score 24 and do not enter the default shortlist (`--min-score 55`).

## Score components

The 0..100 score combines:

| Component | Weight | Meaning |
|---|---:|---|
| Ticket fit | 22% | Material enough to matter, but not immediately dominated by financing/guarantee burden |
| Commercial tractability | 18% | Standard/IT goods are easier first-money targets than regulated goods or execution-heavy works |
| Data clarity | 17% | How complete item codes, quantities, prices and amounts are |
| Deadline runway | 15% | Enough time to source and prepare a bid without making stale opportunities look urgent |
| Method access | 12% | Quote/auction vs more complex competitive procedures |
| Line complexity | 10% | Fewer line items are cheaper to source and validate |
| Locality evidence | 6% | Strength of Sterlitamak customer identity evidence |

## Risk caps

The first-money queue intentionally caps categories that require qualifications or specialized delivery:

- pharmaceuticals: max 64;
- medical diagnostics: max 68;
- medical equipment: max 72;
- construction works: max 70.

These can still be good markets. The cap only prevents them from outranking simple commodity supply before a qualified partner exists.

## Tiers

- `A` (>=80): pursue first;
- `B` (>=68): strong manual review;
- `C` (>=55): review if capacity allows;
- `D` (<55): skip by default.

## CLI

```bash
python -m procure_radar.cli locality-shortlist \
  --key sterlitamak \
  --min-confidence 0.9 \
  --min-score 55 \
  --json-out data/sterlitamak-deal-shortlist.json \
  --csv-out data/sterlitamak-deal-shortlist.csv \
  --md-out data/sterlitamak-deal-shortlist.md \
  --db data/procure_radar.sqlite3
```

## Mandatory checks after shortlist

Before bidding, a human or a later sourcing layer still has to prove:

1. supplier availability and actual purchase price;
2. delivery feasibility before the contractual deadline;
3. certificates / declarations / licenses where required;
4. payment terms and cash gap;
5. bid/contract guarantees;
6. taxes, logistics and gross margin;
7. no hidden specification or brand-equivalence blocker.

The score is therefore a **queueing decision**, not an expected-value calculation.

## Sourcing-ready dossiers

For the top A/B candidates the workflow also builds a compact deal dossier. The dossier includes:

- buyer identity and address;
- deadline, max price and score;
- execution mode and risk flags;
- contract-guarantee fields when present;
- every collected line item with OKPD2/KTRU, quantity, unit, unit price and amount;
- a fixed next-work-unit checklist for supplier sourcing, documents, payment terms and go/no-go.

CLI:

```bash
python -m procure_radar.cli locality-dossiers \
  --key sterlitamak \
  --min-confidence 0.9 \
  --min-score 68 \
  --max-deals 10 \
  --json-out data/sterlitamak-deal-dossiers.json \
  --md-out data/sterlitamak-deal-dossiers.md \
  --db data/procure_radar.sqlite3
```

The dossier deliberately stops before claiming supplier availability or margin. Those are the next evidence gates.
