#!/usr/bin/env python3
"""Financial AI Analyzer — main entry point."""
import logging
import os
import sys
import threading
import time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)
# LOW-PASS2-2: httpx/urllib3 emit an INFO line per HTTP request; the analysis
# daemon polls Paperless every ~60s which produced ~26 MB/day of logs when
# idle. WARNING+ is enough for diagnostics — errors still surface.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Activity log for web UI
_activity_log: list[str] = []
_analysis_status = {"running": False, "last_run": None, "analyzed_this_cycle": 0, "mode": None}


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}"
    logger.info(msg)
    _activity_log.append(entry)
    if len(_activity_log) > 500:
        _activity_log.pop(0)


def get_activity_log() -> list[str]:
    return list(reversed(_activity_log[-100:]))


def get_analysis_status() -> dict:
    return _analysis_status.copy()


def analysis_daemon():
    """Background thread: continuously analyze new Paperless documents.

    Per-document work lives in app.analysis_core.process_document (shared
    with the manual /api/analyze/trigger route). This loop only decides
    WHICH documents to process and in which mode.
    """
    from app import db, config
    from app.paperless_client import PaperlessClient
    from app.llm_client import LLMClient, has_llm_capability
    from app.analysis_core import process_document

    _log("Analysis daemon started")

    while True:
        try:
            _analysis_status["running"] = True

            # Get LLM config from DB (allows runtime override)
            llm_provider = db.get_setting("llm_provider") or config.LLM_PROVIDER
            llm_api_key = db.get_setting("llm_api_key") or config.LLM_API_KEY
            llm_model = db.get_setting("llm_model") or config.LLM_MODEL
            paperless_token = db.get_setting("paperless_api_token") or config.PAPERLESS_API_TOKEN

            # Fixed 2026-09-05 per llm-proxy2 ops feedback: gate on
            # has_llm_capability(), which accepts EITHER direct-SDK key OR a
            # configured proxy endpoint. The previous `if not llm_api_key`
            # gate silently skipped the daemon for months while the proxy
            # chain worked, because LLM_API_KEY has been empty since 2026-05-01.
            #
            # 2026-09-06: no LLM no longer means "do nothing". New documents
            # get a provisional rules-only classification (app.rules_analyzer)
            # and are re-analyzed by the LLM as soon as it is reachable again.
            llm_ok = has_llm_capability(llm_api_key)
            mode = "llm" if llm_ok else "rules-only"
            if _analysis_status.get("mode") != mode:
                _log(f"Analysis mode: {mode}" + ("" if llm_ok else
                     " (no API key AND no enabled proxy endpoint) — new documents get a "
                     "provisional rules-based classification and are re-analyzed when the LLM is back"))
            _analysis_status["mode"] = mode

            client = PaperlessClient(token=paperless_token)
            llm = LLMClient(provider=llm_provider, api_key=llm_api_key, model=llm_model) if llm_ok else None

            # Get all Paperless doc IDs
            all_ids = client.get_all_document_ids()
            analyzed_ids = db.get_analyzed_doc_ids()
            new_ids = [d for d in all_ids if d not in analyzed_ids]
            batch = new_ids[:20]

            # With the LLM back, use spare capacity in this cycle to upgrade
            # provisional (rules-only) results, oldest first.
            redo_ids: list = []
            if llm_ok and len(batch) < 20:
                all_set = set(all_ids)
                redo_ids = [d for d in db.get_provisional_doc_ids() if d in all_set][:20 - len(batch)]
                batch += redo_ids
            redo_set = set(redo_ids)

            if new_ids:
                _log(f"Found {len(new_ids)} unanalyzed documents (processing up to 20, mode={mode})")
            if redo_ids:
                _log(f"Re-analyzing {len(redo_ids)} provisional documents with the LLM")

            analyzed_this_cycle = 0
            llm_failures = 0
            for doc_id in batch:
                if doc_id in redo_set and not llm_ok:
                    continue  # LLM went away mid-cycle; leave provisional rows for later
                try:
                    doc = client.get_document(doc_id)
                    _log(f"Analyzing doc {doc_id}: {str(doc.get('title', ''))[:50]}")
                    out = process_document(doc_id, doc, llm=llm, llm_ok=llm_ok, client=client, log=_log)
                    if out["status"] == "empty":
                        continue
                    if out["llm_failed"]:
                        llm_failures += 1
                        if llm_failures >= 3 and llm_ok:
                            llm_ok = False
                            _analysis_status["mode"] = "rules-only"
                            _log("3 LLM failures this cycle — rules-only for the rest of it")
                    analyzed_this_cycle += 1
                except Exception as e:
                    _log(f"Error analyzing doc {doc_id}: {e}")
                    import traceback
                    _log(traceback.format_exc()[:300])

            _analysis_status["analyzed_this_cycle"] = analyzed_this_cycle
            _analysis_status["last_run"] = datetime.utcnow().isoformat()

            # Auto-dedup: if anything was analyzed this cycle, re-scan for duplicates
            if analyzed_this_cycle > 0:
                try:
                    result = db.flag_duplicate_analyzed_docs()
                    if result["flagged"] > 0:
                        _log(f"Auto-dedup: flagged {result['flagged']} new duplicates "
                             f"across {result['groups']} groups")
                except Exception as de:
                    _log(f"Auto-dedup error: {de}")

        except Exception as e:
            _log(f"Analysis daemon cycle error: {e}")
        finally:
            _analysis_status["running"] = False
            # Phase 14B: write a heartbeat so /api/health/extended knows
            # we're alive even when the endpoint is hit from a different
            # process. Best-effort — a heartbeat write failure must never
            # take down the daemon thread.
            try:
                from app import db as _db
                _db.record_heartbeat("analysis-daemon")
            except Exception as hb_err:
                _log(f"Heartbeat write failed (non-fatal): {hb_err}")

        time.sleep(config.POLL_INTERVAL)


