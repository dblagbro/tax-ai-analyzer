"""
Paperless-ngx API client for Financial AI Analyzer.

Features:
  - Reads base URL and token from DB settings at call time (UI-configurable)
  - Retry logic with exponential backoff on 5xx errors (3 attempts)
  - Tag ID cache to avoid repeated lookups
  - Full document CRUD + consume endpoint upload
  - httpx with 30s timeout
"""
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Tag cache (in-process, per-base-url) ──────────────────────────────────────
_tag_cache: dict = {}  # {base_url: {tag_name: tag_id}}


def _get_config() -> tuple:
    """
    Return (base_url, token), resolving from DB settings at call time
    with fallback to environment variables.
    """
    from app import db
    from app import config

    base_url = (
        db.get_setting("paperless_url")
        or config.PAPERLESS_API_BASE_URL
    ).rstrip("/")
    token = (
        db.get_setting("paperless_token")
        or config.PAPERLESS_API_TOKEN
    )
    return base_url, token


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
    }


def _request(
    method: str,
    url: str,
    token: str,
    retries: int = 3,
    backoff: float = 1.0,
    **kwargs,
) -> httpx.Response:
    """
    Execute an HTTP request with retry logic on 5xx errors.

    Raises httpx.HTTPStatusError on unrecoverable failure.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.request(
                    method, url, headers=_headers(token), **kwargs
                )
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp
            # 5xx — log and retry
            logger.warning(
                f"Paperless API {method} {url} returned {resp.status_code} "
                f"(attempt {attempt}/{retries})"
            )
            last_exc = httpx.HTTPStatusError(
                f"Server error {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        except httpx.TimeoutException as e:
            logger.warning(f"Paperless API timeout on attempt {attempt}/{retries}: {e}")
            last_exc = e
        except httpx.HTTPStatusError:
            raise  # 4xx — do not retry
        except httpx.RequestError as e:
            logger.warning(
                f"Paperless API request error on attempt {attempt}/{retries}: {e}"
            )
            last_exc = e

        if attempt < retries:
            sleep_time = backoff * (2 ** (attempt - 1))
            time.sleep(sleep_time)

    raise last_exc or RuntimeError(
        f"Paperless API request failed after {retries} attempts"
    )


# ── Documents ─────────────────────────────────────────────────────────────────

def get_documents(
    page: int = 1,
    page_size: int = 25,
    tag_filter: Optional[list] = None,
    ordering: str = "-created",
) -> dict:
    """
    Fetch a page of documents from Paperless.

    Args:
        page: Page number (1-based)
        page_size: Results per page
        tag_filter: Optional list of tag names — documents must have ALL these tags
        ordering: API ordering field (default newest first)

    Returns raw API response dict with 'count', 'next', 'previous', 'results'.
    """
    base_url, token = _get_config()
    params: dict = {"page": page, "page_size": page_size, "ordering": ordering}

    if tag_filter:
        tag_ids = []
        for name in tag_filter:
            tid = get_or_create_tag(name)
            if tid:
                tag_ids.append(str(tid))
        if tag_ids:
            params["tags__id__all"] = ",".join(tag_ids)

    resp = _request("GET", f"{base_url}/api/documents/", token, params=params)
    return resp.json()


def get_document(doc_id: int) -> dict:
    """Fetch a single document by ID."""
    base_url, token = _get_config()
    resp = _request("GET", f"{base_url}/api/documents/{doc_id}/", token)
    return resp.json()


def get_all_document_ids(tag_filter: Optional[list] = None) -> list:
    """
    Fetch all document IDs from Paperless, iterating through all pages.

    Returns list of integer document IDs.
    """
    ids = []
    page = 1
    while True:
        data = get_documents(page=page, page_size=100, tag_filter=tag_filter)
        for doc in data.get("results", []):
            ids.append(doc["id"])
        if not data.get("next"):
            break
        page += 1
    return ids


def get_document_content(doc_id: int) -> str:
    """Return the OCR-extracted text content of a document."""
    doc = get_document(doc_id)
    return doc.get("content", "") or ""


def get_document_download_url(doc_id: int) -> str:
    """Return the download URL for the original document file."""
    base_url, _ = _get_config()
    return f"{base_url}/api/documents/{doc_id}/download/"


def download_document(doc_id: int) -> bytes:
    """Download the original document file and return raw bytes."""
    base_url, token = _get_config()
    resp = _request("GET", f"{base_url}/api/documents/{doc_id}/download/", token)
    return resp.content


def get_document_preview(doc_id: int) -> bytes:
    """Download the document preview image (first page PNG) and return raw bytes."""
    base_url, token = _get_config()
    resp = _request("GET", f"{base_url}/api/documents/{doc_id}/preview/", token)
    return resp.content


def patch_document(doc_id: int, data: dict) -> dict:
    """
    Partially update a document (PATCH).

    Common fields: title, tags, correspondent, document_type, custom_fields
    """
    base_url, token = _get_config()
    resp = _request("PATCH", f"{base_url}/api/documents/{doc_id}/", token, json=data)
    return resp.json()


def update_document(doc_id: int, data: dict) -> dict:
    """Alias for patch_document."""
    return patch_document(doc_id, data)


def delete_document(doc_id: int):
    """Delete a document from Paperless (permanent)."""
    base_url, token = _get_config()
    _request("DELETE", f"{base_url}/api/documents/{doc_id}/", token)


# ── Tags ──────────────────────────────────────────────────────────────────────

def _get_tag_cache_for(base_url: str) -> dict:
    if base_url not in _tag_cache:
        _tag_cache[base_url] = {}
    return _tag_cache[base_url]


def _refresh_tag_cache(base_url: str, token: str):
    """Reload entire tag list from Paperless into the local cache."""
    cache = _get_tag_cache_for(base_url)
    page = 1
    while True:
        resp = _request(
            "GET",
            f"{base_url}/api/tags/",
            token,
            params={"page": page, "page_size": 200},
        )
        data = resp.json()
        for t in data.get("results", []):
            cache[t["name"]] = t["id"]
        if not data.get("next"):
            break
        page += 1


def get_all_tags() -> dict:
    """Return {tag_name: tag_id} mapping, refreshing the cache."""
    base_url, token = _get_config()
    _refresh_tag_cache(base_url, token)
    return dict(_get_tag_cache_for(base_url))


def get_or_create_tag(name: str) -> int:
    """
    Return the ID for a tag, creating it if it doesn't exist.
    Results are cached in-process to minimize API round-trips.
    """
    base_url, token = _get_config()
    cache = _get_tag_cache_for(base_url)

    if name in cache:
        return cache[name]

    # Refresh cache and check again
    _refresh_tag_cache(base_url, token)
    if name in cache:
        return cache[name]

    # Create the tag
    resp = _request(
        "POST", f"{base_url}/api/tags/", token, json={"name": name}
    )
    tag_id = resp.json()["id"]
    cache[name] = tag_id
    logger.info(f"Created Paperless tag: '{name}' (id={tag_id})")
    return tag_id


def invalidate_tag_cache():
    """Clear the tag cache (call after bulk tag operations)."""
    _tag_cache.clear()


def apply_tags(doc_id: int, tag_names: list):
    """
    Apply tags to a document, merging with any existing tags.

    Args:
        doc_id: Paperless document ID
        tag_names: List of tag name strings to add
    """
    if not tag_names:
        return

    tag_ids = [get_or_create_tag(name) for name in tag_names]
    doc = get_document(doc_id)
    existing_ids = doc.get("tags", [])
    merged = list(set(existing_ids) | set(tag_ids))
    patch_document(doc_id, {"tags": merged})
    logger.debug(f"Applied tags {tag_names} to document {doc_id}")


def set_tags(doc_id: int, tag_names: list):
    """
    Set a document's tags to exactly the given list (replaces existing tags).
    """
    tag_ids = [get_or_create_tag(name) for name in tag_names]
    patch_document(doc_id, {"tags": tag_ids})


# ── Upload (consume endpoint) ─────────────────────────────────────────────────

def upload_document(
    file_bytes: bytes,
    filename: str,
    title: str = "",
    tag_names: Optional[list] = None,
    correspondent: Optional[str] = None,
) -> dict:
    """
    Upload a document to Paperless via the post_document consume endpoint.

    Args:
        file_bytes: Raw file content (PDF, image, etc.)
        filename: Filename including extension (e.g. "invoice_2024.pdf")
        title: Optional title override
        tag_names: Optional list of tags to apply after upload
        correspondent: Optional correspondent name (unused in POST, for future use)

    Returns API response dict.
    """
    base_url, token = _get_config()

    # Resolve tags to IDs
    tag_ids = []
    if tag_names:
        for name in tag_names:
            try:
                tag_ids.append(get_or_create_tag(name))
            except Exception as e:
                logger.warning(f"Could not resolve tag '{name}': {e}")

    # Determine MIME type
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type_map = {
        "pdf": "application/pdf",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "tiff": "image/tiff",
        "tif": "image/tiff",
        "gif": "image/gif",
    }
    mime = content_type_map.get(ext, "application/octet-stream")

    files = {"document": (filename, file_bytes, mime)}
    form_data: dict = {}
    if title:
        form_data["title"] = title
    if tag_ids:
        form_data["tags"] = tag_ids

    try:
        resp = _request(
            "POST",
            f"{base_url}/api/documents/post_document/",
            token,
            files=files,
            data=form_data,
        )
        result = resp.json() if resp.content else {"status": "queued"}
        logger.info(f"Uploaded document '{filename}' to Paperless")
        return result
    except Exception as e:
        logger.error(f"Failed to upload document '{filename}': {e}")
        raise


# ── Correspondents ────────────────────────────────────────────────────────────

def get_or_create_correspondent(name: str) -> int:
    """Return ID for a correspondent, creating if it doesn't exist."""
    base_url, token = _get_config()
    resp = _request(
        "GET", f"{base_url}/api/correspondents/", token,
        params={"name__iexact": name},
    )
    data = resp.json()
    if data.get("results"):
        return data["results"][0]["id"]
    resp = _request(
        "POST", f"{base_url}/api/correspondents/", token, json={"name": name}
    )
    return resp.json()["id"]


