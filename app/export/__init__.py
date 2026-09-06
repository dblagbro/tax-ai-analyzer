"""Export all formats for an entity/year. Returns dict of {format: filepath}."""
import logging
from app.export import (
    csv_exporter, json_exporter, quickbooks, ofx_exporter, txf_exporter,
    pdf_report, zip_bundler, beancount_exporter,
)

logger = logging.getLogger(__name__)


def export_all(year: str, entity_slug: str, media_path: str = "/paperless/media") -> dict:
    files = {}
    errors = {}

    generators = [
        ("csv", lambda: csv_exporter.export_transactions_csv(year, entity_slug)),
        ("json", lambda: json_exporter.export_json(year, entity_slug)),
        ("iif", lambda: quickbooks.export_iif(year, entity_slug)),
        ("qbo", lambda: quickbooks.export_qbo(year, entity_slug)),
        ("ofx", lambda: ofx_exporter.export_ofx(year, entity_slug)),
        ("txf", lambda: txf_exporter.export_txf(year, entity_slug)),
        # 2026-09-06: plain-text double-entry ledger for accountants who don't
        # use QuickBooks Online. Validates with `bean-check`, browses with Fava.
        ("beancount", lambda: beancount_exporter.export_beancount(year, entity_slug)),
        ("pdf", lambda: pdf_report.export_pdf(year, entity_slug)),
    ]

    for fmt, fn in generators:
        try:
            files[fmt] = fn()
            logger.info(f"Exported {fmt} for {entity_slug}/{year}: {files[fmt]}")
        except Exception as e:
            errors[fmt] = str(e)
            logger.error(f"Export {fmt} failed: {e}")

    if files:
        try:
            files["zip"] = zip_bundler.create_bundle(year, entity_slug, list(files.values()), media_path)
        except Exception as e:
            errors["zip"] = str(e)

    return {"files": files, "errors": errors}
