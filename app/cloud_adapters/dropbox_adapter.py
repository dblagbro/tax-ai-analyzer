"""
Dropbox cloud adapter.

Stores OAuth2 token in DB settings under key 'dropbox_oauth_token'.

Dependencies:
    dropbox  (pip install dropbox)
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from typing import Optional

from app.cloud_adapters.base import CloudAdapter, CloudFile

logger = logging.getLogger(__name__)

# File extensions we consider importable
_IMPORTABLE_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif",
    ".doc", ".docx", ".xls", ".xlsx", ".csv",
}


class DropboxAdapter(CloudAdapter):
    """Dropbox read-only adapter backed by DB credential storage."""

    # ── credential storage ─────────────────────────────────────────────────────

    def _load_token(self) -> Optional[dict]:
        from app import db
        settings = db.get_settings()
        raw = settings.get("dropbox_oauth_token", "")
        # Also check legacy flat key
        if not raw:
            raw = settings.get("dropbox_token", "")
            if raw:
                # Legacy: plain access token string
                return {"access_token": raw, "token_type": "bearer"}
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            # Treat as plain access token string
            return {"access_token": raw, "token_type": "bearer"}

    def _save_token(self, token_data: dict):
        from app import db
        db.save_settings({"dropbox_oauth_token": json.dumps(token_data)})

    def _get_app_credentials(self) -> tuple[Optional[str], Optional[str]]:
        """Return (app_key, app_secret) from DB settings."""
        from app import db
        settings = db.get_settings()
        return (
            settings.get("dropbox_app_key", "").strip() or None,
            settings.get("dropbox_app_secret", "").strip() or None,
        )

    def _build_client(self):
        """Return an authenticated dropbox.Dropbox client."""
        import dropbox
        token_data = self._load_token()
        if not token_data:
            raise RuntimeError("Dropbox: not authenticated")

        access_token = token_data.get("access_token")
        if not access_token:
            raise RuntimeError("Dropbox: access token not found in stored credentials")

        return dropbox.Dropbox(access_token)

    # ── CloudAdapter interface ─────────────────────────────────────────────────

    def is_authenticated(self) -> bool:
        """Return True if a stored access token exists and is usable."""
        token = self._load_token()
        if not token or not token.get("access_token"):
            return False
        try:
            dbx = self._build_client()
            dbx.users_get_current_account()
            return True
        except Exception:
            return False

    def get_auth_url(self, redirect_uri: str) -> str:
        """
        Build Dropbox OAuth2 authorization URL (PKCE flow).

        Returns auth_url string. The caller should store the state token
        in their session for CSRF validation.
        """
        app_key, _ = self._get_app_credentials()
        if not app_key:
            raise RuntimeError(
                "Dropbox app_key not configured. Add dropbox_app_key in Settings."
            )

        import dropbox
        auth_flow = dropbox.DropboxOAuth2Flow(
            consumer_key=app_key,
            redirect_uri=redirect_uri,
            session={},
            csrf_token_session_key="dropbox_csrf",
            use_pkce=True,
            token_access_type="offline",
        )
        auth_url = auth_flow.start()
        return auth_url

    def get_auth_url_with_state(self, redirect_uri: str) -> tuple[str, str]:
        """
        Extended version that returns (auth_url, state) for CSRF protection.
        """
        app_key, _ = self._get_app_credentials()
        if not app_key:
            raise RuntimeError("Dropbox app_key not configured.")

        import dropbox
        state = secrets.token_urlsafe(16)
        session = {}
        auth_flow = dropbox.DropboxOAuth2Flow(
            consumer_key=app_key,
            redirect_uri=redirect_uri,
            session=session,
            csrf_token_session_key="dropbox_csrf",
            use_pkce=True,
            token_access_type="offline",
        )
        auth_url = auth_flow.start()
        # Extract state from URL for external storage
        csrf_token = session.get("dropbox_csrf", state)
        return auth_url, csrf_token

    def complete_auth(self, code: str, redirect_uri: str) -> bool:
        """
        Exchange authorization code for an access + refresh token.

        For simple token exchange without a full OAuth2 flow object,
        we use a direct POST to the Dropbox token endpoint.

        Returns:
            True on success.
        """
        app_key, app_secret = self._get_app_credentials()
        if not app_key:
            raise RuntimeError("Dropbox app_key not configured.")

        import urllib.request
        import urllib.parse
        import base64

        data = urllib.parse.urlencode({
            "code":         code,
            "grant_type":   "authorization_code",
            "redirect_uri": redirect_uri,
        }).encode()

        auth_str = f"{app_key}:{app_secret or ''}"
        auth_b64 = base64.b64encode(auth_str.encode()).decode()

        req = urllib.request.Request(
            "https://api.dropboxapi.com/oauth2/token",
            data=data,
            headers={
                "Authorization": f"Basic {auth_b64}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
            method="POST",
        )

        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())

        if "access_token" not in result:
            logger.error(f"Dropbox token exchange failed: {result}")
            return False

        self._save_token({
            "access_token":  result["access_token"],
            "refresh_token": result.get("refresh_token", ""),
            "token_type":    result.get("token_type", "bearer"),
            "account_id":    result.get("account_id", ""),
            "uid":           result.get("uid", ""),
        })
        return True

    def list_files(
        self,
        folder_id: str = None,
        query: str = "",
    ) -> list[CloudFile]:
        """
        List files in a Dropbox folder, filtered to importable types.

        Args:
            folder_id: Dropbox folder path (e.g. "/Tax Documents/2024").
                       Empty string or None means the root.
            query:     If provided, use Dropbox search API instead of folder listing.

        Returns:
            List of CloudFile objects.
        """
        dbx = self._build_client()
        folder_path = folder_id or ""

        files: list[CloudFile] = []

        try:
            if query:
                # Use search API
                import dropbox
                result = dbx.files_search_v2(query, options=dropbox.files.SearchOptions(
                    path=folder_path or None,
                    max_results=200,
                ))
                entries = [m.metadata.get_metadata() for m in result.matches]
                has_more = result.has_more
                cursor = result.cursor if has_more else None

                while has_more and cursor:
                    cont = dbx.files_search_continue_v2(cursor)
                    entries.extend([m.metadata.get_metadata() for m in cont.matches])
                    has_more = cont.has_more
                    cursor = cont.cursor if has_more else None
            else:
                # List folder recursively
                result = dbx.files_list_folder(folder_path, recursive=True)
                entries = result.entries
                has_more = result.has_more
                cursor = result.cursor

                while has_more:
                    cont = dbx.files_list_folder_continue(cursor)
                    entries.extend(cont.entries)
                    has_more = cont.has_more
                    cursor = cont.cursor

            import dropbox.files as dbx_files
            for entry in entries:
                if not isinstance(entry, dbx_files.FileMetadata):
                    continue  # skip folders / deleted entries

                name = entry.name
                ext = os.path.splitext(name)[1].lower()
                if ext not in _IMPORTABLE_EXTENSIONS:
                    continue

                files.append(CloudFile(
                    file_id   = entry.id,
                    name      = name,
                    size      = entry.size,
                    mime_type = _ext_to_mime(ext),
                    modified  = str(entry.server_modified),
                    path      = entry.path_display or "",
                ))

        except Exception as e:
            logger.error(f"Dropbox list_files failed: {e}")

        logger.info(f"Dropbox: found {len(files)} files in '{folder_path}'")
        return files

    def download_file(self, file_path: str) -> tuple[bytes, str]:
        """
        Download a file from Dropbox by its path or ID.

        Args:
            file_path: Dropbox file path (e.g. "/invoices/receipt.pdf")
                       or a file ID (e.g. "id:abc123...").

        Returns:
            (file_bytes, filename)
        """
        dbx = self._build_client()

        try:
            metadata, response = dbx.files_download(file_path)
            content = response.content
            filename = metadata.name
        except Exception as e:
            logger.error(f"Dropbox download_file failed for '{file_path}': {e}")
            raise

        return content, filename


def _ext_to_mime(ext: str) -> str:
    """Return a MIME type string for a common file extension."""
    _map = {
        ".pdf":  "application/pdf",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tiff": "image/tiff",
        ".tif":  "image/tiff",
        ".doc":  "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls":  "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".csv":  "text/csv",
    }
    return _map.get(ext, "application/octet-stream")