# ── Document types ────────────────────────────────────────────────────────────

def get_or_create_document_type(name: str) -> int:
    """Return ID for a document type, creating if it doesn't exist."""
    base_url, token = _get_config()
    resp = _request(
        "GET", f"{base_url}/api/document_types/", token,
        params={"name__iexact": name},
    )
    data = resp.json()
    if data.get("results"):
        return data["results"][0]["id"]
    resp = _request(
        "POST", f"{base_url}/api/document_types/", token, json={"name": name}
    )
    return resp.json()["id"]


# ── Bulk helpers ──────────────────────────────────────────────────────────────

def get_documents_by_tag(tag_name: str, max_results: int = 1000) -> list:
    """Return all documents that have a specific tag."""
    docs = []
    page = 1
    while len(docs) < max_results:
        data = get_documents(page=page, page_size=100, tag_filter=[tag_name])
        docs.extend(data.get("results", []))
        if not data.get("next") or len(docs) >= max_results:
            break
        page += 1
    return docs[:max_results]


def get_documents_since(doc_id: int, limit: int = 200) -> list:
    """
    Return documents with ID greater than doc_id (newer documents).
    Useful for incremental polling.
    """
    base_url, token = _get_config()
    docs = []
    page = 1
    while len(docs) < limit:
        params = {
            "page": page,
            "page_size": 100,
            "ordering": "id",
            "id__gt": doc_id,
        }
        resp = _request("GET", f"{base_url}/api/documents/", token, params=params)
        data = resp.json()
        docs.extend(data.get("results", []))
        if not data.get("next"):
            break
        page += 1
    return docs[:limit]


