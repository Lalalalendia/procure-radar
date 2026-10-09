from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS raw_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    external_id TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    payload_json TEXT NOT NULL,
    UNIQUE(source, endpoint, external_id)
);

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_document_id INTEGER NOT NULL UNIQUE REFERENCES raw_documents(id) ON DELETE CASCADE,
    purchase_number TEXT NOT NULL UNIQUE,
    published_at TEXT,
    collecting_finished_at TEXT,
    doc_created_at TEXT,
    doc_updated_at TEXT,
    updated_at TEXT,
    max_price REAL,
    currency_code TEXT,
    object_info TEXT,
    purchase_type TEXT,
    region_code INTEGER,
    stage INTEGER,
    responsible_inn TEXT,
    contract_guarantee_amount REAL,
    contract_guarantee_part REAL
);

CREATE INDEX IF NOT EXISTS ix_purchases_published_at ON purchases(published_at);
CREATE INDEX IF NOT EXISTS ix_purchases_region_code ON purchases(region_code);
CREATE INDEX IF NOT EXISTS ix_purchases_stage ON purchases(stage);
CREATE INDEX IF NOT EXISTS ix_purchases_max_price ON purchases(max_price);

CREATE TABLE IF NOT EXISTS purchase_parties (
    purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('customer', 'owner')),
    inn TEXT NOT NULL,
    PRIMARY KEY (purchase_id, role, inn)
);
CREATE INDEX IF NOT EXISTS ix_purchase_parties_inn ON purchase_parties(inn);

CREATE TABLE IF NOT EXISTS purchase_codes (
    purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    system TEXT NOT NULL CHECK(system IN ('okpd2', 'ktru')),
    code TEXT NOT NULL,
    PRIMARY KEY (purchase_id, system, code)
);
CREATE INDEX IF NOT EXISTS ix_purchase_codes_system_code ON purchase_codes(system, code);

CREATE TABLE IF NOT EXISTS purchase_links (
    purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('ikz', 'plan_number', 'position_number')),
    value TEXT NOT NULL,
    PRIMARY KEY (purchase_id, kind, value)
);
CREATE INDEX IF NOT EXISTS ix_purchase_links_kind_value ON purchase_links(kind, value);

CREATE TABLE IF NOT EXISTS purchase_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    doc_type TEXT,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_purchase_documents_purchase_id ON purchase_documents(purchase_id);
"""


def _ensure_organization_import_run_columns(conn: sqlite3.Connection) -> None:
    """Upgrade the organization import journal in-place for existing databases."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='organization_import_runs'"
    ).fetchone()
    if table is None:
        return

    columns = {row[1] for row in conn.execute("PRAGMA table_info(organization_import_runs)")}
    status_added = False
    if "status" not in columns:
        conn.execute(
            "ALTER TABLE organization_import_runs "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'running'"
        )
        status_added = True
    if "files_failed" not in columns:
        conn.execute(
            "ALTER TABLE organization_import_runs "
            "ADD COLUMN files_failed INTEGER NOT NULL DEFAULT 0"
        )

    if status_added:
        # Old rows predate explicit lifecycle tracking. We can faithfully infer
        # interrupted/fatal runs from completed_at/error; successful-looking
        # legacy rows remain success because per-member failure counts were not
        # persisted in the old schema.
        conn.execute(
            """
            UPDATE organization_import_runs
            SET status=CASE
                WHEN completed_at IS NULL THEN 'interrupted'
                WHEN error IS NOT NULL THEN 'failed'
                ELSE 'success'
            END
            """
        )
    conn.commit()


def _ensure_fsa_sync_state_columns(conn: sqlite3.Connection) -> None:
    """Add resumable date-cursor fields to older FSA sync-state tables."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fsa_sync_state'"
    ).fetchone()
    if table is None:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fsa_sync_state)")}
    if "cursor_max_date" not in columns:
        conn.execute("ALTER TABLE fsa_sync_state ADD COLUMN cursor_max_date TEXT")
    if "partition_count" not in columns:
        conn.execute(
            "ALTER TABLE fsa_sync_state "
            "ADD COLUMN partition_count INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")

    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='purchases'"
    ).fetchone()
    if existing is not None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(purchases)")}
        if "purchase_number" not in columns:
            conn.close()
            raise RuntimeError(
                "The database uses the old MVP schema. Delete data/procure_radar.sqlite3 "
                "or pass a new --db path, then ingest again."
            )

    conn.executescript(SCHEMA)
    _ensure_organization_import_run_columns(conn)
    _ensure_fsa_sync_state_columns(conn)
    return conn


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an existing database for UI/reporting reads without schema work.

    ``connect()`` intentionally runs the idempotent schema bootstrap because CLI
    ingestion commands may be pointed at a new database.  The UI used to call it
    every time a tab was opened, which needlessly re-ran hundreds of CREATE TABLE /
    CREATE INDEX checks on the GUI path and could contend with a running collector.
    Read-only connections skip all schema initialization and use conservative SQLite
    read-performance pragmas instead.
    """
    db_path = Path(path).expanduser().resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    uri = db_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32768")
    try:
        conn.execute("PRAGMA mmap_size=268435456")
    except sqlite3.DatabaseError:
        pass
    return conn


def insert_raw(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    external_id: str | None,
    *,
    endpoint: str = "/fz44/purchases",
) -> int:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if external_id:
        conn.execute(
            """
            INSERT INTO raw_documents(source, endpoint, external_id, payload_json)
            VALUES('gosplan-v2', ?, ?, ?)
            ON CONFLICT(source, endpoint, external_id)
            DO UPDATE SET payload_json=excluded.payload_json, fetched_at=datetime('now')
            """,
            (endpoint, external_id, encoded),
        )
        row = conn.execute(
            """
            SELECT id FROM raw_documents
            WHERE source='gosplan-v2' AND endpoint=? AND external_id=?
            """,
            (endpoint, external_id),
        ).fetchone()
        assert row is not None
        return int(row["id"])

    cur = conn.execute(
        """
        INSERT INTO raw_documents(source, endpoint, payload_json)
        VALUES('gosplan-v2', ?, ?)
        """,
        (endpoint, encoded),
    )
    return int(cur.lastrowid)

