from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from .analytics import (
    compute_buyer_profile,
    compute_buyer_supplier_relationships,
    compute_manufacturing_opportunities,
    compute_opportunities,
    compute_supplier_profile,
    compute_winner_bundles,
    compute_winner_concentration,
    method_stats,
    outcome_stats,
)
from .bulk import ingest_region_batch
from .classification import (
    classify_purchase_type,
    competition_exclusion_reason,
    lifecycle_from_docs,
    select_competition_protocol,
)
from .client import GosplanClient
from .contract_history import contract_history_status, ingest_contract_history_sharded
from .db import connect
from .deal_screening import export_deal_shortlist, locality_deal_shortlist
from .deal_dossier import export_deal_dossiers, locality_deal_dossiers
from .entry_opportunity import procurement_contract_coverage, procurement_entry_opportunities
from .forward_opportunities import forward_opportunities, sync_forward_sources
from .regional_demand import (
    regional_demand_buyers,
    regional_demand_competitors,
    regional_manufacturer_demand,
)
from .history import history_status, ingest_region_history_sharded
from .gisp import import_registry_xlsx, okpd2_stats as gisp_okpd2_stats, registry_stats as gisp_registry_stats
from .fsa import (
    FsaClient,
    column_search as fsa_column_search,
    registry_stats as fsa_registry_stats,
    known_detail_match_report as fsa_known_detail_match_report,
    sync_certificate_details as fsa_sync_certificate_details,
    sync_certificates as fsa_sync_certificates,
    sync_tnved_nsi as fsa_sync_tnved_nsi,
    tnved_stats as fsa_tnved_stats,
    upsert_certificate_detail as fsa_upsert_certificate_detail,
)
from .ingest import ingest_contract, ingest_procedure, ingest_purchase, ingest_tender_protocol, ingest_tenderplan
from .outcomes import classify_protocol_outcome, protocol_weakness
from .locality import (
    enrich_purchase_identities,
    export_locality_purchases,
    locality_purchases,
    read_inn_file,
    refresh_locality_memberships,
)
from .organization import (
    backfill_organizations,
    import_fns_msp,
    import_fns_revexp,
    list_organizations,
    organization_profile,
    organization_stats,
)
from .party_intelligence import (
    backfill_contract_parties,
    resolve_regional_demand_party_names_live,
)
from .rzn import (
    RznClient,
    okpd2_registry_stats,
    registry_stats,
    sync_nsi_records,
    sync_registry,
    upsert_nsi_record,
)
from .segmentation import SEGMENTS, segment_codes

RowIngestor = Callable[[Any, dict[str, Any]], int]


def _parse_param(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"--param must be KEY=VALUE, got: {value}")
        key, val = value.split("=", 1)
        result[key] = val
    return result


def _load_rows(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return [payload]
    if not isinstance(payload, list):
        raise SystemExit("JSON must contain an object or an array of objects")
    rows = [row for row in payload if isinstance(row, dict)]
    if len(rows) != len(payload):
        raise SystemExit("JSON array contains non-object elements")
    return rows


def _probe_rows(rows: list[dict[str, Any]], output: str | None) -> None:
    print(f"rows={len(rows)}")
    if not rows:
        return
    print("top_level_keys:")
    print("\n".join(sorted(rows[0].keys())))
    if output:
        Path(output).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved={output}")
    else:
        print(json.dumps(rows[0], ensure_ascii=False, indent=2)[:12000])


def cmd_probe(args: argparse.Namespace) -> None:
    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    rows = client.get_purchases(limit=args.limit, skip=args.skip, extra=_parse_param(args.param))
    _probe_rows(rows, args.output)


def cmd_probe_endpoint(args: argparse.Namespace) -> None:
    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    rows = client.get_rows(args.endpoint, limit=args.limit, skip=args.skip, extra=_parse_param(args.param))
    _probe_rows(rows, args.output)


def _save_probe_payload(payload: Any, output: str | None) -> None:
    if isinstance(payload, list):
        print(f"rows={len(payload)}")
        if payload and isinstance(payload[0], dict):
            print("first_item_keys:")
            print("\n".join(sorted(payload[0].keys())))
    elif isinstance(payload, dict):
        print("top_level_keys:")
        print("\n".join(sorted(payload.keys())))
    else:
        print(f"payload_type={type(payload).__name__}")

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
        print(f"saved={output}")
    else:
        print(rendered[:20000])


def cmd_probe_protocols(args: argparse.Namespace) -> None:
    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    payload = client.get_purchase_protocols(args.purchase_number)
    _save_probe_payload(payload, args.output)


def cmd_probe_result(args: argparse.Namespace) -> None:
    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    payload = client.get_purchase_result(args.purchase_number)
    _save_probe_payload(payload, args.output)


def cmd_probe_purchase_chain(args: argparse.Namespace) -> None:
    import httpx

    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = [
        ("purchase.json", client.get_purchase, False),
        ("protocols.json", client.get_purchase_protocols, False),
        ("result.json", client.get_purchase_result, True),
    ]

    for name, getter, optional in targets:
        path = out_dir / name
        try:
            payload = getter(args.purchase_number)
        except httpx.HTTPStatusError as exc:
            if optional and exc.response.status_code == 404:
                path.write_text("null\n", encoding="utf-8")
                print(f"{name}: not found (404), saved null -> {path}")
                continue
            raise

        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if isinstance(payload, list):
            shape = f"list[{len(payload)}]"
        elif isinstance(payload, dict):
            shape = f"object[{len(payload)} keys]"
        else:
            shape = type(payload).__name__
        print(f"{name}: {shape} -> {path}")


def _ingest_live(args: argparse.Namespace, endpoint: str, ingestor: RowIngestor) -> None:
    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    conn = connect(args.db)
    total = 0
    try:
        for page in range(args.pages):
            skip = args.skip + page * args.limit
            rows = client.get_rows(endpoint, limit=args.limit, skip=skip, extra=_parse_param(args.param))
            if not rows:
                break
            for row in rows:
                ingestor(conn, row)
                total += 1
            conn.commit()
            print(f"endpoint={endpoint} page={page + 1} skip={skip} rows={len(rows)} total={total}")
            if len(rows) < args.limit:
                break
    finally:
        conn.close()
    print(f"done endpoint={endpoint} total={total} db={args.db}")


def cmd_ingest(args: argparse.Namespace) -> None:
    _ingest_live(args, "/fz44/purchases", ingest_purchase)


def cmd_ingest_procedures(args: argparse.Namespace) -> None:
    _ingest_live(args, "/fz44/procedures", ingest_procedure)


def cmd_ingest_contracts(args: argparse.Namespace) -> None:
    _ingest_live(args, "/fz44/contracts", ingest_contract)


def _ingest_file(args: argparse.Namespace, ingestor: RowIngestor) -> None:
    rows = _load_rows(args.path)
    conn = connect(args.db)
    try:
        for row in rows:
            ingestor(conn, row)
        conn.commit()
    finally:
        conn.close()
    print(f"done total={len(rows)} db={args.db}")


def cmd_ingest_file(args: argparse.Namespace) -> None:
    _ingest_file(args, ingest_purchase)


def cmd_ingest_procedures_file(args: argparse.Namespace) -> None:
    _ingest_file(args, ingest_procedure)


def cmd_ingest_contracts_file(args: argparse.Namespace) -> None:
    _ingest_file(args, ingest_contract)


def cmd_ingest_protocols_file(args: argparse.Namespace) -> None:
    _ingest_file(args, ingest_tender_protocol)


def cmd_ingest_protocols(args: argparse.Namespace) -> None:
    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    payload = client.get_purchase_protocols(args.purchase_number)
    rows = payload if isinstance(payload, list) else [payload]
    rows = [row for row in rows if isinstance(row, dict)]
    conn = connect(args.db)
    try:
        for row in rows:
            ingest_tender_protocol(conn, row)
        conn.commit()
    finally:
        conn.close()
    print(f"done purchase_number={args.purchase_number} protocols={len(rows)} db={args.db}")


def cmd_stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        summary = conn.execute(
            """
            SELECT COUNT(*) AS purchases,
                   COUNT(DISTINCT region_code) AS regions,
                   COALESCE(SUM(max_price), 0) AS total_nmck,
                   COUNT(CASE WHEN max_price IS NOT NULL THEN 1 END) AS with_price,
                   COUNT(CASE WHEN object_info IS NOT NULL THEN 1 END) AS with_object_info
            FROM purchases
            """
        ).fetchone()
        assert summary is not None
        result = dict(summary)
        result["okpd2_codes"] = conn.execute(
            "SELECT COUNT(*) FROM purchase_codes WHERE system='okpd2'"
        ).fetchone()[0]
        result["ktru_codes"] = conn.execute(
            "SELECT COUNT(*) FROM purchase_codes WHERE system='ktru'"
        ).fetchone()[0]
        result["customers"] = conn.execute(
            "SELECT COUNT(DISTINCT inn) FROM purchase_parties WHERE role='customer'"
        ).fetchone()[0]
        result["procedures"] = conn.execute("SELECT COUNT(*) FROM contract_procedures").fetchone()[0]
        result["contracts"] = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        result["contract_suppliers"] = conn.execute(
            "SELECT COUNT(DISTINCT inn) FROM contract_suppliers"
        ).fetchone()[0]
        result["tender_protocols"] = conn.execute(
            "SELECT COUNT(*) FROM tender_protocols"
        ).fetchone()[0]
        result["tender_applications"] = conn.execute(
            "SELECT COUNT(*) FROM tender_applications"
        ).fetchone()[0]
        result["one_bid_protocols"] = conn.execute(
            "SELECT COUNT(*) FROM tender_protocols WHERE applications_count=1"
        ).fetchone()[0]
        result["abandoned_protocols"] = conn.execute(
            "SELECT COUNT(*) FROM tender_protocols WHERE is_abandoned=1"
        ).fetchone()[0]
        result["purchase_items"] = conn.execute(
            "SELECT COUNT(*) FROM purchase_items"
        ).fetchone()[0]
        result["purchase_details"] = conn.execute(
            "SELECT COUNT(*) FROM purchase_detail_fetches WHERE status='ok'"
        ).fetchone()[0]
        print(result)

        for item in conn.execute(
            """
            SELECT p.purchase_number, p.published_at, p.max_price, p.region_code,
                   p.stage, p.object_info,
                   (SELECT COUNT(*) FROM purchase_codes c
                    WHERE c.purchase_id=p.id AND c.system='okpd2') AS okpd2_count,
                   (SELECT COUNT(*) FROM purchase_codes c
                    WHERE c.purchase_id=p.id AND c.system='ktru') AS ktru_count
            FROM purchases p
            ORDER BY p.max_price DESC
            LIMIT ?
            """,
            (args.limit,),
        ):
            print(dict(item))
    finally:
        conn.close()


def cmd_link_stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM purchases) AS purchases,
                (SELECT COUNT(*) FROM contract_procedures) AS procedures,
                (SELECT COUNT(*) FROM contracts) AS contracts,
                (SELECT COUNT(*) FROM contract_procedures cp
                 JOIN purchases p ON p.purchase_number=cp.purchase_number) AS procedures_linked,
                (SELECT COUNT(*) FROM contracts c
                 JOIN purchases p ON p.purchase_number=c.purchase_number) AS contracts_linked
            """
        ).fetchone()
        assert counts is not None
        print(dict(counts))

        rows = conn.execute(
            """
            SELECT p.purchase_number,
                   p.max_price AS nmck,
                   c.price AS contract_price,
                   CASE
                     WHEN p.max_price > 0 AND c.price IS NOT NULL
                     THEN ROUND((p.max_price - c.price) / p.max_price * 100.0, 2)
                   END AS discount_pct,
                   c.reg_num,
                   c.subject,
                   c.customer_inn
            FROM purchases p
            JOIN contracts c ON c.purchase_number=p.purchase_number
            ORDER BY ABS(COALESCE((p.max_price - c.price) / NULLIF(p.max_price, 0), 0)) DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        for row in rows:
            print(dict(row))
    finally:
        conn.close()