# ── Health check ──────────────────────────────────────────────────────────────

def health_check() -> dict:
    """
    Check connectivity to the Paperless instance.

    Returns {"ok": bool, "base_url": str, "error": str or None}
    """
    base_url, token = _get_config()
    try:
        _request("GET", f"{base_url}/api/", token, retries=1)
        return {"ok": True, "base_url": base_url, "error": None}
    except Exception as e:
        return {"ok": False, "base_url": base_url, "error": str(e)}


# ── Object-oriented wrapper (used by analysis daemon) ─────────────────────────

class PaperlessClient:
    """
    Object-oriented wrapper around the module-level Paperless functions.
    Accepts optional base_url and token constructor arguments that override
    the DB/env defaults — useful when the daemon has already resolved config.
    """

    def __init__(self, base_url: str = None, token: str = None):
        self._base_url = base_url
        self._token = token

    def _patch_config(self):
        """Temporarily override module-level config if constructor args were given."""
        # We inject overrides by calling the underlying functions with explicit params.
        # Since the module functions call _get_config() internally we can't cleanly
        # inject per-call — instead we expose simple delegation methods below.
        pass

    def get_all_document_ids(self) -> list:
        if self._base_url or self._token:
            return _get_all_document_ids_direct(self._base_url, self._token)
        return get_all_document_ids()

    def get_document(self, doc_id: int) -> dict:
        if self._base_url or self._token:
            return _get_document_direct(doc_id, self._base_url, self._token)
        return get_document(doc_id)

    def apply_tags(self, doc_id: int, tag_names: list):
        if self._base_url or self._token:
            _apply_tags_direct(doc_id, tag_names, self._base_url, self._token)
        else:
            apply_tags(doc_id, tag_names)