# Schema extension v2: contract-conclusion procedures and signed contracts.
SCHEMA += """

CREATE TABLE IF NOT EXISTS contract_procedures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_document_id INTEGER NOT NULL UNIQUE REFERENCES raw_documents(id) ON DELETE CASCADE,
    contract_project_number TEXT NOT NULL UNIQUE,
    purchase_number TEXT,
    customer_inn TEXT,
    participant_inn TEXT,
    price REAL,
    currency_code TEXT,
    region_code INTEGER,
    subject TEXT,
    published_at TEXT,
    doc_created_at TEXT,
    doc_updated_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_contract_procedures_purchase_number ON contract_procedures(purchase_number);
CREATE INDEX IF NOT EXISTS ix_contract_procedures_customer_inn ON contract_procedures(customer_inn);
CREATE INDEX IF NOT EXISTS ix_contract_procedures_participant_inn ON contract_procedures(participant_inn);

CREATE TABLE IF NOT EXISTS procedure_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES contract_procedures(id) ON DELETE CASCADE,
    doc_type TEXT,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_procedure_documents_procedure_id ON procedure_documents(procedure_id);

CREATE TABLE IF NOT EXISTS contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_document_id INTEGER NOT NULL UNIQUE REFERENCES raw_documents(id) ON DELETE CASCADE,
    reg_num TEXT NOT NULL UNIQUE,
    purchase_number TEXT,
    customer_inn TEXT,
    price REAL,
    currency_code TEXT,
    region_code INTEGER,
    stage TEXT,
    subject TEXT,
    plan_number TEXT,
    position_number TEXT,
    exe_start TEXT,
    exe_end TEXT,
    elact_at TEXT,
    published_at TEXT,
    doc_created_at TEXT,
    doc_updated_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_contracts_purchase_number ON contracts(purchase_number);
CREATE INDEX IF NOT EXISTS ix_contracts_customer_inn ON contracts(customer_inn);
CREATE INDEX IF NOT EXISTS ix_contracts_region_code ON contracts(region_code);
CREATE INDEX IF NOT EXISTS ix_contracts_price ON contracts(price);

CREATE TABLE IF NOT EXISTS contract_suppliers (
    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    inn TEXT NOT NULL,
    PRIMARY KEY (contract_id, inn)
);
CREATE INDEX IF NOT EXISTS ix_contract_suppliers_inn ON contract_suppliers(inn);

CREATE TABLE IF NOT EXISTS contract_codes (
    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    system TEXT NOT NULL CHECK(system IN ('okpd2', 'ktru')),
    code TEXT NOT NULL,
    PRIMARY KEY (contract_id, system, code)
);
CREATE INDEX IF NOT EXISTS ix_contract_codes_system_code ON contract_codes(system, code);

CREATE TABLE IF NOT EXISTS contract_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    doc_type TEXT,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_contract_documents_contract_id ON contract_documents(contract_id);
"""

# Schema extension v3: tender protocols and individual applications.
SCHEMA += """

CREATE TABLE IF NOT EXISTS tender_protocols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_document_id INTEGER NOT NULL UNIQUE REFERENCES raw_documents(id) ON DELETE CASCADE,
    protocol_key TEXT NOT NULL UNIQUE,
    purchase_number TEXT NOT NULL,
    doc_type TEXT,
    published_at TEXT,
    source_id TEXT,
    source_external_id TEXT,
    version_number TEXT,
    procedure_at TEXT,
    applications_count INTEGER NOT NULL DEFAULT 0,
    admitted_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    final_price REAL,
    is_abandoned INTEGER NOT NULL DEFAULT 0,
    abandoned_reason_code TEXT,
    abandoned_reason_name TEXT
);
CREATE INDEX IF NOT EXISTS ix_tender_protocols_purchase_number ON tender_protocols(purchase_number);
CREATE INDEX IF NOT EXISTS ix_tender_protocols_doc_type ON tender_protocols(doc_type);
CREATE INDEX IF NOT EXISTS ix_tender_protocols_is_abandoned ON tender_protocols(is_abandoned);

CREATE TABLE IF NOT EXISTS tender_applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id INTEGER NOT NULL REFERENCES tender_protocols(id) ON DELETE CASCADE,
    app_number TEXT,
    app_at TEXT,
    final_price REAL,
    admitted INTEGER,
    app_rating INTEGER,
    UNIQUE(protocol_id, app_number)
);
CREATE INDEX IF NOT EXISTS ix_tender_applications_protocol_id ON tender_applications(protocol_id);
CREATE INDEX IF NOT EXISTS ix_tender_applications_admitted ON tender_applications(admitted);
"""

# Schema extension v4: line-level purchase objects from full purchase documents.
SCHEMA += """

CREATE TABLE IF NOT EXISTS purchase_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    source_doc_type TEXT,
    name TEXT,
    ktru_code TEXT,
    ktru_name TEXT,
    okpd2_code TEXT,
    okpd2_name TEXT,
    okei_code TEXT,
    unit_name TEXT,
    quantity REAL,
    unit_price REAL,
    amount REAL,
    is_medical_product INTEGER,
    characteristics_json TEXT,
    UNIQUE(purchase_id, item_key)
);
CREATE INDEX IF NOT EXISTS ix_purchase_items_purchase_id ON purchase_items(purchase_id);
CREATE INDEX IF NOT EXISTS ix_purchase_items_ktru_code ON purchase_items(ktru_code);
CREATE INDEX IF NOT EXISTS ix_purchase_items_okpd2_code ON purchase_items(okpd2_code);
CREATE INDEX IF NOT EXISTS ix_purchase_items_amount ON purchase_items(amount);
"""