def cmd_show(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        row = conn.execute(
            "SELECT * FROM purchases WHERE purchase_number=?", (args.purchase_number,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"purchase not found: {args.purchase_number}")
        result = dict(row)
        result["customers"] = [
            x[0]
            for x in conn.execute(
                "SELECT inn FROM purchase_parties WHERE purchase_id=? AND role='customer' ORDER BY inn",
                (row["id"],),
            )
        ]
        result["okpd2"] = [
            x[0]
            for x in conn.execute(
                "SELECT code FROM purchase_codes WHERE purchase_id=? AND system='okpd2' ORDER BY code",
                (row["id"],),
            )
        ]
        result["ktru"] = [
            x[0]
            for x in conn.execute(
                "SELECT code FROM purchase_codes WHERE purchase_id=? AND system='ktru' ORDER BY code",
                (row["id"],),
            )
        ]
        result["procedures"] = [
            dict(x)
            for x in conn.execute(
                "SELECT * FROM contract_procedures WHERE purchase_number=? ORDER BY published_at",
                (args.purchase_number,),
            )
        ]
        result["protocols"] = [
            dict(x)
            for x in conn.execute(
                "SELECT * FROM tender_protocols WHERE purchase_number=? ORDER BY published_at",
                (args.purchase_number,),
            )
        ]
        result["items"] = [
            dict(x)
            for x in conn.execute(
                """
                SELECT pi.item_key, pi.name, pi.ktru_code, pi.okpd2_code,
                       pi.quantity, pi.unit_name, pi.unit_price, pi.amount
                FROM purchase_items pi
                JOIN purchases p ON p.id=pi.purchase_id
                WHERE p.purchase_number=?
                ORDER BY pi.amount DESC, pi.id
                """,
                (args.purchase_number,),
            )
        ]
        result["contracts"] = [
            dict(x)
            for x in conn.execute(
                "SELECT * FROM contracts WHERE purchase_number=? ORDER BY published_at",
                (args.purchase_number,),
            )
        ]
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()



def cmd_competition(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        purchase = conn.execute(
            """
            SELECT purchase_number, max_price, object_info, region_code, purchase_type, stage
            FROM purchases WHERE purchase_number=?
            """,
            (args.purchase_number,),
        ).fetchone()
        if purchase is None:
            raise SystemExit(f"purchase not found: {args.purchase_number}")

        doc_types = [
            row["doc_type"]
            for row in conn.execute(
                """
                SELECT pd.doc_type
                FROM purchase_documents pd
                JOIN purchases p ON p.id=pd.purchase_id
                WHERE p.purchase_number=?
                ORDER BY pd.published_at, pd.id
                """,
                (args.purchase_number,),
            )
            if row["doc_type"]
        ]
        rows = conn.execute(
            """
            SELECT tp.id, tp.doc_type, tp.published_at, tp.applications_count, tp.admitted_count,
                   tp.rejected_count, tp.final_price, tp.is_abandoned,
                   tp.abandoned_reason_code, tp.abandoned_reason_name,
                   CASE WHEN p.max_price > 0 AND tp.final_price IS NOT NULL
                        THEN ROUND((p.max_price - tp.final_price) / p.max_price * 100.0, 2)
                   END AS discount_pct,
                   CASE WHEN tp.applications_count = 1 THEN 1 ELSE 0 END AS single_application
            FROM tender_protocols tp
            JOIN purchases p ON p.purchase_number=tp.purchase_number
            WHERE tp.purchase_number=?
            ORDER BY tp.published_at, tp.id
            """,
            (args.purchase_number,),
        ).fetchall()

        result = dict(purchase)
        method = classify_purchase_type(purchase["purchase_type"])
        exclusion = competition_exclusion_reason(
            purchase["purchase_type"], doc_types=doc_types
        )
        lifecycle = lifecycle_from_docs(purchase["stage"], doc_types)
        selected = select_competition_protocol(purchase["purchase_type"], rows)
        result["method"] = method.code
        result["method_label"] = method.label
        result["lifecycle"] = lifecycle
        result["competition_eligible"] = exclusion is None
        result["competition_exclusion_reason"] = exclusion
        result["documents"] = doc_types
        result["protocols"] = [dict(row) for row in rows]
        result["selected_protocol"] = dict(selected) if selected is not None else None
        if exclusion is None and selected is not None:
            outcome = classify_protocol_outcome(selected)
            weakness = protocol_weakness(selected)
            result["competition_outcome"] = outcome.code
            result["competition_outcome_label"] = outcome.label
            result["competition_weakness_score"] = round(weakness, 3)
            result["competition_weak"] = weakness >= 0.6
        else:
            result["competition_outcome"] = None
            result["competition_outcome_label"] = None
            result["competition_weakness_score"] = None
            result["competition_weak"] = None
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_method_stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = method_stats(conn, region_code=args.region)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("no purchases for region")
        return
    for row in rows:
        eligible = "yes" if row["competition_eligible"] else "no"
        print(
            f"{row['purchase_type'] or '<missing>'}: purchases={row['purchases']} "
            f"method={row['method']} eligible={eligible} "
            f"eligible_rows={row['eligible_purchases']} cancelled={row['cancelled']} "
            f"final_candidates={row['final_candidates']} "
            f"competition_protocols={row['competition_protocols']} "
            f"coverage={row['protocol_coverage']:.0%} stages={row['stage_counts']} "
            f"lifecycle={row['lifecycle_counts']}"
        )
        if row["exclusion_reason"]:
            print(f"  excluded_from_competition={row['exclusion_reason']}")


def cmd_outcome_stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = outcome_stats(conn, region_code=args.region)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"selected_protocols={result['selected_protocols']} "
        f"median_applications={result['median_applications']} "
        f"median_admitted={result['median_admitted']}"
    )
    print(f"methods={result['methods']}")
    print(f"outcomes={result['outcomes']}")
    if result['reason_codes']:
        print("abandoned_reason_codes:")
        for row in result['reason_codes']:
            print(f"  {row['code']}: count={row['count']} name={row['name']!r}")


def cmd_locality_enrich_identities(args: argparse.Namespace) -> None:
    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    conn = connect(args.db)
    try:
        result = enrich_purchase_identities(
            conn,
            client,
            region_code=args.region,
            max_requests=args.max_requests,
            request_delay=args.request_delay,
            refresh=args.refresh,
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))


def cmd_locality_refresh(args: argparse.Namespace) -> None:
    manual = read_inn_file(args.manual_inn_file)
    manual.extend(args.manual_inn or [])
    conn = connect(args.db)
    try:
        result = refresh_locality_memberships(
            conn,
            locality_key=args.key,
            locality_name=args.name,
            aliases=args.alias or [args.name],
            region_code=args.region,
            manual_inns=manual,
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))


