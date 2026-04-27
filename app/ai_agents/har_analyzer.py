"""HAR (HTTP Archive) parsing utilities for the codegen agent.

A real bank-session HAR is too large (10–50 MB, hundreds of entries) to feed
verbatim into an LLM context window. We pre-digest it into a compact "flow
summary" — a list of the meaningful navigation/POST events with their URLs,
form fields, status codes, and select response markers — that the codegen
prompt can chew on cheaply.

We deliberately strip:
  - all binary/large response bodies
  - all third-party analytics/tag-manager/CDN traffic
  - cookies / Authorization headers (so we never leak the user's session)
  - sub-resource fetches (.css, .png, .woff, .js after the first few)
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Hosts we don't care about for importer codegen — analytics, tracking,
# CDN-served fonts/images. Substring match against the request host.
_NOISE_HOST_FRAGMENTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.com", "facebook.net", "fbcdn.net", "linkedin.com",
    "adsystem.com", "adservice.google", "twitter.com", "tiktok.com",
    "newrelic.com", "nr-data.net", "datadoghq.com", "segment.io",
    "optimizely.com", "fullstory.com", "hotjar.com", "mouseflow.com",
    "demdex.net", "krxd.net", "akamaihd.net", "akamaized.net",
    "cloudfront.net", "cloudflareinsights.com", "scorecardresearch.com",
    "everestech.net", "tealiumiq.com", "branch.io", "bugsnag.com",
)

# Ignore static-asset content types — we only want navigation / API calls.
_NOISE_CONTENT_TYPES = (
    "image/", "font/", "text/css", "video/", "audio/",
    "application/octet-stream", "application/font",
)

# Resource types we never need (HAR's _resourceType field, when present).
_NOISE_RESOURCE_TYPES = (
    "image", "font", "stylesheet", "media", "manifest", "websocket",
)


def parse_har(har_path: str, max_entries: int = 80) -> dict:
    """Read a HAR file from disk and return a compact flow summary.

    Returns a dict with keys:
      - host          : the primary host the user logged into
      - login_url     : best guess at the login page URL
      - flow          : list[dict] of meaningful entries (capped at max_entries)
      - form_posts    : list[dict] of POSTs that look like login/auth submissions
      - notable_urls  : list[str] of URLs that look like statements/downloads/AJAX
      - error         : error string if parsing failed (else "")
    """
    try:
        with open(har_path) as f:
            har = json.load(f)
    except Exception as e:
        return _empty_summary(error=f"could not read HAR: {e}")

    entries = (har.get("log") or {}).get("entries") or []
    if not entries:
        return _empty_summary(error="HAR has no entries")

    primary_host = _guess_primary_host(entries)

    flow: list[dict] = []
    form_posts: list[dict] = []
    notable_urls: list[str] = []
    login_url = ""

    for e in entries:
        compact = _compact_entry(e)
        if compact is None:
            continue

        url = compact["url"]
        host = urlparse(url).hostname or ""

        # Skip third-party noise unless it's a POST on the primary host
        if _is_noise_host(host):
            continue

        # Cap navigation flow at max_entries, but keep all primary-host POSTs +
        # download-looking URLs even past the cap.
        is_primary = (primary_host and primary_host in host)
        is_post = compact["method"] in ("POST", "PUT", "PATCH")
        is_download = _looks_like_download(url, compact.get("response_mime", ""))

        if len(flow) < max_entries or (is_primary and (is_post or is_download)):
            flow.append(compact)

        if is_primary and is_post:
            form_posts.append({
                "url": url,
                "status": compact["status"],
                "request_form": compact.get("request_form") or {},
                "request_json_keys": compact.get("request_json_keys") or [],
            })
            if not login_url and _looks_like_login(url, compact.get("request_form", {})):
                login_url = url

        if is_primary and (is_download or _looks_like_api(url)):
            notable_urls.append(url)

    return {
        "host": primary_host,
        "login_url": login_url or _first_doc_url(entries, primary_host),
        "flow": flow,
        "form_posts": form_posts,
        "notable_urls": notable_urls[:30],
        "error": "",
    }


def render_summary_for_prompt(summary: dict, max_chars: int = 25000) -> str:
    """Format a parse_har() summary as a compact text block for an LLM prompt.

    Keeps the per-entry detail (URL/method/status/form fields) but strips the
    Python dict noise. Soft-caps total character count so we don't blow up
    the context window on a HAR with 200+ kept entries.
    """
    if summary.get("error"):
        return f"(HAR parse error: {summary['error']})"

    lines = []
    lines.append(f"Primary host: {summary.get('host') or '(unknown)'}")
    lines.append(f"Likely login URL: {summary.get('login_url') or '(unknown)'}")
    lines.append("")

    form_posts = summary.get("form_posts") or []
    if form_posts:
        lines.append(f"== Form POSTs ({len(form_posts)}) ==")
        for fp in form_posts[:20]:
            keys = list((fp.get("request_form") or {}).keys()) \
                or fp.get("request_json_keys") or []
            lines.append(
                f"  POST {fp['url']} → {fp['status']}  fields={keys}"
            )
        lines.append("")

    notable = summary.get("notable_urls") or []
    if notable:
        lines.append(f"== Download / API URLs ({len(notable)}) ==")
        for u in notable[:20]:
            lines.append(f"  {u}")
        lines.append("")

    flow = summary.get("flow") or []
    if flow:
        lines.append(f"== Navigation flow ({len(flow)}) ==")
        for e in flow:
            lines.append(
                f"  {e['method']:6} {e['url']}  → {e['status']}"
                + (f"  ({e['response_mime']})" if e.get("response_mime") else "")
            )

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... (truncated at {max_chars} chars)"
    return text


# ── internals ────────────────────────────────────────────────────────────────

def _empty_summary(error: str = "") -> dict:
    return {
        "host": "", "login_url": "", "flow": [],
        "form_posts": [], "notable_urls": [], "error": error,
    }


def _guess_primary_host(entries: list) -> str:
    """The host with the most non-noise, non-static document hits."""
    counts: dict[str, int] = {}
    for e in entries:
        url = (e.get("request") or {}).get("url") or ""
        host = urlparse(url).hostname or ""
        if not host or _is_noise_host(host):
            continue
        rtype = e.get("_resourceType", "")
        if rtype in _NOISE_RESOURCE_TYPES:
            continue
        counts[host] = counts.get(host, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _is_noise_host(host: str) -> bool:
    if not host:
        return True
    return any(frag in host for frag in _NOISE_HOST_FRAGMENTS)


def _compact_entry(e: dict) -> dict | None:
    req = e.get("request") or {}
    resp = e.get("response") or {}
    url = req.get("url") or ""
    if not url:
        return None

    rtype = e.get("_resourceType", "")
    if rtype in _NOISE_RESOURCE_TYPES:
        return None

    resp_mime = (resp.get("content") or {}).get("mimeType") or ""
    if any(resp_mime.startswith(noise) for noise in _NOISE_CONTENT_TYPES):
        return None

    method = req.get("method", "GET")
    status = resp.get("status", 0)

    out = {
        "method": method,
        "url": url,
        "status": status,
        "response_mime": resp_mime.split(";")[0].strip(),
    }

    # POST/PUT/PATCH bodies — extract form fields or JSON keys
    if method in ("POST", "PUT", "PATCH"):
        post = req.get("postData") or {}
        params = post.get("params") or []
        if params:
            # Strip values for known sensitive field names
            form = {}
            for p in params:
                name = p.get("name", "")
                val = p.get("value", "")
                if _is_sensitive_field(name):
                    form[name] = "[REDACTED]"
                else:
                    form[name] = val[:80]  # cap value length
            out["request_form"] = form
        else:
            text = post.get("text") or ""
            if text and "json" in (post.get("mimeType") or "").lower():
                try:
                    j = json.loads(text)
                    if isinstance(j, dict):
                        # Just the keys — values may include passwords
                        out["request_json_keys"] = sorted(j.keys())[:20]
                except Exception:
                    pass

    return out


_SENSITIVE_FIELD_RE = re.compile(
    r"(password|passcode|pin|secret|token|cvv|ssn|otp|code|"
    r"answer|security)",
    re.I,
)


def _is_sensitive_field(name: str) -> bool:
    return bool(_SENSITIVE_FIELD_RE.search(name or ""))


def _looks_like_login(url: str, form: dict) -> bool:
    u = url.lower()
    if any(k in u for k in ("login", "logon", "auth", "signin", "sign-in")):
        return True
    if not form:
        return False
    fnames = " ".join(form.keys()).lower()
    return ("password" in fnames or "passcode" in fnames) and \
           ("user" in fnames or "id" in fnames or "email" in fnames or "login" in fnames)


def _looks_like_download(url: str, mime: str) -> bool:
    u = url.lower()
    if any(ext in u for ext in (".pdf", ".csv", ".qfx", ".qbo", ".ofx", ".xls", ".xlsx", "/download", "/export", "/statement")):
        return True
    if "pdf" in mime or "csv" in mime or "octet-stream" in mime:
        return True
    return False


def _looks_like_api(url: str) -> bool:
    u = url.lower()
    return "/api/" in u or "/ajax/" in u or "/rest/" in u or "/graphql" in u


def _first_doc_url(entries: list, primary_host: str) -> str:
    if not primary_host:
        return ""
    for e in entries:
        url = (e.get("request") or {}).get("url") or ""
        if primary_host not in url:
            continue
        rtype = e.get("_resourceType", "")
        if rtype == "document":
            return url
    return ""