# Schema extension v5: remember full-detail fetches so reruns do not waste API calls.
SCHEMA += """

CREATE TABLE IF NOT EXISTS purchase_detail_fetches (
    purchase_number TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    protocol_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_purchase_detail_fetches_status ON purchase_detail_fetches(status);
"""

# Schema extension v6: resumable historical region backfills.
SCHEMA += """

CREATE TABLE IF NOT EXISTS history_backfills (
    checkpoint_key TEXT PRIMARY KEY,
    region_code INTEGER NOT NULL,
    since_date TEXT NOT NULL,
    until_date TEXT,
    query_json TEXT NOT NULL,
    page_limit INTEGER NOT NULL,
    next_skip INTEGER NOT NULL DEFAULT 0,
    pages_scanned INTEGER NOT NULL DEFAULT 0,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    rows_in_window INTEGER NOT NULL DEFAULT 0,
    details_fetched INTEGER NOT NULL DEFAULT 0,
    newest_published_at TEXT,
    oldest_published_at TEXT,
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_history_backfills_region_code ON history_backfills(region_code);
CREATE INDEX IF NOT EXISTS ix_history_backfills_completed ON history_backfills(completed);
"""

# Schema extension v7: resumable date/time shards for historical purchase backfills.
SCHEMA += """

CREATE TABLE IF NOT EXISTS history_time_shards (
    checkpoint_key TEXT NOT NULL,
    shard_start TEXT NOT NULL,
    shard_end TEXT NOT NULL,
    next_skip INTEGER NOT NULL DEFAULT 0,
    total_count INTEGER,
    completed INTEGER NOT NULL DEFAULT 0,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    rows_in_window INTEGER NOT NULL DEFAULT 0,
    details_fetched INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (checkpoint_key, shard_start, shard_end)
);
CREATE INDEX IF NOT EXISTS ix_history_time_shards_checkpoint
    ON history_time_shards(checkpoint_key, completed, shard_start);
"""

# Schema extension v8: public Roszdravnadzor medical-device registry mirror.
SCHEMA += """

CREATE TABLE IF NOT EXISTS rzn_med_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id INTEGER NOT NULL UNIQUE,
    application_id INTEGER,
    status_id INTEGER,
    status_code TEXT,
    status_name TEXT,
    legal_system TEXT,
    registration_number TEXT,
    registration_date TEXT,
    end_date TEXT,
    name TEXT,
    producer_name TEXT,
    producer_address TEXT,
    declarant_name TEXT,
    declarant_address TEXT,
    representative_name TEXT,
    representative_address TEXT,
    parent_med_product_id INTEGER,
    frnsi_id TEXT,
    date_registration_ru TEXT,
    has_changes INTEGER,
    date_start TEXT,
    acceptance_countries_json TEXT NOT NULL DEFAULT '[]',
    production_sites_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_rzn_med_products_registration_number
    ON rzn_med_products(registration_number);
CREATE INDEX IF NOT EXISTS ix_rzn_med_products_status_code
    ON rzn_med_products(status_code);
CREATE INDEX IF NOT EXISTS ix_rzn_med_products_producer_name
    ON rzn_med_products(producer_name);
CREATE INDEX IF NOT EXISTS ix_rzn_med_products_representative_name
    ON rzn_med_products(representative_name);
CREATE INDEX IF NOT EXISTS ix_rzn_med_products_name
    ON rzn_med_products(name);

CREATE TABLE IF NOT EXISTS rzn_med_product_classifier_ids (
    med_product_id INTEGER NOT NULL REFERENCES rzn_med_products(id) ON DELETE CASCADE,
    classifier_external_id TEXT NOT NULL,
    PRIMARY KEY (med_product_id, classifier_external_id)
);
CREATE INDEX IF NOT EXISTS ix_rzn_classifier_external_id
    ON rzn_med_product_classifier_ids(classifier_external_id);

CREATE TABLE IF NOT EXISTS rzn_sync_state (
    sync_key TEXT PRIMARY KEY,
    legal_system TEXT NOT NULL,
    text_search TEXT NOT NULL,
    page_size INTEGER NOT NULL,
    next_page INTEGER NOT NULL DEFAULT 0,
    total_elements INTEGER,
    pages_scanned INTEGER NOT NULL DEFAULT 0,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# Schema extension v9: resolved Roszdravnadzor NSI classifier records.
SCHEMA += """