def cmd_locality_purchases(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = locality_purchases(
            conn,
            locality_key=args.key,
            min_confidence=args.min_confidence,
            limit=args.limit,
        )
    finally:
        conn.close()
    export_locality_purchases(rows, json_path=args.json_out, csv_path=args.csv_out)
    summary = {
        "locality_key": args.key,
        "purchases": len(rows),
        "json_out": args.json_out,
        "csv_out": args.csv_out,
    }
    print(json.dumps(summary, ensure_ascii=False))
    if args.json and not args.json_out:
        print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_locality_shortlist(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = locality_deal_shortlist(
            conn,
            locality_key=args.key,
            min_confidence=args.min_confidence,
            min_score=args.min_score,
            limit=args.limit,
        )
    finally:
        conn.close()
    export_deal_shortlist(
        rows,
        json_path=args.json_out,
        csv_path=args.csv_out,
        markdown_path=args.md_out,
    )
    summary = {
        "locality_key": args.key,
        "candidates": len(rows),
        "pursue": sum(1 for row in rows if row.get("decision") == "pursue"),
        "review": sum(1 for row in rows if row.get("decision") == "review"),
        "skip": sum(1 for row in rows if row.get("decision") == "skip"),
        "json_out": args.json_out,
        "csv_out": args.csv_out,
        "md_out": args.md_out,
    }
    print(json.dumps(summary, ensure_ascii=False))
    if args.json and not args.json_out:
        print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_locality_dossiers(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = locality_deal_dossiers(
            conn,
            locality_key=args.key,
            min_confidence=args.min_confidence,
            min_score=args.min_score,
            max_deals=args.max_deals,
        )
    finally:
        conn.close()
    export_deal_dossiers(rows, json_path=args.json_out, markdown_path=args.md_out)
    print(json.dumps({
        "locality_key": args.key,
        "dossiers": len(rows),
        "json_out": args.json_out,
        "md_out": args.md_out,
    }, ensure_ascii=False))
    if args.json and not args.json_out:
        print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_ingest_region(args: argparse.Namespace) -> None:
    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    conn = connect(args.db)
    try:
        stats = ingest_region_batch(
            conn,
            client,
            region_code=args.region,
            limit=args.limit,
            pages=args.pages,
            skip=args.skip,
            extra=_parse_param(args.param),
            fetch_details=not args.no_details,
            max_details=args.max_details,
            request_delay=args.request_delay,
            refresh_details=args.refresh_details,
            protocol_fallback=not args.no_protocol_fallback,
        )
    finally:
        conn.close()
    print(f"done region={args.region} db={args.db} stats={stats}")


def cmd_ingest_contract_history(args: argparse.Namespace) -> None:
    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    conn = connect(args.db)
    continuous = bool(args.continuous)
    pages = None if continuous else args.pages
    rate_per_minute = args.rate_per_minute
    if continuous and rate_per_minute is None and args.request_delay is None:
        rate_per_minute = 9.0
    try:
        result = ingest_contract_history_sharded(
            conn,
            client,
            region_code=args.region,
            since=args.since,
            until=args.until,
            limit=args.limit,
            pages=pages,
            extra=_parse_param(args.param),
            request_delay=args.request_delay,
            rate_per_minute=rate_per_minute,
            resume_overlap_pages=args.resume_overlap_pages,
            restart=args.restart,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"contract-history status={result['status']} stop={result.get('stop_reason')} "
        f"region={args.region} since={result['since']} until={result['until']} "
        f"pages={result['pages_scanned']} rows={result['rows_seen']} "
        f"contracts={result['contracts_ingested']} next_skip={result['next_skip']} "
        f"shards={result.get('shards_completed', 0)} splits={result.get('shard_splits', 0)}"
    )
    if result.get("current_shard"):
        print(f"current_shard={result['current_shard']}")
    print(f"checkpoint={result['checkpoint_key']}")
    progress = result.get("progress") or {}
    rpm = progress.get("avg_requests_per_minute")
    rpm_text = f"{rpm:.2f}" if rpm is not None else "-"
    print(
        f"progress api_requests={progress.get('api_requests', 0)} "
        f"avg_rpm={rpm_text} elapsed={progress.get('elapsed') or '-'}"
    )
    if result.get("stop_reason") == "page_budget":
        print("Run the same command again to resume from the checkpoint.")


def cmd_contract_history_status(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = contract_history_status(conn, region_code=args.region)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("no contract-history checkpoints")
        return
    for row in rows:
        state = "complete" if row["completed"] else "paused"
        print(
            f"{state} mode={row.get('mode')} region={row['region_code']} "
            f"since={row['since_date']} until={row['until_date']} "
            f"next_skip={row['next_skip']} pages={row['pages_scanned']} "
            f"rows={row['rows_seen']} oldest={row['oldest_published_at']} "
            f"updated={row['updated_at']}"
        )
        print(f"  checkpoint={row['checkpoint_key']}")


def cmd_ingest_history(args: argparse.Namespace) -> None:
    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    conn = connect(args.db)
    continuous = bool(args.continuous)
    pages = None if continuous else args.pages
    max_details = None if continuous or args.max_details == 0 else args.max_details
    rate_per_minute = args.rate_per_minute
    if continuous and rate_per_minute is None and args.request_delay is None:
        rate_per_minute = 9.0
    try:
        result = ingest_region_history_sharded(
            conn,
            client,
            region_code=args.region,
            since=args.since,
            until=args.until,
            limit=args.limit,
            pages=pages,
            extra=_parse_param(args.param),
            fetch_details=not args.no_details,
            max_details=max_details,
            request_delay=args.request_delay,
            rate_per_minute=rate_per_minute,
            refresh_details=args.refresh_details,
            protocol_fallback=not args.no_protocol_fallback,
            resume_overlap_pages=args.resume_overlap_pages,
            restart=args.restart,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"history status={result['status']} stop={result.get('stop_reason')} "
        f"mode={result.get('mode', 'legacy')} region={args.region} "
        f"since={result['since']} until={result['until']} "
        f"pages={result['pages_scanned']} rows={result['rows_seen']} "
        f"window_rows={result['rows_in_window']} next_skip={result['next_skip']} "
        f"shards={result.get('shards_completed', 0)} splits={result.get('shard_splits', 0)} "
        f"out_of_order={result.get('out_of_order_rows', 0)}"
    )
    if result.get("current_shard"):
        print(f"current_shard={result['current_shard']}")
    print(f"checkpoint={result['checkpoint_key']}")
    progress = result.get("progress") or {}
    rpm = progress.get("avg_requests_per_minute")
    rpm_text = f"{rpm:.2f}" if rpm is not None else "-"
    print(
        f"progress api_requests={progress.get('api_requests', 0)} "
        f"avg_rpm={rpm_text} elapsed={progress.get('elapsed') or '-'} "
        f"eta={progress.get('eta') or '-'}"
    )
    if result.get("stop_reason") == "interrupted":
        print("Ctrl+C handled safely; run the same command again to resume from the checkpoint.")
    print(f"stats={result['stats']}")


def cmd_history_status(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = history_status(conn, region_code=args.region)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("no history checkpoints")
        return
    for row in rows:
        state = "complete" if row["completed"] else "paused"
        print(
            f"{state} mode={row.get('mode', 'legacy')} region={row['region_code']} since={row['since_date']} "
            f"until={row['until_date']} next_skip={row['next_skip']} "
            f"pages={row['pages_scanned']} rows={row['rows_seen']} "
            f"window_rows={row['rows_in_window']} details={row['details_fetched']} "
            f"oldest={row['oldest_published_at']} updated={row['updated_at']}"
        )
        print(f"  checkpoint={row['checkpoint_key']}")


def cmd_opportunities(args: argparse.Namespace) -> None:
    min_procurements = 1 if args.new_signals else args.min_procurements
    max_procurements = 1 if args.new_signals else None
    conn = connect(args.db)
    try:
        rows = compute_opportunities(
            conn,
            region_code=args.region,
            min_procurements=min_procurements,
            min_protocol_coverage=args.min_protocol_coverage,
            max_procurements=max_procurements,
            segment=args.segment,
            group=args.group,
            gap_type=args.gap_type,
        )
    finally:
        conn.close()

    rows = rows[: args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print(
            "no ranked categories; collect more full purchase details or lower "
            "--min-procurements / --min-protocol-coverage"
        )
        return
    for idx, row in enumerate(rows, 1):
        gisp_applicability = row.get("gisp_applicability", "unknown")
        top1_winner = row.get("top1_supplier_share")
        top1_winner_text = f"{top1_winner:.0%}" if top1_winner is not None else "n/a"
        if gisp_applicability == "not_applicable":
            gisp_summary = "n/a"
        elif gisp_applicability in {"unknown", "unavailable"}:
            gisp_summary = gisp_applicability
        else:
            gisp_summary = (
                f"{row.get('gisp_active_products', 0)}/"
                f"{row.get('gisp_active_manufacturers', 0)}"
            )
        distribution_score = row.get("distribution_gap_score")
        distribution_text = (
            f"{distribution_score:.2f}" if distribution_score is not None else "n/a"
        )
        median_discount = row.get("median_discount_pct")
        median_discount_text = (
            f"{median_discount:.2f}%" if median_discount is not None else "n/a"
        )
        print(
            f"{idx:>2}. score={row['score']:>5.2f} {row['system'].upper()}={row['code']} "
            f"segment={row['segment']} gap={row['gap_type']}({row['gap_strength']:.0%}) "
            f"demand={row['demand_rub']:.2f} purchases={row['procurements']} "
            f"buyers={row['buyers_count']} top_buyer={row['top_buyer_share']:.0%} "
            f"supplier_gap={row['supplier_gap_share']:.0%} "
            f"no_bid_gap={row['no_bid_gap_share']:.0%} "
            f"barrier_gap={row['barrier_gap_share']:.0%} "
            f"median_apps={row['median_applications']} "
            f"median_discount={median_discount_text} "
            f"discount_coverage={row.get('discount_coverage', 0.0):.0%} "
            f"protocols={row['protocol_count']}/{row['procurements']} "
            f"coverage={row['protocol_coverage']:.0%} "
            f"exact_amount={row.get('exact_amount_coverage', 0.0):.0%} "
            f"rzn={row.get('rzn_active_products', 0)}/{row.get('rzn_active_producers', 0)} "
            f"rzn_state={row.get('rzn_enrichment', 'unavailable')} "
            f"rzn_match={row.get('rzn_match_state', 'unavailable')} "
            f"gisp={gisp_summary} "
            f"gisp_state={row.get('gisp_enrichment', 'unavailable')} "
            f"gisp_match={row.get('gisp_match_state', 'unavailable')} "
            f"gisp_applicability={gisp_applicability} "
            f"distribution_gap={distribution_text} "
            f"contract_cov={row.get('winner_purchase_coverage', 0.0):.0%} "
            f"winners={row.get('known_contract_suppliers') if row.get('known_contract_suppliers') is not None else 'unknown'} "
            f"top1_winner={top1_winner_text} "
            f"winner_match={row.get('supplier_concentration_match', 'unavailable')} "
            f"winner_code_cov={row.get('winner_code_match_coverage', 0.0):.0%} "
            f"winner_evidence={row.get('supplier_concentration_evidence', 'none')} "
            f"label={row['label']!r}"
        )


def cmd_manufacturing_opportunities(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = compute_manufacturing_opportunities(
            conn,
            region_code=args.region,
            min_procurements=args.min_procurements,
            min_protocol_coverage=args.min_protocol_coverage,
            segment=args.segment,
            include_unknown_gisp=args.include_unknown_gisp,
        )
    finally:
        conn.close()

    rows = rows[: args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print(
            "no manufacturing opportunities with usable GISP coverage; use "
            "--include-unknown-gisp to inspect unmatched goods markets"
        )
        return
    for idx, row in enumerate(rows, 1):
        mfg_score = row.get("manufacturer_gap_score")
        distribution_score = row.get("distribution_gap_score")
        demand_per_mfr = row.get("demand_per_manufacturer")
        mfg_text = f"{mfg_score:.2f}" if mfg_score is not None else "unknown"
        distribution_text = (
            f"{distribution_score:.2f}" if distribution_score is not None else "n/a"
        )
        demand_per_mfr_text = (
            f"{demand_per_mfr:.2f}" if demand_per_mfr is not None else "unknown"
        )
        known_suppliers = row.get("known_contract_suppliers")
        top1_winner = row.get("top1_supplier_share")
        top1_winner_text = f"{top1_winner:.0%}" if top1_winner is not None else "n/a"
        known_suppliers_text = (
            str(known_suppliers) if known_suppliers is not None else "unknown"
        )
        print(
            f"{idx:>2}. manufacturer_gap={mfg_text} OKPD2={row['code']} "
            f"segment={row['segment']} demand={row['demand_rub']:.2f} "
            f"purchases={row['procurements']} buyers={row['buyers_count']} "
            f"known_suppliers={known_suppliers_text} "
            f"contract_suppliers={row.get('contract_supplier_enrichment', 'unavailable')} "
            f"gisp={row.get('gisp_active_products', 0)}/{row.get('gisp_active_manufacturers', 0)} "
            f"demand_per_manufacturer={demand_per_mfr_text} "
            f"supplier_gap={row['supplier_gap_share']:.0%} "
            f"distribution_gap={distribution_text} "
            f"contract_cov={row.get('winner_purchase_coverage', 0.0):.0%} "
            f"top1_winner={top1_winner_text} "
            f"winner_match={row.get('supplier_concentration_match', 'unavailable')} "
            f"winner_code_cov={row.get('winner_code_match_coverage', 0.0):.0%} "
            f"winner_evidence={row.get('supplier_concentration_evidence', 'none')} "
            f"amount_confidence={row.get('demand_confidence', 0.0):.0%} "
            f"gisp_applicability={row.get('gisp_applicability', 'unknown')} "
            f"label={row['label']!r}"
        )




def cmd_relationship_radar(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = compute_buyer_supplier_relationships(
            conn,
            region_code=args.region,
            min_contracts=args.min_contracts,
            min_value_rub=args.min_value,
            min_buyer_share=args.min_buyer_share,
            min_supplier_dependency=args.min_supplier_dependency,
        )
    finally:
        conn.close()

    rows = rows[: args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print(
            "no buyer-supplier relationships matched the thresholds; lower --min-contracts / "
            "--min-value / share thresholds or ingest more contracts"
        )
        return

    for idx, row in enumerate(rows, 1):
        print(
            f"{idx:>2}. score={row['relationship_score']:.2f} "
            f"buyer={row['buyer_inn']} supplier={row['supplier_inn']} "
            f"type={row['relationship_type']} contracts={row['contracts']} "
            f"purchases={row['purchases']} value={row['pair_value_rub']:.2f} "
            f"buyer_share={row['buyer_share']:.0%} "
            f"supplier_dep={row['supplier_dependency']:.0%} "
            f"mutual_dep={row['mutual_dependency']:.0%} "
            f"buyer_contracts={row['buyer_region_contracts']} "
            f"supplier_contracts={row['supplier_region_contracts']} "
            f"value_cov={row['pair_value_coverage']:.0%} "
            f"basis={row['buyer_share_basis']}/{row['supplier_dependency_basis']} "
            f"evidence={row['evidence']}"
        )


def cmd_buyer_profile(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        profile = compute_buyer_profile(
            conn,
            customer_inn=args.inn,
            region_code=args.region,
            limit=args.limit,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(profile, ensure_ascii=False, indent=2))
        return
    if profile["contracts"] == 0:
        print(f"buyer={profile['customer_inn']} contracts=0 region={profile['region_code']}")
        return

    top_supplier = profile.get("top_supplier_inn") or "unknown"
    top_share = profile.get("top_supplier_share")
    top_share_text = f"{float(top_share):.0%}" if top_share is not None else "n/a"
    hhi = profile.get("supplier_hhi")
    hhi_text = f"{float(hhi):.0f}" if hhi is not None else "n/a"
    print(
        f"buyer={profile['customer_inn']} region={profile['region_code']} "
        f"contracts={profile['contracts']} purchases={profile['purchases']} suppliers={profile['suppliers']} "
        f"gross_value={profile['gross_contract_value_rub']:.2f} value_cov={profile['value_coverage']:.0%} "
        f"multi_supplier={profile['multi_supplier_contracts']} top_supplier={top_supplier} "
        f"top_supplier_share={top_share_text} supplier_hhi={hhi_text} "
        f"repeat_supplier={profile['repeat_supplier_contract_share']:.0%} "
        f"ktru={profile['ktru_codes']} okpd2={profile['okpd2_codes']} "
        f"code_allocation=equal_split"
    )

    suppliers = profile.get("suppliers_ranked") or []
    if suppliers:
        print("suppliers:")
        for idx, row in enumerate(suppliers, 1):
            buyer_share = row.get("buyer_share")
            supplier_dep = row.get("supplier_dependency_on_buyer")
            mutual = row.get("mutual_dependency")
            print(
                f"  {idx}. inn={row['supplier_inn']} contracts={row['contracts']} "
                f"purchases={row['purchases']} value={row['buyer_attributed_value_rub']:.2f} "
                f"buyer_share={float(buyer_share):.0%} " if buyer_share is not None else
                f"  {idx}. inn={row['supplier_inn']} contracts={row['contracts']} "
                f"purchases={row['purchases']} value={row['buyer_attributed_value_rub']:.2f} buyer_share=n/a "
            , end="")
            dep_text = f"{float(supplier_dep):.0%}" if supplier_dep is not None else "n/a"
            mutual_text = f"{float(mutual):.0%}" if mutual is not None else "n/a"
            print(
                f"supplier_dep={dep_text} mutual_dep={mutual_text} "
                f"supplier_region_contracts={row['supplier_region_contracts']} "
                f"basis={row['share_basis']}"
            )

    for key, title in (("okpd2_markets", "okpd2"), ("ktru_markets", "ktru")):
        rows = profile.get(key) or []
        if not rows:
            continue
        print(f"{title}:")
        for idx, row in enumerate(rows, 1):
            print(
                f"  {idx}. code={row['code']} contracts={row['contracts']} purchases={row['purchases']} "
                f"buyer_value={row['buyer_value_rub']:.2f} value_cov={row['value_coverage']:.0%}"
            )

    recent = profile.get("recent_contracts") or []
    if recent:
        print("recent_contracts:")
        for idx, row in enumerate(recent, 1):
            price = "n/a" if row.get("price") is None else f"{float(row['price']):.2f}"
            subject = str(row.get("subject") or "")
            if len(subject) > 120:
                subject = subject[:117] + "..."
            supplier_preview = ",".join(row.get("suppliers") or []) or "-"
            print(
                f"  {idx}. reg={row.get('reg_num') or '-'} purchase={row.get('purchase_number') or '-'} "
                f"price={price} suppliers={supplier_preview} ktru={row.get('ktru_count', 0)} "
                f"okpd2={row.get('okpd2_count', 0)} subject={subject!r}"
            )


def cmd_supplier_profile(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        profile = compute_supplier_profile(
            conn,
            supplier_inn=args.inn,
            region_code=args.region,
            limit=args.limit,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(profile, ensure_ascii=False, indent=2))
        return
    if profile["contracts"] == 0:
        print(f"supplier={profile['supplier_inn']} contracts=0 region={profile['region_code']}")
        return

    top_customer = profile.get("top_customer_inn") or "unknown"
    top_customer_share = profile.get("top_customer_share")
    top_customer_text = f"{float(top_customer_share):.0%}" if top_customer_share is not None else "n/a"
    hhi = profile.get("customer_hhi")
    hhi_text = f"{float(hhi):.0f}" if hhi is not None else "n/a"
    print(
        f"supplier={profile['supplier_inn']} region={profile['region_code']} "
        f"contracts={profile['contracts']} purchases={profile['purchases']} customers={profile['customers']} "
        f"gross_value={profile['gross_contract_value_rub']:.2f} "
        f"supplier_value={profile['supplier_attributed_value_rub']:.2f} "
        f"value_cov={profile['value_coverage']:.0%} customer_cov={profile['customer_coverage']:.0%} "
        f"multi_supplier={profile['multi_supplier_contracts']} "
        f"top_customer={top_customer} top_customer_share={top_customer_text} "
        f"customer_hhi={hhi_text} repeat_customer={profile['repeat_customer_contract_share']:.0%} "
        f"ktru={profile['ktru_codes']} okpd2={profile['okpd2_codes']} "
        f"code_allocation=equal_split"
    )

    bundles = profile.get("bundles") or []
    if bundles:
        print("bundles:")
        for idx, row in enumerate(bundles, 1):
            codes = list(row.get("codes") or [])
            preview = ",".join(codes[:4])
            if len(codes) > 4:
                preview += f",+{len(codes) - 4}"
            print(
                f"  {idx}. bundle={row['bundle_id']} system={str(row['system']).upper()} "
                f"contracts={row['contracts']} codes={row['codes_count']} "
                f"supplier_value={row['supplier_value_rub']:.2f} "
                f"gross_value={row['gross_contract_value_rub']:.2f} codes={preview}"
            )

    customers = profile.get("customers_ranked") or []
    if customers:
        print("customers:")
        for idx, row in enumerate(customers, 1):
            share = row.get("share")
            share_text = f"{float(share):.0%}" if share is not None else "n/a"
            print(
                f"  {idx}. inn={row['customer_inn']} contracts={row['contracts']} "
                f"purchases={row['purchases']} value={row['supplier_value_rub']:.2f} "
                f"share={share_text} basis={row['share_basis']}"
            )

    for key, title in (("okpd2_markets", "okpd2"), ("ktru_markets", "ktru")):
        rows = profile.get(key) or []
        if not rows:
            continue
        print(f"{title}:")
        for idx, row in enumerate(rows, 1):
            bundle = row.get("bundle_id") or "-"
            print(
                f"  {idx}. code={row['code']} contracts={row['contracts']} "
                f"purchases={row['purchases']} supplier_value={row['supplier_value_rub']:.2f} "
                f"value_cov={row['value_coverage']:.0%} bundle={bundle}"
            )

    recent = profile.get("recent_contracts") or []
    if recent:
        print("recent_contracts:")
        for idx, row in enumerate(recent, 1):
            price = "n/a" if row.get("price") is None else f"{float(row['price']):.2f}"
            supplier_value = (
                "n/a" if row.get("supplier_value_rub") is None else f"{float(row['supplier_value_rub']):.2f}"
            )
            subject = str(row.get("subject") or "")
            if len(subject) > 120:
                subject = subject[:117] + "..."
            print(
                f"  {idx}. reg={row.get('reg_num') or '-'} purchase={row.get('purchase_number') or '-'} "
                f"customer={row.get('customer_inn') or '-'} price={price} supplier_value={supplier_value} "
                f"suppliers={row.get('supplier_count', 0)} ktru={row.get('ktru_count', 0)} "
                f"okpd2={row.get('okpd2_count', 0)} subject={subject!r}"
            )

def cmd_winner_bundles(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = compute_winner_bundles(
            conn,
            region_code=args.region,
            min_procurements=args.min_procurements,
            min_protocol_coverage=args.min_protocol_coverage,
            min_contract_coverage=args.min_contract_coverage,
            min_contracts=args.min_contracts,
            min_markets=args.min_markets,
            segment=args.segment,
            group=args.group,
        )
    finally:
        conn.close()

    rows = rows[: args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print(
            "no repeated contract-evidence bundles; lower --min-markets / "
            "--min-contract-coverage or ingest more contracts"
        )
        return
    for idx, row in enumerate(rows, 1):
        top1 = float(row.get("top1_supplier_share") or 0.0)
        top3 = float(row.get("top3_supplier_share") or top1)
        hhi = float(row.get("supplier_hhi") or 0.0)
        codes = list(row.get("codes") or [])
        code_preview = ",".join(codes[:4])
        if len(codes) > 4:
            code_preview += f",+{len(codes) - 4}"
        print(
            f"{idx:>2}. bundle={row['bundle_id']} system={str(row['system']).upper()} "
            f"markets={row['markets_count']} contracts={row['contracts']} "
            f"winner={row.get('top_supplier_inn') or 'unknown'} "
            f"top1={top1:.0%} top3={top3:.0%} hhi={hhi:.0f} "
            f"bundle_value={row.get('winner_bundle_value_rub', 0.0):.2f} "
            f"allocated_per_market={row.get('winner_allocated_value_per_market_rub', 0.0):.2f} "
            f"contract_cov={row.get('min_market_contract_coverage', 0.0):.0%}-"
            f"{row.get('max_market_contract_coverage', 0.0):.0%} "
            f"supplier_gap={row.get('min_supplier_gap_share', 0.0):.0%}-"
            f"{row.get('max_supplier_gap_share', 0.0):.0%} "
            f"code_cov={row.get('winner_code_match_coverage', 0.0):.0%} "
            f"evidence={row.get('supplier_concentration_evidence', 'none')} "
            f"codes={code_preview}"
        )


def cmd_winner_concentration(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = compute_winner_concentration(
            conn,
            region_code=args.region,
            min_procurements=args.min_procurements,
            min_protocol_coverage=args.min_protocol_coverage,
            min_contract_coverage=args.min_contract_coverage,
            min_contracts=args.min_contracts,
            segment=args.segment,
            group=args.group,
        )
    finally:
        conn.close()

    rows = rows[: args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print(
            "no markets with enough linked contract-winner evidence; lower "
            "--min-contract-coverage / --min-contracts or ingest more contracts"
        )
        return
    for idx, row in enumerate(rows, 1):
        top1 = float(row["top1_supplier_share"])
        top3 = float(row["top3_supplier_share"]) if row.get("top3_supplier_share") is not None else top1
        hhi = float(row["supplier_hhi"]) if row.get("supplier_hhi") is not None else 0.0
        print(
            f"{idx:>2}. {row['system'].upper()}={row['code']} "
            f"demand={row['demand_rub']:.2f} purchases={row['procurements']} "
            f"contracts={row.get('known_contracts', 0)} "
            f"contract_cov={row.get('winner_purchase_coverage', 0.0):.0%} "
            f"winners={row.get('known_contract_suppliers', 0)} "
            f"top1={top1:.0%} top3={top3:.0%} hhi={hhi:.0f} "
            f"top1_inn={row.get('top_supplier_inn') or 'unknown'} "
            f"basis={row.get('supplier_concentration_basis') or 'n/a'} "
            f"match={row.get('supplier_concentration_match', 'unavailable')} "
            f"code_cov={row.get('winner_code_match_coverage', 0.0):.0%} "
            f"observed_value={row.get('winner_observed_value_rub', 0.0):.2f} "
            f"exact_contracts={row.get('winner_exact_contracts', 0)} "
            f"fallback_contracts={row.get('winner_fallback_contracts', 0)} "
            f"value_coverage={row.get('winner_value_coverage', 0.0):.0%} "
            f"evidence={row.get('supplier_concentration_evidence', 'none')} "
            f"supplier_gap={row['supplier_gap_share']:.0%} label={row['label']!r}"
        )


def cmd_repair_protocol_prices(args: argparse.Namespace) -> None:
    """Recompute stored protocol final prices from normalized applications."""
    conn = connect(args.db)
    checked = 0
    changed = 0
    cleared = 0
    try:
        protocols = conn.execute(
            "SELECT id, final_price FROM tender_protocols ORDER BY id"
        ).fetchall()
        for protocol in protocols:
            applications = conn.execute(
                """
                SELECT final_price, admitted, app_rating
                FROM tender_applications
                WHERE protocol_id=?
                """,
                (protocol["id"],),
            ).fetchall()
            if not applications:
                continue
            checked += 1
            admitted = [
                row
                for row in applications
                if row["admitted"] == 1 and row["final_price"] is not None
            ]
            if admitted:
                rated = [row for row in admitted if row["app_rating"] is not None]
                if rated:
                    winner = min(
                        rated,
                        key=lambda row: (int(row["app_rating"]), float(row["final_price"])),
                    )
                else:
                    winner = min(admitted, key=lambda row: float(row["final_price"]))
                new_price = float(winner["final_price"])
            elif any(row["admitted"] is not None for row in applications):
                new_price = None
            else:
                # No admission signal: preserve the existing protocol value.
                continue

            old_price = protocol["final_price"]
            same = (
                old_price is None and new_price is None
            ) or (
                old_price is not None
                and new_price is not None
                and abs(float(old_price) - float(new_price)) < 1e-9
            )
            if same:
                continue
            conn.execute(
                "UPDATE tender_protocols SET final_price=? WHERE id=?",
                (new_price, protocol["id"]),
            )
            changed += 1
            if new_price is None:
                cleared += 1
        conn.commit()
    finally:
        conn.close()
    print(
        f"protocol-price-repair checked={checked} changed={changed} "
        f"cleared_rejected_only={cleared} db={args.db}"
    )


def cmd_rzn_search(args: argparse.Namespace) -> None:
    client = RznClient(base_url=args.base_url, cookie=args.cookie, verify_ssl=not args.insecure)
    payload = client.search_med_products(
        text_search=args.query,
        legal_system=args.legal_system,
        page=args.page,
        size=args.size,
        status_ids=(1,) if args.active_only else None,
    )
    if args.output:
        Path(args.output).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"saved={args.output}")
        return
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(
        f"total={payload.get('totalElements')} pages={payload.get('totalPages')} "
        f"page={payload.get('number')} size={payload.get('size')}"
    )
    for row in payload.get("content", []):
        if not isinstance(row, dict):
            continue
        status = row.get("status") if isinstance(row.get("status"), dict) else {}
        producer = row.get("producer") if isinstance(row.get("producer"), dict) else {}
        print(
            f"{row.get('id')} RU={row.get('noRu')!r} date={row.get('dateRu')} "
            f"status={status.get('name')!r} producer={producer.get('name')!r} "
            f"name={row.get('name')!r}"
        )


def cmd_rzn_sync(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        client = RznClient(base_url=args.base_url, cookie=args.cookie, verify_ssl=not args.insecure)
        result = sync_registry(
            conn,
            client,
            legal_system=args.legal_system,
            text_search=args.query,
            page_size=args.size,
            pages=None if args.continuous else args.pages,
            request_delay=args.request_delay,
            active_only=args.active_only,
            restart=args.restart,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "rzn status={status} stop={stop_reason} next_page={next_page} "
            "rows={rows} pages={pages} total={total_elements}".format(**result)
        )


def cmd_rzn_nsi_get(args: argparse.Namespace) -> None:
    client = RznClient(base_url=args.base_url, cookie=args.cookie, verify_ssl=not args.insecure)
    payload = client.get_nsi_record(args.record_id)
    if args.db:
        conn = connect(args.db)
        try:
            upsert_nsi_record(conn, payload)
            conn.commit()
        finally:
            conn.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_rzn_nsi_sync(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        client = RznClient(base_url=args.base_url, cookie=args.cookie, verify_ssl=not args.insecure)
        result = sync_nsi_records(
            conn,
            client,
            limit=None if args.continuous else args.limit,
            request_delay=args.request_delay,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"rzn-nsi resolved={result['resolved_this_run']} "
            f"pending={result['pending_after']} complete={result['complete']}"
        )


def cmd_rzn_okpd2(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = okpd2_registry_stats(
            conn,
            args.code,
            active_only=not args.all_statuses,
            limit=args.limit,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"OKPD2={result['okpd2_code']} active_only={result['active_only']} "
        f"products={result['products']} producers={result['producers']} "
        f"representatives={result['representatives']}"
    )
    for row in result['sample_products']:
        print(
            f"  {row['external_id']} RU={row['registration_number']!r} "
            f"date={row['registration_date']} producer={row['producer_name']!r} "
            f"name={row['name']!r}"
        )
    if result['top_producers']:
        print('top_producers:')
        for row in result['top_producers']:
            print(f"  {row['products']:>4} {row['producer']}")


def cmd_rzn_stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        stats = registry_stats(conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return
    print(
        f"products={stats['products']} classifier_links={stats['classifier_links']} "
        f"nsi_records={stats['nsi_records']} unresolved_nsi={stats['unresolved_nsi']} "
        f"producers={stats['producers']} representatives={stats['representatives']}"
    )
    print(f"legal_systems={stats['legal_systems']}")
    print(f"statuses={stats['statuses']}")
    print(f"nsi_catalogs={stats.get('nsi_catalogs', {})}")
    for row in stats["sync"]:
        print(
            f"sync key={row['sync_key']!r} next_page={row['next_page']} "
            f"rows={row['rows_seen']} total={row['total_elements']} completed={row['completed']}"
        )


def cmd_gisp_import(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = import_registry_xlsx(
            conn,
            args.path,
            scope=args.scope,
            sheet_name=args.sheet,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"gisp imported={result['rows_imported']} seen={result['rows_seen']} "
        f"skipped={result['rows_skipped']} stale_deleted={result['stale_deleted']} "
        f"stale_rows_deleted={result.get('stale_rows_deleted', 0)} "
        f"scope={result['scope']} sheet={result['sheet']!r} header_row={result['header_row']}"
    )
    if result['skipped_reasons']:
        print(f"skipped_reasons={result['skipped_reasons']}")


def cmd_gisp_stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        stats = gisp_registry_stats(conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return
    print(
        f"registry_records={stats['products']} registry_rows={stats.get('registry_rows', 0)} "
        f"active_products={stats['active_products']} manufacturers={stats['manufacturers']} "
        f"okpd2_codes={stats['okpd2_codes']}"
    )
    print(f"latest_import={stats['latest_import']}")


def cmd_gisp_okpd2(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = gisp_okpd2_stats(
            conn, args.code, active_only=not args.all_statuses, limit=args.limit
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"OKPD2={result['okpd2_code']} active_only={result['active_only']} "
        f"products={result['products']} manufacturers={result['manufacturers']}"
    )
    for row in result['sample_products']:
        print(
            f"  reg={row['registry_number']!r} inn={row['manufacturer_inn']!r} "
            f"manufacturer={row['manufacturer_name']!r} valid_until={row['valid_until']} "
            f"product={row['product_name']!r}"
        )


def _fsa_filter_overrides(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    path = Path(value)
    raw = path.read_text(encoding="utf-8") if path.exists() else value
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise SystemExit("--filter-json must be a JSON object or a path to one")
    return payload


def _fsa_search_columns(args: argparse.Namespace) -> list[dict[str, Any]] | None:
    if not getattr(args, "query", None):
        return None
    return [fsa_column_search(args.search_field, args.query)]


def _fsa_client(args: argparse.Namespace) -> FsaClient:
    return FsaClient(
        base_url=args.base_url,
        token=args.token,
        cookie=args.cookie,
        username=args.username,
        password=args.password,
        verify_ssl=not args.insecure,
    )


def cmd_fsa_search(args: argparse.Namespace) -> None:
    client = _fsa_client(args)
    payload = client.search_certificates(
        page=args.page,
        size=args.size,
        active_only=not args.all_statuses,
        columns_search=_fsa_search_columns(args),
        filter_overrides=_fsa_filter_overrides(args.filter_json),
    )
    if args.output:
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved={args.output}")
        return
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    rows = payload.get("items") or []
    print(f"total={payload.get('total')} page={args.page} requested_size={args.size} rows={len(rows)}")
    for row in rows:
        if not isinstance(row, dict):
            continue
        print(
            f"{row.get('id')} cert={row.get('number')!r} date={row.get('date')} "
            f"origin={row.get('productOrig')!r} applicant={row.get('applicantName')!r} "
            f"manufacturer={row.get('manufacterName')!r} product={row.get('productFullName')!r}"
        )


def cmd_fsa_get(args: argparse.Namespace) -> None:
    client = _fsa_client(args)
    payload = client.get_certificate(args.certificate_id)
    if args.db:
        conn = connect(args.db)
        try:
            fsa_upsert_certificate_detail(conn, payload)
            conn.commit()
        finally:
            conn.close()
    if args.output:
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved={args.output}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_fsa_sync(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = fsa_sync_certificates(
            conn,
            _fsa_client(args),
            page_size=args.size,
            pages=None if args.continuous else args.pages,
            request_delay=args.request_delay,
            active_only=not args.all_statuses,
            columns_search=_fsa_search_columns(args),
            filter_overrides=_fsa_filter_overrides(args.filter_json),
            restart=args.restart,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "fsa status={status} stop={stop_reason} next_page={next_page} "
            "rows={rows} pages={pages} total={total_elements}".format(**result)
        )


def cmd_fsa_detail_sync(args: argparse.Namespace) -> None:
    def show_progress(row: dict[str, Any]) -> None:
        if args.json:
            return
        print(
            f"fsa-details batch={row['batch']} selected={row['selected']} "
            f"fetched={row['fetched']} errors={row['errors']} "
            f"total_fetched={row['fetched_total']} pending={row['pending']}"
        )

    conn = connect(args.db)
    try:
        result = fsa_sync_certificate_details(
            conn,
            _fsa_client(args),
            limit=args.limit,
            request_delay=args.request_delay,
            certificate_ids=args.id or None,
            continuous=args.continuous,
            known_only=args.known_only,
            known_region_code=args.region,
            progress=show_progress if args.continuous else None,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"fsa-details scope={result.get('scope', 'all')} "
            f"selected={result['selected']} fetched={result['fetched']} "
            f"errors={result['errors']} batches={result['batches']} "
            f"pending={result['pending_after']} complete={result['complete']}"
        )
        if result.get("scope") == "known_only":
            print(
                "  known_candidates={known_candidates_total} "
                "known_pending={known_candidates_pending} "
                "manufacturer_candidates={known_manufacturer_candidates} "
                "applicant_only_candidates={known_applicant_only_candidates} "
                "matched_organizations={known_match_organizations}".format(**result)
            )
        if result["error_samples"]:
            for row in result["error_samples"][:5]:
                print(f"  id={row['certificate_id']} error={row['error']}")


def cmd_fsa_tnved_sync(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = fsa_sync_tnved_nsi(
            conn,
            _fsa_client(args),
            batch_size=args.batch_size,
            batches=None if args.continuous else args.batches,
            request_delay=args.request_delay,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"fsa-tnved resolved={result['resolved_this_run']} batches={result['batches']} "
            f"unresolved={result['unresolved']}"
        )


def cmd_fsa_stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        stats = fsa_registry_stats(conn, region_code=args.region)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return
    print(
        f"certificates={stats['certificates']} details={stats['details']} "
        f"pending_details={stats['pending_details']} coverage={stats['detail_coverage_pct']}% "
        f"applicant_inns={stats['applicant_inns']} manufacturer_inns={stats['manufacturer_inns']} "
        f"tnved_links={stats['tnved_links']} tnved_records={stats['tnved_records']} "
        f"unresolved_tnved={stats['unresolved_tnved']}"
    )
    print(f"latest_sync={stats['latest_sync']}")



def cmd_fsa_known_matches(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = fsa_known_detail_match_report(
            conn,
            limit=args.limit,
            validation=None if args.validation == "all" else args.validation,
            region_code=args.region,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"matching_details={result['matching_details']} verified={result['verified']} "
        f"mismatch={result['mismatch']} missing_inn={result['missing_inn']} "
        f"validation_rate={result['validation_rate_pct']}%"
    )
    for row in result["rows"]:
        expected = ",".join(item["inn"] for item in row["matched_known"])
        print(
            f"  id={row['external_id']} validation={row['validation']} "
            f"party={row['match_party']} expected={expected or '-'} "
            f"official_inn={row['official_inn'] or '-'} "
            f"summary={row['summary_name']!r} official={row['official_name']!r}"
        )

def cmd_fsa_tnved(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = fsa_tnved_stats(conn, args.code, limit=args.limit)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"TNVED={result['tnved_code']} certificates={result['certificates']} "
        f"applicants={result['applicants']} manufacturers={result['manufacturers']}"
    )
    for row in result['sample']:
        print(
            f"  cert={row['number']!r} applicant={row['applicant_name']!r} "
            f"inn={row['applicant_inn']!r} phone={row['applicant_phone']!r} "
            f"email={row['applicant_email']!r} manufacturer={row['manufacturer_name']!r} "
            f"product={row['product_full_name']!r}"
        )



def cmd_organizations_backfill(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = backfill_organizations(conn)
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result)


def cmd_contract_parties_backfill(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        backfill_organizations(conn)
        result = backfill_contract_parties(
            conn,
            region_code=args.region,
            year=args.year,
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result)


def cmd_regional_demand_parties_resolve(args: argparse.Namespace) -> None:
    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    conn = connect(args.db)
    try:
        result = resolve_regional_demand_party_names_live(
            conn,
            client=client,
            manufacturer_inn=args.inn,
            region_code=args.region,
            year=args.year,
            max_requests=args.max_requests,
            rate_per_minute=args.rate_per_minute,
            refresh=args.refresh,
            emit=None if args.json else print,
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result)


def cmd_fns_msp_import(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        if args.known_only:
            backfill_organizations(conn)
        result = import_fns_msp(
            conn,
            args.path,
            known_only=args.known_only,
            region_code=args.region,
            commit_every=args.commit_every,
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result)


def cmd_fns_revexp_import(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        if args.known_only:
            backfill_organizations(conn)
        result = import_fns_revexp(
            conn,
            args.path,
            known_only=args.known_only,
            year=args.year,
            commit_every=args.commit_every,
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result)


def cmd_organizations_list(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = list_organizations(
            conn,
            msp_only=args.msp,
            financials_only=args.financials,
            role=args.role,
            region_code=args.region,
            year=args.year,
            manufacturer_source=args.manufacturer_source,
            limit=args.limit,
            offset=args.offset,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"total={result['total']} shown={len(result['rows'])} offset={result['offset']}")
    for row in result["rows"]:
        print(
            f"{row['inn']}\t{row['name'] or '-'}\tregion={row['region_code']} "
            f"msp={row['msp_category'] or '-'} employees={row['employee_count']} "
            f"financial_year={row['financial_year']} revenue={row['revenue']} "
            f"activity_year={row['activity_year']} "
            f"supplier_contracts_year={row['supplier_contracts_year']} "
            f"supplier_contract_value_year_rub={row['supplier_contract_value_year_rub']} "
            f"supplier_contracts_all={row['supplier_contracts_all']} "
            f"supplier_contract_value_all_rub={row['supplier_contract_value_all_rub']} "
            f"manufacturer_source={row['manufacturer_source'] or '-'} "
            f"manufacturer_confidence={row['manufacturer_confidence']}"
        )


def cmd_organization_show(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = organization_profile(conn, args.inn)
    finally:
        conn.close()
    if result is None:
        raise SystemExit(f"organization not found: {args.inn}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_organization_stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = organization_stats(conn)
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result)


def cmd_procurement_coverage(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = procurement_contract_coverage(conn, region_code=args.region, year=args.year)
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result)




def cmd_regional_demand(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        coverage = procurement_contract_coverage(conn, region_code=args.region, year=args.year)
        result = regional_manufacturer_demand(
            conn,
            manufacturer_inn=args.inn,
            region_code=args.region,
            year=args.year,
            history_confidence=float(coverage["confidence"]),
            history_status=str(coverage["status"]),
        )
        result["contract_history_coverage"] = coverage
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"inn={result['inn']} region={result['region_code']} year={result['year']} "
        f"status={result['match_status']} demand={result['regional_demand_value_year_rub']} "
        f"contracts={result['regional_demand_contracts_year']} "
        f"buyers={result['regional_demand_buyers_year']} "
        f"addressable={result['addressable_market_rub']} "
        f"market_share={result['manufacturer_regional_market_share_year']}"
    )


def cmd_regional_demand_buyers(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        coverage = procurement_contract_coverage(conn, region_code=args.region, year=args.year)
        result = regional_demand_buyers(
            conn,
            manufacturer_inn=args.inn,
            region_code=args.region,
            year=args.year,
            history_confidence=float(coverage["confidence"]),
            history_status=str(coverage["status"]),
            limit=args.limit,
            offset=args.offset,
            contracts_per_buyer=args.contracts_per_buyer,
        )
        result["contract_history_coverage"] = coverage
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    concentration = result["buyer_concentration"]
    print(
        f"inn={result['inn']} region={result['region_code']} year={result['year']} "
        f"addressable={result['addressable_market_rub']} buyers={result['addressable_buyers']} "
        f"top5={concentration['top_5_buyers_value_rub']} "
        f"top5_share={concentration['top_5_share_pct']}% hhi={concentration['hhi_10000']}"
    )
    for row in result["rows"]:
        print(
            f"{row['buyer_inn']}\t{row['buyer_name'] or '-'}\t"
            f"addressable={row['addressable_value_rub']} "
            f"contracts={row['addressable_contracts']} "
            f"share={row['share_of_addressable_market_pct']}% "
            f"switch_rate={row['supplier_switch_rate']} "
            f"contestability={row['contestability_score']} "
            f"opportunity={row['buyer_opportunity_score']}"
        )


def cmd_regional_demand_competitors(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        coverage = procurement_contract_coverage(conn, region_code=args.region, year=args.year)
        result = regional_demand_competitors(
            conn,
            manufacturer_inn=args.inn,
            region_code=args.region,
            year=args.year,
            history_confidence=float(coverage["confidence"]),
            history_status=str(coverage["status"]),
            limit=args.limit,
            offset=args.offset,
        )
        result["contract_history_coverage"] = coverage
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    concentration = result["competitor_concentration"]
    print(
        f"inn={result['inn']} region={result['region_code']} year={result['year']} "
        f"addressable={result['addressable_market_rub']} competitors={result['total']} "
        f"top5={concentration['top_5_competitors_value_rub']} "
        f"top5_share={concentration['top_5_share_pct']}% hhi={concentration['hhi_10000']}"
    )
    for row in result["rows"]:
        print(
            f"{row['supplier_inn']}\t{row['supplier_name'] or '-'}\t"
            f"value={row['matched_value_rub']} contracts={row['matched_contracts']} "
            f"buyers={row['buyers']} share={row['share_of_addressable_market_pct']}%"
        )


def cmd_forward_opportunities(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = forward_opportunities(
            conn,
            manufacturer_inn=args.inn,
            region_code=args.region,
            as_of=args.as_of,
            history_year=args.history_year,
            current_limit=args.current_limit,
            planned_limit=args.planned_limit,
            recent_notice_days=args.recent_notice_days,
            planned_horizon_days=args.planned_horizon_days,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"inn={result['manufacturer_inn']} region={result['region_code']} as_of={result['as_of']} "
        f"current={result['current_opportunities_total']} current_value={result['current_opportunities_value_rub']} "
        f"planned={result['planned_opportunities_total']} planned_value={result['planned_opportunities_value_rub']}"
    )
    for row in result["current_opportunities"]:
        print(
            f"OPEN\t{row['purchase_number']}\t{row['buyer_name'] or '-'}\t"
            f"value={row['estimated_addressable_value_rub']} deadline={row['collecting_finished_at'] or '-'} "
            f"score={row['evidenced_forward_opportunity_score']} tier={row['forward_opportunity_tier']} "
            f"{row['object_info'] or '-'}"
        )
    for row in result["planned_opportunities"]:
        print(
            f"PLAN\t{row['position_number'] or row['plan_number']}\t{row['buyer_name'] or '-'}\t"
            f"value={row['estimated_addressable_value_rub']} planned={row['planned_at'] or '-'} "
            f"score={row['evidenced_forward_opportunity_score']} tier={row['forward_opportunity_tier']} "
            f"{row['object_info'] or '-'}"
        )


def cmd_forward_opportunities_sync(args: argparse.Namespace) -> None:
    client = GosplanClient(base_url=args.base_url, api_key=args.api_key)
    conn = connect(args.db)
    try:
        result = sync_forward_sources(
            conn,
            client,
            manufacturer_inn=args.inn,
            region_code=args.region,
            history_year=args.history_year,
            purchase_pages=args.purchase_pages,
            tenderplan_pages=args.tenderplan_pages,
            page_size=args.page_size,
            max_requests=args.max_requests,
            rate_per_minute=args.rate_per_minute,
            refresh_details=args.refresh,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        f"inn={result['manufacturer_inn']} region={result['region_code']} requests={result['network_requests']} "
        f"purchases={result['purchase_rows_ingested']} purchase_details={result['purchase_detail_requests']} "
        f"plans={result['tenderplan_rows_ingested']} plan_details={result['tenderplan_detail_requests']} "
        f"positions={result['tenderplan_positions_ingested']} stop={result['stop_reason']}"
    )


def cmd_procurement_entry_opportunities(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        result = procurement_entry_opportunities(
            conn,
            region_code=args.region,
            year=args.year,
            manufacturer_source=args.manufacturer_source,
            min_score=args.min_score,
            limit=args.limit,
            offset=args.offset,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    coverage = result["coverage"]
    print(
        f"coverage={coverage['status']} confidence={coverage['confidence']} "
        f"scope={result['scope']} total={result['total']}"
    )
    for row in result["rows"]:
        print(
            f"{row['procurement_entry_score']:6.2f}\t{row['procurement_entry_tier']}\t"
            f"{row['inn']}\t{row['name'] or '-'}\t"
            f"revenue={row['revenue']} contracts={row['regional_supplier_contracts_year']} "
            f"value={row['regional_supplier_contract_value_year_rub']} "
            f"demand={row['regional_demand_value_year_rub']} "
            f"addressable={row['addressable_market_rub']} "
            f"buyers={row['regional_demand_buyers_year']} "
            f"manufacturer_source={row['manufacturer_source']}"
        )


def cmd_ui(args: argparse.Namespace) -> None:
    from .ui import launch_ui

    launch_ui(args.db, region_code=args.region)

def cmd_segments(args: argparse.Namespace) -> None:
    del args
    for item in SEGMENTS.values():
        print(f"{item.code}: group={item.group} label={item.label!r}")


def _add_live_ingest_parser(sub: argparse._SubParsersAction, name: str, help_text: str, func: Callable) -> None:
    p = sub.add_parser(name, parents=[_COMMON], help=help_text)
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--db", default="data/procure_radar.sqlite3")
    p.set_defaults(func=func)


def _add_file_ingest_parser(sub: argparse._SubParsersAction, name: str, help_text: str, func: Callable) -> None:
    p = sub.add_parser(name, help=help_text)
    p.add_argument("path")
    p.add_argument("--db", default="data/procure_radar.sqlite3")
    p.set_defaults(func=func)


_COMMON = argparse.ArgumentParser(add_help=False)
_COMMON.add_argument("--base-url", default="https://v2test.gosplan.info")
_COMMON.add_argument("--limit", type=int, default=10)
_COMMON.add_argument("--skip", "--offset", dest="skip", type=int, default=0)
_COMMON.add_argument("--param", action="append", default=[], help="Pass API parameter KEY=VALUE; repeatable")
_COMMON.add_argument("--api-key", help="Gosplan product API key; not needed for v2test")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="procure-radar")
    sub = parser.add_subparsers(required=True)

    probe = sub.add_parser("probe", parents=[_COMMON], help="Inspect the live Gosplan purchase response")
    probe.add_argument("--output")
    probe.set_defaults(func=cmd_probe)

    probe_endpoint = sub.add_parser(
        "probe-endpoint", parents=[_COMMON], help="Inspect any list endpoint before implementing its schema"
    )
    probe_endpoint.add_argument("endpoint")
    probe_endpoint.add_argument("--output")
    probe_endpoint.set_defaults(func=cmd_probe_endpoint)

    probe_protocols = sub.add_parser(
        "probe-protocols", parents=[_COMMON], help="Fetch raw tender protocols for one purchase"
    )
    probe_protocols.add_argument("purchase_number")
    probe_protocols.add_argument("--output")
    probe_protocols.set_defaults(func=cmd_probe_protocols)

    probe_result = sub.add_parser(
        "probe-result", parents=[_COMMON], help="Fetch raw supplier-selection result for one purchase"
    )
    probe_result.add_argument("purchase_number")
    probe_result.add_argument("--output")
    probe_result.set_defaults(func=cmd_probe_result)

    probe_chain = sub.add_parser(
        "probe-purchase-chain",
        parents=[_COMMON],
        help="Fetch full purchase, protocols and result for one purchase",
    )
    probe_chain.add_argument("purchase_number")
    probe_chain.add_argument("--output-dir", default="purchase_probe")
    probe_chain.set_defaults(func=cmd_probe_purchase_chain)

    _add_live_ingest_parser(sub, "ingest", "Fetch and persist purchases", cmd_ingest)
    _add_live_ingest_parser(sub, "ingest-procedures", "Fetch contract-conclusion procedures", cmd_ingest_procedures)
    _add_live_ingest_parser(sub, "ingest-contracts", "Fetch signed contracts", cmd_ingest_contracts)

    _add_file_ingest_parser(sub, "ingest-file", "Persist purchases from saved JSON", cmd_ingest_file)
    _add_file_ingest_parser(sub, "ingest-procedures-file", "Persist contract-conclusion procedures from JSON", cmd_ingest_procedures_file)
    _add_file_ingest_parser(sub, "ingest-contracts-file", "Persist contracts from JSON", cmd_ingest_contracts_file)
    _add_file_ingest_parser(sub, "ingest-protocols-file", "Persist tender protocols from JSON", cmd_ingest_protocols_file)

    ingest_protocols = sub.add_parser(
        "ingest-protocols", parents=[_COMMON], help="Fetch and persist tender protocols for one purchase"
    )
    ingest_protocols.add_argument("purchase_number")
    ingest_protocols.add_argument("--db", default="data/procure_radar.sqlite3")
    ingest_protocols.set_defaults(func=cmd_ingest_protocols)

    ingest_region = sub.add_parser(
        "ingest-region", parents=[_COMMON],
        help="Collect one region; fetch details only for competitive rows advertising a final protocol",
    )
    ingest_region.add_argument("--region", type=int, required=True, help="Gosplan/KLADR region code")
    ingest_region.add_argument("--pages", type=int, default=1)
    ingest_region.add_argument("--db", default="data/procure_radar.sqlite3")
    ingest_region.add_argument(
        "--detail-stage-min", type=int, default=None, help=argparse.SUPPRESS
    )
    ingest_region.add_argument("--max-details", type=int, default=100)
    ingest_region.add_argument(
        "--request-delay", type=float, default=None,
        help="Seconds between API requests; default is 6.2 on v2test and 0.15 elsewhere",
    )
    ingest_region.add_argument("--refresh-details", action="store_true")
    ingest_region.add_argument(
        "--no-protocol-fallback", action="store_true",
        help="Do not call /protocols when a candidate detail omits the advertised final protocol",
    )
    ingest_region.add_argument("--no-details", action="store_true")
    ingest_region.set_defaults(func=cmd_ingest_region)

    locality_identity = sub.add_parser(
        "locality-enrich-identities",
        parents=[_COMMON],
        help="Fetch a bounded set of full purchase cards to learn customer addresses",
    )
    locality_identity.add_argument("--region", type=int, required=True)
    locality_identity.add_argument("--max-requests", type=int, default=20)
    locality_identity.add_argument("--request-delay", type=float, default=None)
    locality_identity.add_argument("--refresh", action="store_true")
    locality_identity.add_argument("--db", default="data/procure_radar.sqlite3")
    locality_identity.set_defaults(func=cmd_locality_enrich_identities)

    locality_refresh = sub.add_parser(
        "locality-refresh",
        help="Build an evidence-backed organization allowlist for one city/locality",
    )
    locality_refresh.add_argument("--key", required=True, help="Stable locality key, e.g. sterlitamak")
    locality_refresh.add_argument("--name", required=True, help="Display name, e.g. Стерлитамак")
    locality_refresh.add_argument("--alias", action="append", default=[], help="Address/name alias; repeatable")
    locality_refresh.add_argument("--region", type=int, help="Optional region guard")
    locality_refresh.add_argument("--manual-inn", action="append", default=[], help="Trusted INN; repeatable")
    locality_refresh.add_argument("--manual-inn-file", help="UTF-8 file with one trusted INN per line")
    locality_refresh.add_argument("--db", default="data/procure_radar.sqlite3")
    locality_refresh.set_defaults(func=cmd_locality_refresh)

    locality_rows = sub.add_parser(
        "locality-purchases",
        help="Export purchases whose customer belongs to a locality allowlist",
    )
    locality_rows.add_argument("--key", required=True)
    locality_rows.add_argument("--min-confidence", type=float, default=0.9)
    locality_rows.add_argument("--limit", type=int, default=1000)
    locality_rows.add_argument("--db", default="data/procure_radar.sqlite3")
    locality_rows.add_argument("--json-out")
    locality_rows.add_argument("--csv-out")
    locality_rows.add_argument("--json", action="store_true")
    locality_rows.set_defaults(func=cmd_locality_purchases)

    locality_shortlist = sub.add_parser(
        "locality-shortlist",
        help="Score concrete locality purchases for first-money manual review",
    )
    locality_shortlist.add_argument("--key", required=True)
    locality_shortlist.add_argument("--min-confidence", type=float, default=0.9)
    locality_shortlist.add_argument("--min-score", type=float, default=55.0)
    locality_shortlist.add_argument("--limit", type=int, default=1000)
    locality_shortlist.add_argument("--db", default="data/procure_radar.sqlite3")
    locality_shortlist.add_argument("--json-out")
    locality_shortlist.add_argument("--csv-out")
    locality_shortlist.add_argument("--md-out")
    locality_shortlist.add_argument("--json", action="store_true")
    locality_shortlist.set_defaults(func=cmd_locality_shortlist)

    locality_dossiers = sub.add_parser(
        "locality-dossiers",
        help="Build sourcing-ready dossiers for the top concrete locality deals",
    )
    locality_dossiers.add_argument("--key", required=True)
    locality_dossiers.add_argument("--min-confidence", type=float, default=0.9)
    locality_dossiers.add_argument("--min-score", type=float, default=68.0)
    locality_dossiers.add_argument("--max-deals", type=int, default=10)
    locality_dossiers.add_argument("--db", default="data/procure_radar.sqlite3")
    locality_dossiers.add_argument("--json-out")
    locality_dossiers.add_argument("--md-out")
    locality_dossiers.add_argument("--json", action="store_true")
    locality_dossiers.set_defaults(func=cmd_locality_dossiers)

    ingest_contract_history = sub.add_parser(
        "ingest-contract-history",
        help="Resumable historical contract backfill using published_at time shards",
    )
    ingest_contract_history.add_argument("--base-url", default="https://v2test.gosplan.info")
    ingest_contract_history.add_argument("--limit", type=int, default=100)
    ingest_contract_history.add_argument(
        "--param", action="append", default=[], help="Pass API parameter KEY=VALUE; repeatable"
    )
    ingest_contract_history.add_argument("--api-key", help="Gosplan product API key; not needed for v2test")
    ingest_contract_history.add_argument("--region", type=int, required=True, help="Gosplan/KLADR region code")
    ingest_contract_history.add_argument("--since", required=True, help="Inclusive lower published_at date, YYYY-MM-DD")
    ingest_contract_history.add_argument("--until", help="Inclusive upper published_at date, YYYY-MM-DD")
    ingest_contract_history.add_argument(
        "--pages", type=int, default=100,
        help="Maximum contract list pages in this run; ignored by --continuous",
    )
    ingest_contract_history.add_argument("--db", default="data/procure_radar.sqlite3")
    ingest_contract_history.add_argument(
        "--continuous", action="store_true",
        help="Run until the date window is complete or Ctrl+C; disables page budget",
    )
    ingest_contract_history.add_argument(
        "--rate-per-minute", type=float,
        help="Conservative request pacing target; --continuous defaults to 9 req/min",
    )
    ingest_contract_history.add_argument(
        "--request-delay", type=float, default=None,
        help="Seconds between API requests; mutually exclusive with --rate-per-minute",
    )
    ingest_contract_history.add_argument(
        "--resume-overlap-pages", type=int, default=1,
        help="On resume, rewind this many pages inside the current shard",
    )
    ingest_contract_history.add_argument(
        "--restart", action="store_true",
        help="Reset this contract-history checkpoint and start again",
    )
    ingest_contract_history.add_argument("--json", action="store_true")
    ingest_contract_history.set_defaults(func=cmd_ingest_contract_history)

    contract_history_state = sub.add_parser(
        "contract-history-status", help="Show resumable contract-history checkpoints"
    )
    contract_history_state.add_argument("--region", type=int)
    contract_history_state.add_argument("--db", default="data/procure_radar.sqlite3")
    contract_history_state.add_argument("--json", action="store_true")
    contract_history_state.set_defaults(func=cmd_contract_history_status)

    ingest_history = sub.add_parser(
        "ingest-history",
        help="Resumable historical region backfill using published_at time shards",
    )
    ingest_history.add_argument("--base-url", default="https://v2test.gosplan.info")
    ingest_history.add_argument("--limit", type=int, default=50)
    ingest_history.add_argument("--param", action="append", default=[], help="Pass API parameter KEY=VALUE; repeatable")
    ingest_history.add_argument("--api-key", help="Gosplan product API key; not needed for v2test")
    ingest_history.add_argument("--region", type=int, required=True, help="Gosplan/KLADR region code")
    ingest_history.add_argument("--since", required=True, help="Inclusive lower published_at date, YYYY-MM-DD")
    ingest_history.add_argument("--until", help="Inclusive upper published_at date, YYYY-MM-DD")
    ingest_history.add_argument(
        "--pages", type=int, default=100,
        help="Maximum list pages to scan in this run; ignored by --continuous",
    )
    ingest_history.add_argument("--db", default="data/procure_radar.sqlite3")
    ingest_history.add_argument(
        "--max-details",
        type=int,
        default=100,
        help="Maximum new detail requests in this run; 0 means unlimited; ignored by --continuous",
    )
    ingest_history.add_argument(
        "--continuous",
        action="store_true",
        help="Run until --since/index exhaustion or Ctrl+C; disables page/detail budgets",
    )
    ingest_history.add_argument(
        "--rate-per-minute",
        type=float,
        help="Conservative request pacing target; --continuous defaults to 9 req/min",
    )
    ingest_history.add_argument(
        "--request-delay",
        type=float,
        default=None,
        help="Seconds between API requests; mutually exclusive with --rate-per-minute",
    )
    ingest_history.add_argument(
        "--resume-overlap-pages",
        type=int,
        default=2,
        help="On resume, rewind this many list pages to protect against shifting remote offsets",
    )
    ingest_history.add_argument("--restart", action="store_true", help="Reset this date/query checkpoint and start from skip=0")
    ingest_history.add_argument("--refresh-details", action="store_true")
    ingest_history.add_argument("--no-protocol-fallback", action="store_true")
    ingest_history.add_argument("--no-details", action="store_true")
    ingest_history.add_argument("--json", action="store_true")
    ingest_history.set_defaults(func=cmd_ingest_history)

    history_state = sub.add_parser("history-status", help="Show resumable historical backfill checkpoints")
    history_state.add_argument("--region", type=int)
    history_state.add_argument("--db", default="data/procure_radar.sqlite3")
    history_state.add_argument("--json", action="store_true")
    history_state.set_defaults(func=cmd_history_status)

    method_audit = sub.add_parser(
        "method-stats",
        help="Audit purchase types, lifecycle and competition eligibility before ranking",
    )
    method_audit.add_argument("--region", type=int, required=True)
    method_audit.add_argument("--db", default="data/procure_radar.sqlite3")
    method_audit.add_argument("--json", action="store_true")
    method_audit.set_defaults(func=cmd_method_stats)

    outcome_audit = sub.add_parser(
        "outcome-stats",
        help="Audit final protocol outcomes and abandoned-reason codes before ranking",
    )
    outcome_audit.add_argument("--region", type=int, required=True)
    outcome_audit.add_argument("--db", default="data/procure_radar.sqlite3")
    outcome_audit.add_argument("--json", action="store_true")
    outcome_audit.set_defaults(func=cmd_outcome_stats)

    opportunities = sub.add_parser("opportunities", help="Rank KTRU/OKPD2 categories from collected line items")
    opportunities.add_argument("--region", type=int, required=True)
    opportunities.add_argument("--db", default="data/procure_radar.sqlite3")
    opportunities.add_argument("--min-procurements", type=int, default=2)
    opportunities.add_argument("--min-protocol-coverage", type=float, default=0.5)
    opportunities.add_argument("--segment", choices=segment_codes())
    opportunities.add_argument("--group", choices=("goods", "property", "works", "services", "other"))
    opportunities.add_argument(
        "--gap-type",
        choices=("supplier_gap", "no_bid_gap", "barrier_gap", "mixed", "competitive_market"),
    )
    opportunities.add_argument(
        "--new-signals", action="store_true",
        help="Show only categories seen in exactly one procurement",
    )
    opportunities.add_argument("--limit", type=int, default=30)
    opportunities.add_argument("--json", action="store_true")
    opportunities.set_defaults(func=cmd_opportunities)

    manufacturing = sub.add_parser(
        "manufacturing-opportunities",
        help="Rank OKPD2 goods markets by procurement demand relative to GISP manufacturers",
    )
    manufacturing.add_argument("--region", type=int, required=True)
    manufacturing.add_argument("--db", default="data/procure_radar.sqlite3")
    manufacturing.add_argument("--min-procurements", type=int, default=2)
    manufacturing.add_argument("--min-protocol-coverage", type=float, default=0.5)
    manufacturing.add_argument("--segment", choices=segment_codes())
    manufacturing.add_argument(
        "--include-unknown-gisp",
        action="store_true",
        help="Also show goods OKPD2 markets with no GISP match; they are not scored as zero manufacturers",
    )
    manufacturing.add_argument("--limit", type=int, default=30)
    manufacturing.add_argument("--json", action="store_true")
    manufacturing.set_defaults(func=cmd_manufacturing_opportunities)
    winners = sub.add_parser(
        "winner-concentration",
        help="rank markets by observed concentration of linked contract winners",
    )
    winners.add_argument("--region", type=int, required=True)
    winners.add_argument("--db", default="data/procure_radar.sqlite3")
    winners.add_argument("--min-procurements", type=int, default=2)
    winners.add_argument("--min-protocol-coverage", type=float, default=0.5)
    winners.add_argument("--min-contract-coverage", type=float, default=0.3)
    winners.add_argument("--min-contracts", type=int, default=2)
    winners.add_argument("--segment", choices=segment_codes())
    winners.add_argument("--group", choices=("goods", "services"), default="goods")
    winners.add_argument("--limit", type=int, default=30)
    winners.add_argument("--json", action="store_true")
    winners.set_defaults(func=cmd_winner_concentration)

    bundles = sub.add_parser(
        "winner-bundles",
        help="collapse markets that share the same linked contract-winner evidence",
    )
    bundles.add_argument("--region", type=int, required=True)
    bundles.add_argument("--db", default="data/procure_radar.sqlite3")
    bundles.add_argument("--min-procurements", type=int, default=2)
    bundles.add_argument("--min-protocol-coverage", type=float, default=0.5)
    bundles.add_argument("--min-contract-coverage", type=float, default=0.3)
    bundles.add_argument("--min-contracts", type=int, default=2)
    bundles.add_argument("--min-markets", type=int, default=2)
    bundles.add_argument("--segment", choices=segment_codes())
    bundles.add_argument("--group", choices=("goods", "services"), default="goods")
    bundles.add_argument("--limit", type=int, default=30)
    bundles.add_argument("--json", action="store_true")
    bundles.set_defaults(func=cmd_winner_bundles)


    relationships = sub.add_parser(
        "relationship-radar",
        help="rank recurring buyer-supplier relationships by mutual observed dependence",
    )
    relationships.add_argument("--region", type=int, required=True)
    relationships.add_argument("--db", default="data/procure_radar.sqlite3")
    relationships.add_argument("--min-contracts", type=int, default=2)
    relationships.add_argument("--min-value", type=float, default=100000.0)
    relationships.add_argument("--min-buyer-share", type=float, default=0.05)
    relationships.add_argument("--min-supplier-dependency", type=float, default=0.05)
    relationships.add_argument("--limit", type=int, default=30)
    relationships.add_argument("--json", action="store_true")
    relationships.set_defaults(func=cmd_relationship_radar)


    buyer_profile = sub.add_parser(
        "buyer-profile",
        help="show observed suppliers, concentration and markets for one buyer INN",
    )
    buyer_profile.add_argument("inn")
    buyer_profile.add_argument("--region", type=int, required=True)
    buyer_profile.add_argument("--db", default="data/procure_radar.sqlite3")
    buyer_profile.add_argument("--limit", type=int, default=10)
    buyer_profile.add_argument("--json", action="store_true")
    buyer_profile.set_defaults(func=cmd_buyer_profile)

    supplier_profile = sub.add_parser(
        "supplier-profile",
        help="show observed contracts, customers, markets and evidence bundles for one supplier INN",
    )
    supplier_profile.add_argument("inn")
    supplier_profile.add_argument("--region", type=int, required=True)
    supplier_profile.add_argument("--db", default="data/procure_radar.sqlite3")
    supplier_profile.add_argument("--limit", type=int, default=10)
    supplier_profile.add_argument("--json", action="store_true")
    supplier_profile.set_defaults(func=cmd_supplier_profile)


    repair_protocol_prices = sub.add_parser(
        "repair-protocol-prices",
        help="Recompute protocol final prices from admitted tender applications",
    )
    repair_protocol_prices.add_argument("--db", default="data/procure_radar.sqlite3")
    repair_protocol_prices.set_defaults(func=cmd_repair_protocol_prices)

    segments = sub.add_parser("segments", help="List commercial opportunity segments")
    segments.set_defaults(func=cmd_segments)


    rzn_search = sub.add_parser("rzn-search", help="Search the public Roszdravnadzor medical-device registry")
    rzn_search.add_argument("query", nargs="?", default="", help="Full-text search; empty lists the registry")
    rzn_search.add_argument("--base-url", default="https://elk.roszdravnadzor.gov.ru")
    rzn_search.add_argument("--legal-system", default="RUSSIA")
    rzn_search.add_argument("--page", type=int, default=0)
    rzn_search.add_argument("--size", type=int, default=10)
    rzn_search.add_argument(
        "--active-only",
        action="store_true",
        help="Filter to active registration certificates (RZN statusIds=[1])",
    )
    rzn_search.add_argument("--cookie", help="Optional browser Cookie header if the public endpoint ever requires it")
    rzn_search.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification for Roszdravnadzor only")
    rzn_search.add_argument("--output")
    rzn_search.add_argument("--json", action="store_true")
    rzn_search.set_defaults(func=cmd_rzn_search)

    rzn_sync = sub.add_parser("rzn-sync", help="Mirror the public Roszdravnadzor medical-device registry into SQLite")
    rzn_sync.add_argument("--db", default="data/procure_radar.sqlite3")
    rzn_sync.add_argument("--base-url", default="https://elk.roszdravnadzor.gov.ru")
    rzn_sync.add_argument("--legal-system", default="RUSSIA")
    rzn_sync.add_argument("--query", default="", help="Optional text filter; empty mirrors the whole selected registry")
    rzn_sync.add_argument("--size", type=int, default=100, help="Page size, 1..100")
    rzn_sync.add_argument("--pages", type=int, default=10, help="Pages per run unless --continuous")
    rzn_sync.add_argument("--continuous", action="store_true", help="Continue until the registry's last page")
    rzn_sync.add_argument("--request-delay", type=float, default=0.25)
    rzn_sync.add_argument(
        "--active-only",
        action="store_true",
        help="Mirror only active registration certificates (RZN statusIds=[1])",
    )
    rzn_sync.add_argument("--restart", action="store_true")
    rzn_sync.add_argument("--cookie", help="Optional browser Cookie header if the public endpoint ever requires it")
    rzn_sync.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification for Roszdravnadzor only")
    rzn_sync.add_argument("--json", action="store_true")
    rzn_sync.set_defaults(func=cmd_rzn_sync)

    rzn_nsi_get = sub.add_parser("rzn-nsi-get", help="Resolve one Roszdravnadzor NSI record ID")
    rzn_nsi_get.add_argument("record_id")
    rzn_nsi_get.add_argument("--db", help="Optionally persist the resolved record into SQLite")
    rzn_nsi_get.add_argument("--base-url", default="https://elk.roszdravnadzor.gov.ru")
    rzn_nsi_get.add_argument("--cookie")
    rzn_nsi_get.add_argument("--insecure", action="store_true")
    rzn_nsi_get.set_defaults(func=cmd_rzn_nsi_get)

    rzn_nsi_sync = sub.add_parser("rzn-nsi-sync", help="Resolve classifier IDs from the local RZN mirror")
    rzn_nsi_sync.add_argument("--db", default="data/procure_radar.sqlite3")
    rzn_nsi_sync.add_argument("--base-url", default="https://elk.roszdravnadzor.gov.ru")
    rzn_nsi_sync.add_argument("--limit", type=int, default=100, help="NSI records per run unless --continuous")
    rzn_nsi_sync.add_argument("--continuous", action="store_true")
    rzn_nsi_sync.add_argument("--request-delay", type=float, default=0.25)
    rzn_nsi_sync.add_argument("--cookie")
    rzn_nsi_sync.add_argument("--insecure", action="store_true")
    rzn_nsi_sync.add_argument("--json", action="store_true")
    rzn_nsi_sync.set_defaults(func=cmd_rzn_nsi_sync)

    rzn_okpd2 = sub.add_parser("rzn-okpd2", help="Show local active RZN products/producers for one OKPD2 code")
    rzn_okpd2.add_argument("code")
    rzn_okpd2.add_argument("--db", default="data/procure_radar.sqlite3")
    rzn_okpd2.add_argument("--limit", type=int, default=10)
    rzn_okpd2.add_argument("--all-statuses", action="store_true")
    rzn_okpd2.add_argument("--json", action="store_true")
    rzn_okpd2.set_defaults(func=cmd_rzn_okpd2)

    rzn_stats = sub.add_parser("rzn-stats", help="Show local Roszdravnadzor registry coverage")
    rzn_stats.add_argument("--db", default="data/procure_radar.sqlite3")
    rzn_stats.add_argument("--json", action="store_true")
    rzn_stats.set_defaults(func=cmd_rzn_stats)

    gisp_import = sub.add_parser("gisp-import", help="Import the official GISP PP719 XLSX registry into SQLite")
    gisp_import.add_argument("path", help="Path to the XLSX downloaded from GISP")
    gisp_import.add_argument("--db", default="data/procure_radar.sqlite3")
    gisp_import.add_argument("--scope", choices=("active", "all"), default="active", help="Use active for the 'Скачать только действующие' snapshot")
    gisp_import.add_argument("--sheet", help="Optional worksheet name; defaults to the first sheet")
    gisp_import.add_argument("--json", action="store_true")
    gisp_import.set_defaults(func=cmd_gisp_import)

    gisp_stats = sub.add_parser("gisp-stats", help="Show local GISP registry coverage")
    gisp_stats.add_argument("--db", default="data/procure_radar.sqlite3")
    gisp_stats.add_argument("--json", action="store_true")
    gisp_stats.set_defaults(func=cmd_gisp_stats)

    gisp_okpd2 = sub.add_parser("gisp-okpd2", help="Show Russian industrial products/manufacturers for one OKPD2 code")
    gisp_okpd2.add_argument("code")
    gisp_okpd2.add_argument("--db", default="data/procure_radar.sqlite3")
    gisp_okpd2.add_argument("--limit", type=int, default=20)
    gisp_okpd2.add_argument("--all-statuses", action="store_true")
    gisp_okpd2.add_argument("--json", action="store_true")
    gisp_okpd2.set_defaults(func=cmd_gisp_okpd2)


    def add_fsa_auth(p: argparse.ArgumentParser) -> None:
        p.add_argument("--base-url", default="https://pub.fsa.gov.ru")
        p.add_argument("--token", help="Optional Authorization token copied from the browser; auto-login is default")
        p.add_argument("--cookie", help="Optional Cookie header copied from the browser")
        p.add_argument("--username", default="anonymous")
        p.add_argument("--password", default="hrgesf7HDR67Bd")
        p.add_argument("--insecure", action="store_true", help="Disable TLS verification for FSA only")

    fsa_search = sub.add_parser("fsa-search", help="Search/list Rosaccreditation conformity certificates")
    fsa_search.add_argument("query", nargs="?", default="")
    fsa_search.add_argument("--search-field", default="productFullName", help="columnsSearch field name; use the exact field from DevTools if FSA changes it")
    fsa_search.add_argument("--page", type=int, default=0)
    fsa_search.add_argument("--size", type=int, default=10)
    fsa_search.add_argument("--all-statuses", action="store_true")
    fsa_search.add_argument("--filter-json", help="JSON object (or file) merged into the FSA filter")
    fsa_search.add_argument("--output")
    fsa_search.add_argument("--json", action="store_true")
    add_fsa_auth(fsa_search)
    fsa_search.set_defaults(func=cmd_fsa_search)

    fsa_get = sub.add_parser("fsa-get", help="Fetch one full Rosaccreditation certificate card")
    fsa_get.add_argument("certificate_id", type=int)
    fsa_get.add_argument("--db", help="Optionally persist the detail card and its TN VED IDs")
    fsa_get.add_argument("--output")
    add_fsa_auth(fsa_get)
    fsa_get.set_defaults(func=cmd_fsa_get)

    fsa_sync = sub.add_parser("fsa-sync", help="Mirror FSA certificate list pages into SQLite (details are separate)")
    fsa_sync.add_argument("--db", default="data/procure_radar.sqlite3")
    fsa_sync.add_argument("--query", default="")
    fsa_sync.add_argument("--search-field", default="productFullName")
    fsa_sync.add_argument("--size", type=int, default=100)
    fsa_sync.add_argument("--pages", type=int, default=10)
    fsa_sync.add_argument("--continuous", action="store_true")
    fsa_sync.add_argument("--request-delay", type=float, default=0.5)
    fsa_sync.add_argument("--all-statuses", action="store_true")
    fsa_sync.add_argument("--filter-json")
    fsa_sync.add_argument("--restart", action="store_true")
    fsa_sync.add_argument("--json", action="store_true")
    add_fsa_auth(fsa_sync)
    fsa_sync.set_defaults(func=cmd_fsa_sync)

    fsa_detail = sub.add_parser("fsa-detail-sync", help="Fetch full detail cards for locally stored FSA certificates")
    fsa_detail.add_argument("--db", default="data/procure_radar.sqlite3")
    fsa_detail.add_argument(
        "--limit", type=int, default=100,
        help="Maximum details in one batch; with --continuous this is the batch size",
    )
    fsa_detail.add_argument("--id", type=int, action="append", help="Fetch only a certificate ID; repeatable")
    fsa_detail.add_argument(
        "--continuous", action="store_true",
        help="Fetch pending detail cards batch-by-batch until exhausted; failed IDs are retried on the next run",
    )
    fsa_detail.add_argument(
        "--known-only", action="store_true",
        help="Fetch only summaries whose applicant/manufacturer name matches an already-known organization; final linking still uses the official INN from FSA detail",
    )
    fsa_detail.add_argument(
        "--region", type=int,
        help="With --known-only, restrict candidate organizations to this region code (e.g. 2 for Bashkortostan)",
    )
    fsa_detail.add_argument("--request-delay", type=float, default=0.5)
    fsa_detail.add_argument("--json", action="store_true")
    add_fsa_auth(fsa_detail)
    fsa_detail.set_defaults(func=cmd_fsa_detail_sync)

    fsa_tnved_sync = sub.add_parser("fsa-tnved-sync", help="Resolve local FSA TN VED NSI IDs in batches")
    fsa_tnved_sync.add_argument("--db", default="data/procure_radar.sqlite3")
    fsa_tnved_sync.add_argument("--batch-size", type=int, default=200)
    fsa_tnved_sync.add_argument("--batches", type=int, default=1)
    fsa_tnved_sync.add_argument("--continuous", action="store_true")
    fsa_tnved_sync.add_argument("--request-delay", type=float, default=0.25)
    fsa_tnved_sync.add_argument("--json", action="store_true")
    add_fsa_auth(fsa_tnved_sync)
    fsa_tnved_sync.set_defaults(func=cmd_fsa_tnved_sync)

    fsa_stats = sub.add_parser("fsa-stats", help="Show local Rosaccreditation enrichment coverage")
    fsa_stats.add_argument("--db", default="data/procure_radar.sqlite3")
    fsa_stats.add_argument("--region", type=int, help="Scope known-only candidate statistics to one region code")
    fsa_stats.add_argument("--json", action="store_true")
    fsa_stats.set_defaults(func=cmd_fsa_stats)

    fsa_known = sub.add_parser(
        "fsa-known-matches",
        help="Inspect name-match candidates after FSA detail and compare them with official INNs",
    )
    fsa_known.add_argument("--db", default="data/procure_radar.sqlite3")
    fsa_known.add_argument("--limit", type=int, default=20)
    fsa_known.add_argument("--region", type=int, help="Restrict expected known organizations to one region code")
    fsa_known.add_argument(
        "--validation",
        choices=("all", "verified", "mismatch", "missing_inn"),
        default="all",
    )
    fsa_known.add_argument("--json", action="store_true")
    fsa_known.set_defaults(func=cmd_fsa_known_matches)

    fsa_tnved = sub.add_parser("fsa-tnved", help="Show certificate/applicant supply for one resolved TN VED code")
    fsa_tnved.add_argument("code")
    fsa_tnved.add_argument("--db", default="data/procure_radar.sqlite3")
    fsa_tnved.add_argument("--limit", type=int, default=20)
    fsa_tnved.add_argument("--json", action="store_true")
    fsa_tnved.set_defaults(func=cmd_fsa_tnved)

    org_backfill = sub.add_parser(
        "organizations-backfill",
        help="Build the canonical organization graph from already collected procurement/GISP/FSA data",
    )
    org_backfill.add_argument("--db", default="data/procure_radar.sqlite3")
    org_backfill.add_argument("--json", action="store_true")
    org_backfill.set_defaults(func=cmd_organizations_backfill)

    contract_parties_backfill = sub.add_parser(
        "contract-parties-backfill",
        help="Fill missing organization names from already stored raw contract/purchase documents",
    )
    contract_parties_backfill.add_argument("--region", type=int)
    contract_parties_backfill.add_argument("--year", type=int)
    contract_parties_backfill.add_argument("--db", default="data/procure_radar.sqlite3")
    contract_parties_backfill.add_argument("--json", action="store_true")
    contract_parties_backfill.set_defaults(func=cmd_contract_parties_backfill)

    regional_demand_parties_resolve = sub.add_parser(
        "regional-demand-parties-resolve",
        help="Resolve missing buyer/supplier names only for one manufacturer's matched regional demand contracts",
    )
    regional_demand_parties_resolve.add_argument("inn", help="Manufacturer INN")
    regional_demand_parties_resolve.add_argument("--region", type=int, required=True)
    regional_demand_parties_resolve.add_argument("--year", type=int, required=True)
    regional_demand_parties_resolve.add_argument("--base-url", default="https://v2test.gosplan.info")
    regional_demand_parties_resolve.add_argument("--api-key")
    regional_demand_parties_resolve.add_argument(
        "--max-requests",
        type=int,
        default=100,
        help="Maximum live detail requests in this run; 0 means unlimited",
    )
    regional_demand_parties_resolve.add_argument(
        "--rate-per-minute",
        type=float,
        default=7.0,
        help="Conservative live detail request rate; default 7/min",
    )
    regional_demand_parties_resolve.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cached detail payloads and fetch them again",
    )
    regional_demand_parties_resolve.add_argument("--db", default="data/procure_radar.sqlite3")
    regional_demand_parties_resolve.add_argument("--json", action="store_true")
    regional_demand_parties_resolve.set_defaults(func=cmd_regional_demand_parties_resolve)

    fns_msp = sub.add_parser(
        "fns-msp-import",
        help="Import the official FNS Unified SME Registry XML/ZIP without extracting it",
    )
    fns_msp.add_argument("path", help="FNS XML, ZIP, or directory with XML/ZIP parts")
    fns_msp.add_argument("--db", default="data/procure_radar.sqlite3")
    fns_msp.add_argument("--known-only", action="store_true", help="Keep only INNs already observed by Procure Radar")
    fns_msp.add_argument("--region", type=int, help="Keep only one FNS region code, e.g. 2 for Bashkortostan")
    fns_msp.add_argument("--commit-every", type=int, default=5000)
    fns_msp.add_argument("--json", action="store_true")
    fns_msp.set_defaults(func=cmd_fns_msp_import)

    fns_revexp = sub.add_parser(
        "fns-revexp-import",
        help="Import official FNS annual revenue/expense open data XML/ZIP",
    )
    fns_revexp.add_argument("path", help="FNS XML, ZIP, or directory with XML/ZIP parts")
    fns_revexp.add_argument("--db", default="data/procure_radar.sqlite3")
    fns_revexp.add_argument("--known-only", action="store_true", help="Keep only INNs already observed by Procure Radar")
    fns_revexp.add_argument("--year", type=int, help="Override reporting year when a source file lacks DateSost")
    fns_revexp.add_argument("--commit-every", type=int, default=5000)
    fns_revexp.add_argument("--json", action="store_true")
    fns_revexp.set_defaults(func=cmd_fns_revexp_import)

    org_list = sub.add_parser(
        "organizations-list",
        help="List unified organizations with optional MSP/financial/role filters",
    )
    org_list.add_argument("--db", default="data/procure_radar.sqlite3")
    org_list.add_argument("--msp", action="store_true", help="Only organizations present in the MSP registry")
    org_list.add_argument(
        "--financials", action="store_true", help="Only organizations with FNS financial rows"
    )
    org_list.add_argument("--role", help="Filter by organization role; manufacturer means GISP/FSA evidence")
    org_list.add_argument("--region", type=int, help="Filter by organization region code")
    org_list.add_argument("--year", type=int, help="Use one calendar year for financial and supplier activity")
    org_list.add_argument(
        "--manufacturer-source",
        choices=("gisp", "fsa", "both"),
        help="Restrict manufacturer evidence source",
    )
    org_list.add_argument("--limit", type=int, default=20)
    org_list.add_argument("--offset", type=int, default=0)
    org_list.add_argument("--json", action="store_true")
    org_list.set_defaults(func=cmd_organizations_list)

    org_show = sub.add_parser("organization", help="Show one unified organization intelligence card")
    org_show.add_argument("inn")
    org_show.add_argument("--db", default="data/procure_radar.sqlite3")
    org_show.set_defaults(func=cmd_organization_show)

    org_stats = sub.add_parser("organization-stats", help="Show organization/FNS enrichment coverage")
    org_stats.add_argument("--db", default="data/procure_radar.sqlite3")
    org_stats.add_argument("--json", action="store_true")
    org_stats.set_defaults(func=cmd_organization_stats)

    procurement_coverage = sub.add_parser(
        "procurement-coverage",
        help="Show regional signed-contract history coverage for one calendar year",
    )
    procurement_coverage.add_argument("--region", type=int, required=True)
    procurement_coverage.add_argument("--year", type=int, required=True)
    procurement_coverage.add_argument("--db", default="data/procure_radar.sqlite3")
    procurement_coverage.add_argument("--json", action="store_true")
    procurement_coverage.set_defaults(func=cmd_procurement_coverage)


    regional_demand = sub.add_parser(
        "regional-demand",
        help="Show signed-contract demand in a region/year for one manufacturer's GISP OKPD2 families",
    )
    regional_demand.add_argument("inn")
    regional_demand.add_argument("--region", type=int, required=True)
    regional_demand.add_argument("--year", type=int, required=True)
    regional_demand.add_argument("--db", default="data/procure_radar.sqlite3")
    regional_demand.add_argument("--json", action="store_true")
    regional_demand.set_defaults(func=cmd_regional_demand)

    regional_demand_buyers_parser = sub.add_parser(
        "regional-demand-buyers",
        help="Show the largest addressable regional buyers for one manufacturer's GISP OKPD2 families",
    )
    regional_demand_buyers_parser.add_argument("inn")
    regional_demand_buyers_parser.add_argument("--region", type=int, required=True)
    regional_demand_buyers_parser.add_argument("--year", type=int, required=True)
    regional_demand_buyers_parser.add_argument("--limit", type=int, default=20)
    regional_demand_buyers_parser.add_argument("--offset", type=int, default=0)
    regional_demand_buyers_parser.add_argument(
        "--contracts-per-buyer",
        type=int,
        default=3,
        help="Include this many largest matching contract examples under each buyer",
    )
    regional_demand_buyers_parser.add_argument("--db", default="data/procure_radar.sqlite3")
    regional_demand_buyers_parser.add_argument("--json", action="store_true")
    regional_demand_buyers_parser.set_defaults(func=cmd_regional_demand_buyers)

    regional_demand_competitors_parser = sub.add_parser(
        "regional-demand-competitors",
        help="Show incumbent suppliers currently serving one manufacturer's addressable regional market",
    )
    regional_demand_competitors_parser.add_argument("inn")
    regional_demand_competitors_parser.add_argument("--region", type=int, required=True)
    regional_demand_competitors_parser.add_argument("--year", type=int, required=True)
    regional_demand_competitors_parser.add_argument("--limit", type=int, default=20)
    regional_demand_competitors_parser.add_argument("--offset", type=int, default=0)
    regional_demand_competitors_parser.add_argument("--db", default="data/procure_radar.sqlite3")
    regional_demand_competitors_parser.add_argument("--json", action="store_true")
    regional_demand_competitors_parser.set_defaults(func=cmd_regional_demand_competitors)

    forward_sync = sub.add_parser(
        "forward-opportunities-sync",
        help="Refresh the latest 44-FZ notices and plan-schedules relevant to one manufacturer",
    )
    forward_sync.add_argument("inn", help="Manufacturer INN")
    forward_sync.add_argument("--region", type=int, required=True)
    forward_sync.add_argument("--history-year", type=int, required=True, help="Completed historical year used for buyer opportunity evidence")
    forward_sync.add_argument("--purchase-pages", type=int, default=5, help="Latest /fz44/purchases pages to scan")
    forward_sync.add_argument("--tenderplan-pages", type=int, default=5, help="Latest /fz44/tenderplans pages to scan")
    forward_sync.add_argument("--page-size", type=int, default=50)
    forward_sync.add_argument("--max-requests", type=int, default=100, help="Total live request budget; 0 means unlimited")
    forward_sync.add_argument("--rate-per-minute", type=float, default=7.0)
    forward_sync.add_argument("--refresh", action="store_true", help="Refetch cached purchase/plan details")
    forward_sync.add_argument("--base-url", default="https://v2test.gosplan.info")
    forward_sync.add_argument("--api-key")
    forward_sync.add_argument("--db", default="data/procure_radar.sqlite3")
    forward_sync.add_argument("--json", action="store_true")
    forward_sync.set_defaults(func=cmd_forward_opportunities_sync)

    forward = sub.add_parser(
        "forward-opportunities",
        help="Rank open notices and unpublished plan-schedule positions matching one manufacturer's GISP OKPD2 families",
    )
    forward.add_argument("inn", help="Manufacturer INN")
    forward.add_argument("--region", type=int, required=True)
    forward.add_argument("--as-of", help="Analysis date YYYY-MM-DD; defaults to today")
    forward.add_argument("--history-year", type=int, help="Historical buyer-evidence year; defaults to as-of year minus one")
    forward.add_argument("--current-limit", type=int, default=20)
    forward.add_argument("--planned-limit", type=int, default=20)
    forward.add_argument("--recent-notice-days", type=int, default=45, help="Keep recent notices with unknown deadline for this many days")
    forward.add_argument("--planned-horizon-days", type=int, default=730)
    forward.add_argument("--db", default="data/procure_radar.sqlite3")
    forward.add_argument("--json", action="store_true")
    forward.set_defaults(func=cmd_forward_opportunities)

    procurement_entry = sub.add_parser(
        "procurement-entry-opportunities",
        help="Rank manufacturers that look underrepresented in regional procurement",
    )
    procurement_entry.add_argument("--region", type=int, required=True)
    procurement_entry.add_argument("--year", type=int, required=True)
    procurement_entry.add_argument(
        "--manufacturer-source",
        choices=("gisp", "fsa", "both"),
        help="Restrict manufacturer evidence source",
    )
    procurement_entry.add_argument("--min-score", type=float, default=0.0)
    procurement_entry.add_argument("--limit", type=int, default=50)
    procurement_entry.add_argument("--offset", type=int, default=0)
    procurement_entry.add_argument("--db", default="data/procure_radar.sqlite3")
    procurement_entry.add_argument("--json", action="store_true")
    procurement_entry.set_defaults(func=cmd_procurement_entry_opportunities)

    ui = sub.add_parser("ui", help="Open the desktop dashboard")
    ui.add_argument("--db", default="data/procure_radar.sqlite3")
    ui.add_argument("--region", type=int, default=2, help="Gosplan/KLADR region code")
    ui.set_defaults(func=cmd_ui)

    stats = sub.add_parser("stats", help="Show extraction coverage and a few rows")
    stats.add_argument("--db", default="data/procure_radar.sqlite3")
    stats.add_argument("--limit", type=int, default=10)
    stats.set_defaults(func=cmd_stats)

    link_stats = sub.add_parser("link-stats", help="Show purchase/procedure/contract linkage and price reductions")
    link_stats.add_argument("--db", default="data/procure_radar.sqlite3")
    link_stats.add_argument("--limit", type=int, default=10)
    link_stats.set_defaults(func=cmd_link_stats)

    show = sub.add_parser("show", help="Show one normalized purchase and linked entities")
    show.add_argument("purchase_number")
    show.add_argument("--db", default="data/procure_radar.sqlite3")
    show.set_defaults(func=cmd_show)

    competition = sub.add_parser("competition", help="Show normalized competition signal for one purchase")
    competition.add_argument("purchase_number")
    competition.add_argument("--db", default="data/procure_radar.sqlite3")
    competition.set_defaults(func=cmd_competition)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