def main():
    from app import db, config

    # Initialize
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.EXPORT_PATH, exist_ok=True)
    os.makedirs(config.CONSUME_PATH, exist_ok=True)

    db.init_db()
    db.ensure_default_data()
    config.validate()
    _log("Financial AI Analyzer starting...")
    _log(f"Web UI: http://0.0.0.0:{config.WEB_PORT}{config.URL_PREFIX}/")

    # Seed PDF hash store from any PDFs still sitting in the consume directory
    # (catches files dropped but not yet ingested by Paperless)
    try:
        seeded = 0
        for root, dirs, files in os.walk(config.CONSUME_PATH):
            for fname in files:
                if not fname.lower().endswith(".pdf"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "rb") as f:
                        data = f.read()
                    parts = root.replace(config.CONSUME_PATH, "").strip("/").split("/")
                    entity_slug = parts[0] if parts else ""
                    year = parts[1] if len(parts) > 1 else ""
                    is_new = db.record_pdf_hash(
                        __import__("hashlib").sha256(data).hexdigest(),
                        source="consume_seed", filename=fname,
                        entity_slug=entity_slug, year=year,
                    )
                    if is_new:
                        seeded += 1
                except Exception:
                    pass
        if seeded:
            _log(f"Seeded {seeded} PDF hashes from consume directory")
    except Exception as e:
        _log(f"Consume dir hash seed error: {e}")

    # Start analysis daemon
    daemon = threading.Thread(target=analysis_daemon, daemon=True, name="analysis-daemon")
    daemon.start()

    # Backfill vendor_normalized for existing transactions on startup
    try:
        from app.dedup import backfill_vendor_normalized
        n = backfill_vendor_normalized()
        if n:
            _log(f"Backfilled vendor_normalized for {n} transactions")
    except Exception as e:
        _log(f"vendor_normalized backfill error: {e}")

    # Daily dedup scan — runs at startup then every 24 hours
    def _daily_dedup():
        while True:
            try:
                result = db.flag_duplicate_analyzed_docs()
                if result["flagged"] or result["already_flagged"]:
                    _log(f"Scheduled dedup scan: {result['flagged']} newly flagged, "
                         f"{result['already_flagged']} already flagged, "
                         f"{result['groups']} total groups")
                hash_stats = db.pdf_hash_stats()
                _log(f"PDF hash store: {hash_stats['total']} entries "
                     f"({', '.join(f'{v} {k}' for k,v in hash_stats['by_source'].items())})")
            except Exception as e:
                _log(f"Scheduled dedup error: {e}")
            try:
                from app.dedup import scan_cross_source_matches
                xs = scan_cross_source_matches()
                if xs["links_created"] or xs["links_updated"]:
                    _log(f"Cross-source dedup: {xs['links_created']} new links, "
                         f"{xs['links_updated']} updated, {xs['scanned']} txns scanned")
            except Exception as e:
                _log(f"Cross-source dedup error: {e}")
            try:
                pruned = db.prune_old_import_jobs(days=90)
                if pruned:
                    _log(f"Pruned {pruned} old import jobs (>90 days)")
            except Exception as e:
                _log(f"Import job prune error: {e}")
            # Phase 14B heartbeat (see analysis daemon for rationale).
            try:
                db.record_heartbeat("dedup-scheduler")
            except Exception as hb_err:
                _log(f"Dedup heartbeat write failed (non-fatal): {hb_err}")
            time.sleep(86400)  # 24 hours

    dedup_thread = threading.Thread(target=_daily_dedup, daemon=True, name="dedup-scheduler")
    dedup_thread.start()

    # Start Flask
    from app.web_ui import app as flask_app
    flask_app.run(
        host="0.0.0.0",
        port=config.WEB_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