CREATE TABLE IF NOT EXISTS rzn_nsi_records (
    classifier_external_id TEXT PRIMARY KEY,
    catalog_code TEXT,
    status_code TEXT,
    actual_start_dt TEXT,
    create_dttm TEXT,
    modify_dttm TEXT,
    code TEXT,
    name TEXT,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_rzn_nsi_catalog_code
    ON rzn_nsi_records(catalog_code);
CREATE INDEX IF NOT EXISTS ix_rzn_nsi_code
    ON rzn_nsi_records(code);
"""

# Schema extension v10: GISP / Minpromtorg Russian industrial product registry snapshots.
SCHEMA += """

CREATE TABLE IF NOT EXISTS gisp_import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    source_scope TEXT NOT NULL CHECK(source_scope IN ('active','all')),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    rows_imported INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    header_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS gisp_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registry_number TEXT NOT NULL UNIQUE,
    primary_registry_number TEXT,
    manufacturer_name TEXT,
    manufacturer_inn TEXT,
    manufacturer_ogrn TEXT,
    manufacturer_address TEXT,
    production_address TEXT,
    registry_date TEXT,
    valid_until TEXT,
    actual_end_date TEXT,
    product_name TEXT,
    okpd2_code TEXT,
    tnved_code TEXT,
    points TEXT,
    percentage_indicator TEXT,
    conformity TEXT,
    source_scope TEXT NOT NULL CHECK(source_scope IN ('active','all')),
    is_active INTEGER,
    last_seen_run_id INTEGER,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_gisp_products_okpd2 ON gisp_products(okpd2_code);
CREATE INDEX IF NOT EXISTS ix_gisp_products_inn ON gisp_products(manufacturer_inn);
CREATE INDEX IF NOT EXISTS ix_gisp_products_active ON gisp_products(is_active);
CREATE INDEX IF NOT EXISTS ix_gisp_products_product_name ON gisp_products(product_name);
"""


# Schema extension v12: preserve every GISP source row / code association.
SCHEMA += """

CREATE TABLE IF NOT EXISTS gisp_registry_rows (
    row_key TEXT PRIMARY KEY,
    registry_number TEXT NOT NULL REFERENCES gisp_products(registry_number) ON DELETE CASCADE,
    product_name TEXT,
    okpd2_code TEXT,
    tnved_code TEXT,
    source_scope TEXT NOT NULL CHECK(source_scope IN ('active','all')),
    last_seen_run_id INTEGER,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_gisp_registry_rows_registry ON gisp_registry_rows(registry_number);
CREATE INDEX IF NOT EXISTS ix_gisp_registry_rows_okpd2 ON gisp_registry_rows(okpd2_code);
CREATE INDEX IF NOT EXISTS ix_gisp_registry_rows_tnved ON gisp_registry_rows(tnved_code);
"""

# Schema extension v11: Rosaccreditation (FSA) certificate summaries, details and TN VED NSI.
SCHEMA += """

CREATE TABLE IF NOT EXISTS fsa_certificates (
    external_id INTEGER PRIMARY KEY,
    status_id INTEGER,
    number TEXT,
    reg_date TEXT,
    end_date TEXT,
    blank_number TEXT,
    technical_reglaments TEXT,
    product_group TEXT,
    cert_type TEXT,
    cert_object_type TEXT,
    applicant_legal_subject_type TEXT,
    applicant_type TEXT,
    applicant_name TEXT,
    applicant_inn TEXT,
    applicant_ogrn TEXT,
    applicant_kpp TEXT,
    applicant_email TEXT,
    applicant_phone TEXT,
    applicant_address TEXT,
    manufacturer_legal_subject_type TEXT,
    manufacturer_name TEXT,
    manufacturer_inn TEXT,
    manufacturer_ogrn TEXT,
    manufacturer_kpp TEXT,
    manufacturer_email TEXT,
    manufacturer_phone TEXT,
    manufacturer_address TEXT,
    certification_authority_id INTEGER,
    certification_authority_attestat_reg_number TEXT,
    product_origin TEXT,
    product_id INTEGER,
    product_full_name TEXT,
    product_identification_name TEXT,
    product_identification_model TEXT,
    product_identification_article TEXT,
    product_identification_gtin TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    detail_json TEXT,
    detail_fetched_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_fsa_certificates_status ON fsa_certificates(status_id);
CREATE INDEX IF NOT EXISTS ix_fsa_certificates_number ON fsa_certificates(number);
CREATE INDEX IF NOT EXISTS ix_fsa_certificates_reg_date ON fsa_certificates(reg_date);
CREATE INDEX IF NOT EXISTS ix_fsa_certificates_applicant_inn ON fsa_certificates(applicant_inn);
CREATE INDEX IF NOT EXISTS ix_fsa_certificates_manufacturer_inn ON fsa_certificates(manufacturer_inn);
CREATE INDEX IF NOT EXISTS ix_fsa_certificates_product_name ON fsa_certificates(product_full_name);

CREATE TABLE IF NOT EXISTS fsa_certificate_tnved (
    certificate_external_id INTEGER NOT NULL REFERENCES fsa_certificates(external_id) ON DELETE CASCADE,
    nsi_id INTEGER NOT NULL,
    PRIMARY KEY (certificate_external_id, nsi_id)
);
CREATE INDEX IF NOT EXISTS ix_fsa_certificate_tnved_nsi ON fsa_certificate_tnved(nsi_id);

CREATE TABLE IF NOT EXISTS fsa_tnved_nsi (
    nsi_id INTEGER PRIMARY KEY,
    master_id TEXT,
    code TEXT,
    name TEXT,
    hidden INTEGER,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_fsa_tnved_nsi_code ON fsa_tnved_nsi(code);

CREATE TABLE IF NOT EXISTS fsa_sync_state (
    sync_key TEXT PRIMARY KEY,
    active_only INTEGER NOT NULL DEFAULT 1,
    filter_json TEXT NOT NULL DEFAULT '{}',
    page_size INTEGER NOT NULL,
    next_page INTEGER NOT NULL DEFAULT 0,
    total_elements INTEGER,
    pages_scanned INTEGER NOT NULL DEFAULT 0,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    cursor_max_date TEXT,
    partition_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_fsa_sync_state_completed ON fsa_sync_state(completed);

-- Composite indexes used heavily by the desktop dashboard and analytics views.
CREATE INDEX IF NOT EXISTS ix_purchases_region_published
    ON purchases(region_code, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_contracts_region_published
    ON contracts(region_code, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_contracts_region_customer
    ON contracts(region_code, customer_inn);
CREATE INDEX IF NOT EXISTS ix_contracts_region_purchase
    ON contracts(region_code, purchase_number);
CREATE INDEX IF NOT EXISTS ix_tender_protocols_purchase_published
    ON tender_protocols(purchase_number, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_gisp_products_active_okpd2_inn
    ON gisp_products(is_active, okpd2_code, manufacturer_inn);
CREATE INDEX IF NOT EXISTS ix_gisp_registry_rows_okpd2_registry
    ON gisp_registry_rows(okpd2_code, registry_number);
"""

# Schema extension v13: canonical organization intelligence layer.
SCHEMA += """

CREATE TABLE IF NOT EXISTS organizations (
    inn TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL DEFAULT 'unknown'
        CHECK(entity_type IN ('unknown','legal_entity','individual_entrepreneur')),
    ogrn TEXT,
    kpp TEXT,
    name TEXT,
    region_code INTEGER,
    address TEXT,
    registration_date TEXT,
    status TEXT,
    okved_main TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_organizations_ogrn ON organizations(ogrn);
CREATE INDEX IF NOT EXISTS ix_organizations_region ON organizations(region_code);
CREATE INDEX IF NOT EXISTS ix_organizations_okved_main ON organizations(okved_main);
CREATE INDEX IF NOT EXISTS ix_organizations_name ON organizations(name);

CREATE TABLE IF NOT EXISTS organization_roles (
    inn TEXT NOT NULL REFERENCES organizations(inn) ON DELETE CASCADE,
    role TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (inn, role)
);
CREATE INDEX IF NOT EXISTS ix_organization_roles_role ON organization_roles(role, inn);

CREATE TABLE IF NOT EXISTS organization_msp (
    inn TEXT PRIMARY KEY REFERENCES organizations(inn) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    category_code TEXT,
    category_name TEXT,
    included_at TEXT,
    snapshot_date TEXT,
    is_new INTEGER,
    is_social INTEGER,
    employee_count INTEGER,
    region_code INTEGER,
    okved_main TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_organization_msp_category ON organization_msp(category_code);
CREATE INDEX IF NOT EXISTS ix_organization_msp_region ON organization_msp(region_code);
CREATE INDEX IF NOT EXISTS ix_organization_msp_okved ON organization_msp(okved_main);

CREATE TABLE IF NOT EXISTS organization_okved (
    inn TEXT NOT NULL REFERENCES organizations(inn) ON DELETE CASCADE,
    source TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    is_main INTEGER NOT NULL DEFAULT 0,
    snapshot_date TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (inn, source, code)
);
CREATE INDEX IF NOT EXISTS ix_organization_okved_code ON organization_okved(code, inn);
CREATE INDEX IF NOT EXISTS ix_organization_okved_main ON organization_okved(inn, is_main);

CREATE TABLE IF NOT EXISTS organization_financials (
    inn TEXT NOT NULL REFERENCES organizations(inn) ON DELETE CASCADE,
    year INTEGER NOT NULL,
    revenue REAL,
    expenses REAL,
    profit REAL,
    source TEXT NOT NULL DEFAULT 'fns_revexp',
    snapshot_date TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (inn, year, source)
);
CREATE INDEX IF NOT EXISTS ix_organization_financials_year ON organization_financials(year, inn);
CREATE INDEX IF NOT EXISTS ix_organization_financials_inn_year
    ON organization_financials(inn, year DESC);

CREATE TABLE IF NOT EXISTS organization_import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_path TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    files_seen INTEGER NOT NULL DEFAULT 0,
    files_failed INTEGER NOT NULL DEFAULT 0,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    rows_imported INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    filter_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_organization_import_runs_source
    ON organization_import_runs(source, started_at DESC);

-- Keep the organization graph warm as the existing collectors ingest new rows.
CREATE TRIGGER IF NOT EXISTS trg_purchase_parties_org
AFTER INSERT ON purchase_parties
WHEN length(trim(NEW.inn)) IN (10, 12) AND trim(NEW.inn) NOT GLOB '*[^0-9]*'
BEGIN
    INSERT OR IGNORE INTO organizations(inn) VALUES(trim(NEW.inn));
    UPDATE organizations SET last_seen_at=datetime('now') WHERE inn=trim(NEW.inn);
    INSERT OR IGNORE INTO organization_roles(inn, role) VALUES(trim(NEW.inn), NEW.role);
    UPDATE organization_roles SET last_seen_at=datetime('now')
      WHERE inn=trim(NEW.inn) AND role=NEW.role;
END;

CREATE TRIGGER IF NOT EXISTS trg_contract_suppliers_org
AFTER INSERT ON contract_suppliers
WHEN length(trim(NEW.inn)) IN (10, 12) AND trim(NEW.inn) NOT GLOB '*[^0-9]*'
BEGIN
    INSERT OR IGNORE INTO organizations(inn) VALUES(trim(NEW.inn));
    UPDATE organizations SET last_seen_at=datetime('now') WHERE inn=trim(NEW.inn);
    INSERT OR IGNORE INTO organization_roles(inn, role) VALUES(trim(NEW.inn), 'supplier');
    UPDATE organization_roles SET last_seen_at=datetime('now')
      WHERE inn=trim(NEW.inn) AND role='supplier';
END;

CREATE TRIGGER IF NOT EXISTS trg_contracts_customer_org
AFTER INSERT ON contracts
WHEN NEW.customer_inn IS NOT NULL
 AND length(trim(NEW.customer_inn)) IN (10, 12)
 AND trim(NEW.customer_inn) NOT GLOB '*[^0-9]*'
BEGIN
    INSERT OR IGNORE INTO organizations(inn) VALUES(trim(NEW.customer_inn));
    UPDATE organizations SET last_seen_at=datetime('now') WHERE inn=trim(NEW.customer_inn);
    INSERT OR IGNORE INTO organization_roles(inn, role) VALUES(trim(NEW.customer_inn), 'customer');
    UPDATE organization_roles SET last_seen_at=datetime('now')
      WHERE inn=trim(NEW.customer_inn) AND role='customer';
END;

CREATE TRIGGER IF NOT EXISTS trg_contracts_customer_org_update
AFTER UPDATE OF customer_inn ON contracts
WHEN NEW.customer_inn IS NOT NULL
 AND length(trim(NEW.customer_inn)) IN (10, 12)
 AND trim(NEW.customer_inn) NOT GLOB '*[^0-9]*'
BEGIN
    INSERT OR IGNORE INTO organizations(inn) VALUES(trim(NEW.customer_inn));
    UPDATE organizations SET last_seen_at=datetime('now') WHERE inn=trim(NEW.customer_inn);
    INSERT OR IGNORE INTO organization_roles(inn, role) VALUES(trim(NEW.customer_inn), 'customer');
    UPDATE organization_roles SET last_seen_at=datetime('now')
      WHERE inn=trim(NEW.customer_inn) AND role='customer';
END;

CREATE TRIGGER IF NOT EXISTS trg_contract_procedures_org
AFTER INSERT ON contract_procedures
BEGIN
    INSERT OR IGNORE INTO organizations(inn)
      SELECT trim(NEW.customer_inn)
      WHERE NEW.customer_inn IS NOT NULL
        AND length(trim(NEW.customer_inn)) IN (10,12)
        AND trim(NEW.customer_inn) NOT GLOB '*[^0-9]*';
    INSERT OR IGNORE INTO organization_roles(inn, role)
      SELECT trim(NEW.customer_inn), 'customer'
      WHERE NEW.customer_inn IS NOT NULL
        AND length(trim(NEW.customer_inn)) IN (10,12)
        AND trim(NEW.customer_inn) NOT GLOB '*[^0-9]*';
    INSERT OR IGNORE INTO organizations(inn)
      SELECT trim(NEW.participant_inn)
      WHERE NEW.participant_inn IS NOT NULL
        AND length(trim(NEW.participant_inn)) IN (10,12)
        AND trim(NEW.participant_inn) NOT GLOB '*[^0-9]*';
    INSERT OR IGNORE INTO organization_roles(inn, role)
      SELECT trim(NEW.participant_inn), 'procedure_participant'
      WHERE NEW.participant_inn IS NOT NULL
        AND length(trim(NEW.participant_inn)) IN (10,12)
        AND trim(NEW.participant_inn) NOT GLOB '*[^0-9]*';
END;

CREATE TRIGGER IF NOT EXISTS trg_gisp_products_org
AFTER INSERT ON gisp_products
WHEN NEW.manufacturer_inn IS NOT NULL
 AND length(trim(NEW.manufacturer_inn)) IN (10, 12)
 AND trim(NEW.manufacturer_inn) NOT GLOB '*[^0-9]*'
BEGIN
    INSERT OR IGNORE INTO organizations(
        inn, entity_type, ogrn, name, address
    ) VALUES(
        trim(NEW.manufacturer_inn), 'legal_entity', NULLIF(trim(NEW.manufacturer_ogrn), ''),
        NULLIF(trim(NEW.manufacturer_name), ''), NULLIF(trim(NEW.manufacturer_address), '')
    );
    UPDATE organizations SET
        entity_type=CASE WHEN entity_type='unknown' THEN 'legal_entity' ELSE entity_type END,
        ogrn=COALESCE(ogrn, NULLIF(trim(NEW.manufacturer_ogrn), '')),
        name=COALESCE(name, NULLIF(trim(NEW.manufacturer_name), '')),
        address=COALESCE(address, NULLIF(trim(NEW.manufacturer_address), '')),
        last_seen_at=datetime('now'), updated_at=datetime('now')
    WHERE inn=trim(NEW.manufacturer_inn);
    INSERT OR IGNORE INTO organization_roles(inn, role)
      VALUES(trim(NEW.manufacturer_inn), 'manufacturer');
END;

CREATE TRIGGER IF NOT EXISTS trg_gisp_products_org_update
AFTER UPDATE OF manufacturer_inn, manufacturer_ogrn, manufacturer_name, manufacturer_address ON gisp_products
WHEN NEW.manufacturer_inn IS NOT NULL
 AND length(trim(NEW.manufacturer_inn)) IN (10, 12)
 AND trim(NEW.manufacturer_inn) NOT GLOB '*[^0-9]*'
BEGIN
    INSERT OR IGNORE INTO organizations(inn, entity_type)
      VALUES(trim(NEW.manufacturer_inn), 'legal_entity');
    UPDATE organizations SET
        entity_type=CASE WHEN entity_type='unknown' THEN 'legal_entity' ELSE entity_type END,
        ogrn=COALESCE(ogrn, NULLIF(trim(NEW.manufacturer_ogrn), '')),
        name=COALESCE(name, NULLIF(trim(NEW.manufacturer_name), '')),
        address=COALESCE(address, NULLIF(trim(NEW.manufacturer_address), '')),
        last_seen_at=datetime('now'), updated_at=datetime('now')
    WHERE inn=trim(NEW.manufacturer_inn);
    INSERT OR IGNORE INTO organization_roles(inn, role)
      VALUES(trim(NEW.manufacturer_inn), 'manufacturer');
END;

CREATE TRIGGER IF NOT EXISTS trg_fsa_certificates_applicant_org
AFTER INSERT ON fsa_certificates
WHEN NEW.applicant_inn IS NOT NULL
 AND length(trim(NEW.applicant_inn)) IN (10, 12)
 AND trim(NEW.applicant_inn) NOT GLOB '*[^0-9]*'
BEGIN
    INSERT OR IGNORE INTO organizations(inn, ogrn, kpp, name, address)
      VALUES(trim(NEW.applicant_inn), NULLIF(trim(NEW.applicant_ogrn), ''),
             NULLIF(trim(NEW.applicant_kpp), ''), NULLIF(trim(NEW.applicant_name), ''),
             NULLIF(trim(NEW.applicant_address), ''));
    UPDATE organizations SET
        ogrn=COALESCE(ogrn, NULLIF(trim(NEW.applicant_ogrn), '')),
        kpp=COALESCE(kpp, NULLIF(trim(NEW.applicant_kpp), '')),
        name=COALESCE(name, NULLIF(trim(NEW.applicant_name), '')),
        address=COALESCE(address, NULLIF(trim(NEW.applicant_address), '')),
        last_seen_at=datetime('now'), updated_at=datetime('now')
    WHERE inn=trim(NEW.applicant_inn);
    INSERT OR IGNORE INTO organization_roles(inn, role)
      VALUES(trim(NEW.applicant_inn), 'fsa_applicant');
END;

CREATE TRIGGER IF NOT EXISTS trg_fsa_certificates_manufacturer_org
AFTER INSERT ON fsa_certificates
WHEN NEW.manufacturer_inn IS NOT NULL
 AND length(trim(NEW.manufacturer_inn)) IN (10, 12)
 AND trim(NEW.manufacturer_inn) NOT GLOB '*[^0-9]*'
BEGIN
    INSERT OR IGNORE INTO organizations(inn, ogrn, kpp, name, address)
      VALUES(trim(NEW.manufacturer_inn), NULLIF(trim(NEW.manufacturer_ogrn), ''),
             NULLIF(trim(NEW.manufacturer_kpp), ''), NULLIF(trim(NEW.manufacturer_name), ''),
             NULLIF(trim(NEW.manufacturer_address), ''));
    UPDATE organizations SET
        ogrn=COALESCE(ogrn, NULLIF(trim(NEW.manufacturer_ogrn), '')),
        kpp=COALESCE(kpp, NULLIF(trim(NEW.manufacturer_kpp), '')),
        name=COALESCE(name, NULLIF(trim(NEW.manufacturer_name), '')),
        address=COALESCE(address, NULLIF(trim(NEW.manufacturer_address), '')),
        last_seen_at=datetime('now'), updated_at=datetime('now')
    WHERE inn=trim(NEW.manufacturer_inn);
    INSERT OR IGNORE INTO organization_roles(inn, role)
      VALUES(trim(NEW.manufacturer_inn), 'fsa_manufacturer');
END;
"""

# Schema extension v14: keep organization enrichment fresh on source-row UPSERT updates.
SCHEMA += """
CREATE TRIGGER IF NOT EXISTS trg_contract_procedures_org_update
AFTER UPDATE OF customer_inn, participant_inn ON contract_procedures
BEGIN
    INSERT OR IGNORE INTO organizations(inn)
      SELECT trim(NEW.customer_inn)
      WHERE NEW.customer_inn IS NOT NULL
        AND length(trim(NEW.customer_inn)) IN (10,12)
        AND trim(NEW.customer_inn) NOT GLOB '*[^0-9]*';
    INSERT OR IGNORE INTO organization_roles(inn, role)
      SELECT trim(NEW.customer_inn), 'customer'
      WHERE NEW.customer_inn IS NOT NULL
        AND length(trim(NEW.customer_inn)) IN (10,12)
        AND trim(NEW.customer_inn) NOT GLOB '*[^0-9]*';
    INSERT OR IGNORE INTO organizations(inn)
      SELECT trim(NEW.participant_inn)
      WHERE NEW.participant_inn IS NOT NULL
        AND length(trim(NEW.participant_inn)) IN (10,12)
        AND trim(NEW.participant_inn) NOT GLOB '*[^0-9]*';
    INSERT OR IGNORE INTO organization_roles(inn, role)
      SELECT trim(NEW.participant_inn), 'procedure_participant'
      WHERE NEW.participant_inn IS NOT NULL
        AND length(trim(NEW.participant_inn)) IN (10,12)
        AND trim(NEW.participant_inn) NOT GLOB '*[^0-9]*';
    UPDATE organizations SET last_seen_at=datetime('now')
      WHERE inn IN (trim(NEW.customer_inn), trim(NEW.participant_inn));
END;

CREATE TRIGGER IF NOT EXISTS trg_fsa_certificates_org_update
AFTER UPDATE OF applicant_inn, applicant_ogrn, applicant_kpp, applicant_name, applicant_address,
                manufacturer_inn, manufacturer_ogrn, manufacturer_kpp, manufacturer_name,
                manufacturer_address ON fsa_certificates
BEGIN
    INSERT OR IGNORE INTO organizations(inn, ogrn, kpp, name, address)
      SELECT trim(NEW.applicant_inn), NULLIF(trim(NEW.applicant_ogrn), ''),
             NULLIF(trim(NEW.applicant_kpp), ''), NULLIF(trim(NEW.applicant_name), ''),
             NULLIF(trim(NEW.applicant_address), '')
      WHERE NEW.applicant_inn IS NOT NULL
        AND length(trim(NEW.applicant_inn)) IN (10,12)
        AND trim(NEW.applicant_inn) NOT GLOB '*[^0-9]*';
    UPDATE organizations SET
        ogrn=COALESCE(ogrn, NULLIF(trim(NEW.applicant_ogrn), '')),
        kpp=COALESCE(kpp, NULLIF(trim(NEW.applicant_kpp), '')),
        name=COALESCE(name, NULLIF(trim(NEW.applicant_name), '')),
        address=COALESCE(address, NULLIF(trim(NEW.applicant_address), '')),
        last_seen_at=datetime('now'), updated_at=datetime('now')
      WHERE inn=trim(NEW.applicant_inn)
        AND length(trim(NEW.applicant_inn)) IN (10,12);
    INSERT OR IGNORE INTO organization_roles(inn, role)
      SELECT trim(NEW.applicant_inn), 'fsa_applicant'
      WHERE NEW.applicant_inn IS NOT NULL
        AND length(trim(NEW.applicant_inn)) IN (10,12)
        AND trim(NEW.applicant_inn) NOT GLOB '*[^0-9]*';

    INSERT OR IGNORE INTO organizations(inn, ogrn, kpp, name, address)
      SELECT trim(NEW.manufacturer_inn), NULLIF(trim(NEW.manufacturer_ogrn), ''),
             NULLIF(trim(NEW.manufacturer_kpp), ''), NULLIF(trim(NEW.manufacturer_name), ''),
             NULLIF(trim(NEW.manufacturer_address), '')
      WHERE NEW.manufacturer_inn IS NOT NULL
        AND length(trim(NEW.manufacturer_inn)) IN (10,12)
        AND trim(NEW.manufacturer_inn) NOT GLOB '*[^0-9]*';
    UPDATE organizations SET
        ogrn=COALESCE(ogrn, NULLIF(trim(NEW.manufacturer_ogrn), '')),
        kpp=COALESCE(kpp, NULLIF(trim(NEW.manufacturer_kpp), '')),
        name=COALESCE(name, NULLIF(trim(NEW.manufacturer_name), '')),
        address=COALESCE(address, NULLIF(trim(NEW.manufacturer_address), '')),
        last_seen_at=datetime('now'), updated_at=datetime('now')
      WHERE inn=trim(NEW.manufacturer_inn)
        AND length(trim(NEW.manufacturer_inn)) IN (10,12);
    INSERT OR IGNORE INTO organization_roles(inn, role)
      SELECT trim(NEW.manufacturer_inn), 'fsa_manufacturer'
      WHERE NEW.manufacturer_inn IS NOT NULL
        AND length(trim(NEW.manufacturer_inn)) IN (10,12)
        AND trim(NEW.manufacturer_inn) NOT GLOB '*[^0-9]*';
END;
"""

# Schema extension v15: 44-FZ plan-schedules for forward-looking opportunities.
SCHEMA += """

CREATE TABLE IF NOT EXISTS tenderplans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_document_id INTEGER NOT NULL UNIQUE REFERENCES raw_documents(id) ON DELETE CASCADE,
    plan_number TEXT NOT NULL UNIQUE,
    published_at TEXT,
    plan_year INTEGER,
    region_code INTEGER,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_tenderplans_region_year
    ON tenderplans(region_code, plan_year);
CREATE INDEX IF NOT EXISTS ix_tenderplans_published
    ON tenderplans(published_at DESC);

CREATE TABLE IF NOT EXISTS tenderplan_customers (
    tenderplan_id INTEGER NOT NULL REFERENCES tenderplans(id) ON DELETE CASCADE,
    inn TEXT NOT NULL,
    PRIMARY KEY(tenderplan_id, inn)
);
CREATE INDEX IF NOT EXISTS ix_tenderplan_customers_inn
    ON tenderplan_customers(inn);

CREATE TABLE IF NOT EXISTS tenderplan_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenderplan_id INTEGER NOT NULL REFERENCES tenderplans(id) ON DELETE CASCADE,
    position_key TEXT NOT NULL,
    position_number TEXT,
    ikz TEXT,
    customer_inn TEXT,
    object_info TEXT,
    planned_at TEXT,
    planned_year INTEGER,
    planned_month INTEGER,
    amount REAL,
    raw_index INTEGER,
    UNIQUE(tenderplan_id, position_key)
);
CREATE INDEX IF NOT EXISTS ix_tenderplan_positions_number
    ON tenderplan_positions(position_number);
CREATE INDEX IF NOT EXISTS ix_tenderplan_positions_customer
    ON tenderplan_positions(customer_inn);
CREATE INDEX IF NOT EXISTS ix_tenderplan_positions_year_month
    ON tenderplan_positions(planned_year, planned_month);
CREATE INDEX IF NOT EXISTS ix_tenderplan_positions_amount
    ON tenderplan_positions(amount);

CREATE TABLE IF NOT EXISTS tenderplan_position_codes (
    position_id INTEGER NOT NULL REFERENCES tenderplan_positions(id) ON DELETE CASCADE,
    system TEXT NOT NULL CHECK(system IN ('okpd2','ktru')),
    code TEXT NOT NULL,
    PRIMARY KEY(position_id, system, code)
);
CREATE INDEX IF NOT EXISTS ix_tenderplan_position_codes_system_code
    ON tenderplan_position_codes(system, code);

CREATE TABLE IF NOT EXISTS tenderplan_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenderplan_id INTEGER NOT NULL REFERENCES tenderplans(id) ON DELETE CASCADE,
    doc_type TEXT,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tenderplan_documents_plan
    ON tenderplan_documents(tenderplan_id);

CREATE TABLE IF NOT EXISTS tenderplan_detail_fetches (
    plan_number TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL,
    position_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tenderplan_detail_fetches_status
    ON tenderplan_detail_fetches(status);
"""

# Schema extension v16: evidence-backed organization locality membership.
SCHEMA += """

CREATE TABLE IF NOT EXISTS organization_localities (
    inn TEXT NOT NULL REFERENCES organizations(inn) ON DELETE CASCADE,
    locality_key TEXT NOT NULL,
    locality_name TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    evidence TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (inn, locality_key, source)
);
CREATE INDEX IF NOT EXISTS ix_organization_localities_key
    ON organization_localities(locality_key, confidence DESC, inn);
"""

# Schema extension v17: bounded identity-enrichment journal for locality filtering.
SCHEMA += """

CREATE TABLE IF NOT EXISTS purchase_identity_fetches (
    purchase_number TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL,
    customer_count INTEGER NOT NULL DEFAULT 0,
    addressed_customers INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_purchase_identity_fetches_status
    ON purchase_identity_fetches(status, fetched_at DESC);
"""


# Schema extension v18: structured purchase-level locality evidence.
SCHEMA += """

CREATE TABLE IF NOT EXISTS purchase_localities (
    purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    locality_key TEXT NOT NULL,
    locality_name TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    evidence TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (purchase_id, locality_key, source)
);
CREATE INDEX IF NOT EXISTS ix_purchase_localities_key
    ON purchase_localities(locality_key, confidence DESC, purchase_id);
"""