def _resolve_base_token(base_url, token):
    """Resolve base_url/token with fallback to module defaults."""
    if base_url and token:
        return base_url.rstrip("/"), token
    default_base, default_token = _get_config()
    return (base_url or default_base).rstrip("/"), token or default_token


def _get_all_document_ids_direct(base_url: str, token: str) -> list:
    base_url, token = _resolve_base_token(base_url, token)
    ids = []
    page = 1
    while True:
        resp = _request("GET", f"{base_url}/api/documents/", token,
                        params={"page": page, "page_size": 100})
        data = resp.json()
        for doc in data.get("results", []):
            ids.append(doc["id"])
        if not data.get("next"):
            break
        page += 1
    return ids


def _get_document_direct(doc_id: int, base_url: str, token: str) -> dict:
    base_url, token = _resolve_base_token(base_url, token)
    resp = _request("GET", f"{base_url}/api/documents/{doc_id}/", token)
    return resp.json()


def _apply_tags_direct(doc_id: int, tag_names: list, base_url: str, token: str):
    base_url, token = _resolve_base_token(base_url, token)
    # Resolve tag IDs
    tag_ids = []
    for name in tag_names:
        # Check cache first
        cache = _get_tag_cache_for(base_url)
        if name not in cache:
            _refresh_tag_cache(base_url, token)
        if name in cache:
            tag_ids.append(cache[name])
        else:
            resp = _request("POST", f"{base_url}/api/tags/", token, json={"name": name})
            tid = resp.json()["id"]
            cache[name] = tid
            tag_ids.append(tid)
    # Merge with existing tags
    resp = _request("GET", f"{base_url}/api/documents/{doc_id}/", token)
    existing = resp.json().get("tags", [])
    merged = list(set(existing) | set(tag_ids))
    _request("PATCH", f"{base_url}/api/documents/{doc_id}/", token, json={"tags": merged})
