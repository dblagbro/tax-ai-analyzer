"""
Google Drive cloud adapter.

Stores OAuth2 token in DB settings under key 'google_drive_oauth_token'.

Dependencies:
    google-auth google-auth-oauthlib google-auth-httplib2
    google-api-python-client
"""
from __future__ import annotations

import io
import json
import logging
import os
import secrets
from typing import Optional

from app.cloud_adapters.base import CloudAdapter, CloudFile

logger = logging.getLogger(__name__)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# MIME types we consider importable documents
_IMPORTABLE_MIME_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/tiff",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/msword",
    "application/vnd.ms-excel",
    # Google Workspace types (exported to PDF on download)
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
}

# Google Workspace → export MIME type
_EXPORT_MAP = {
    "application/vnd.google-apps.document":    "application/pdf",
    "application/vnd.google-apps.spreadsheet": "application/pdf",
    "application/vnd.google-apps.presentation": "application/pdf",
    "application/vnd.google-apps.drawing":     "application/pdf",
}


def _google_imports():
    from google_auth_oauthlib.flow import Flow
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request
    return Flow, Credentials, build, Request


class GoogleDriveAdapter(CloudAdapter):
    """Google Drive read-only adapter backed by DB credential storage."""

    def __init__(self):
        self._credentials = None

    # ── credential storage ─────────────────────────────────────────────────────

    def _load_token(self) -> Optional[dict]:
        from app import db
        settings = db.get_settings()
        raw = settings.get("google_drive_oauth_token", "")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _save_token(self, token_data: dict):
        from app import db
        db.save_settings({"google_drive_oauth_token": json.dumps(token_data)})

    def _get_client_config(self) -> Optional[dict]:
        """
        Load OAuth client config.

        Priority:
          1. DB settings key 'google_drive_client_config'
          2. DB settings key 'gmail_client_config' (shared OAuth app)
          3. File at config.GMAIL_CREDENTIALS_FILE (shared credentials file)
        """
        from app import db, config
        settings = db.get_settings()

        for key in ("google_drive_client_config", "gmail_client_config"):
            raw = settings.get(key, "")
            if raw:
                try:
                    return json.loads(raw)
                except Exception:
                    pass

        creds_file = config.GMAIL_CREDENTIALS_FILE
        if os.path.exists(creds_file):
            with open(creds_file) as f:
                return json.load(f)

        return None

    # ── CloudAdapter interface ─────────────────────────────────────────────────

    def is_authenticated(self) -> bool:
        """Return True if a valid (possibly refreshable) token exists in DB."""
        try:
            creds = self._get_credentials()
            return creds is not None and creds.valid
        except Exception:
            return False

    def _get_credentials(self):
        """Return valid Credentials, refreshing if needed."""
        token = self._load_token()
        if not token:
            return None

        Flow, Credentials, _, Request = _google_imports()
        creds = Credentials(
            token=token.get("token"),
            refresh_token=token.get("refresh_token"),
            token_uri=token.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=token.get("client_id"),
            client_secret=token.get("client_secret"),
            scopes=token.get("scopes", DRIVE_SCOPES),
        )

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_token({
                    "token":         creds.token,
                    "refresh_token": creds.refresh_token,
                    "token_uri":     creds.token_uri,
                    "client_id":     creds.client_id,
                    "client_secret": creds.client_secret,
                    "scopes":        list(creds.scopes or DRIVE_SCOPES),
                })
            except Exception as e:
                logger.warning(f"Google Drive token refresh failed: {e}")
                return None

        return creds if creds.valid else None

    def get_auth_url(self, redirect_uri: str) -> str:
        """
        Build Google OAuth2 URL for Drive read-only scope.

        Returns auth_url as a string (state is stored separately by caller).
        """
        client_config = self._get_client_config()
        if not client_config:
            raise RuntimeError(
                "Google Drive credentials not configured. Upload credentials.json in Settings."
            )

        Flow, _, _, _ = _google_imports()
        flow = Flow.from_client_config(
            client_config,
            scopes=DRIVE_SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return auth_url

    def get_auth_url_with_state(self, redirect_uri: str) -> tuple[str, str]:
        """
        Extended version that also returns a CSRF state token.

        Returns:
            (auth_url, state)
        """
        client_config = self._get_client_config()
        if not client_config:
            raise RuntimeError("Google Drive credentials not configured.")

        Flow, _, _, _ = _google_imports()
        state = secrets.token_urlsafe(16)
        flow = Flow.from_client_config(
            client_config,
            scopes=DRIVE_SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state,
        )
        return auth_url, state

    def complete_auth(self, code: str, redirect_uri: str) -> bool:
        """
        Exchange authorization code for token and persist to DB.

        Returns:
            True on success.
        """
        client_config = self._get_client_config()
        if not client_config:
            raise RuntimeError("Google Drive credentials not configured.")

        Flow, _, _, _ = _google_imports()
        flow = Flow.from_client_config(
            client_config,
            scopes=DRIVE_SCOPES,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        self._save_token({
            "token":         creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri":     creds.token_uri,
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
            "scopes":        list(creds.scopes or DRIVE_SCOPES),
        })
        return True

    def list_files(
        self,
        folder_id: str = None,
        query: str = "",
    ) -> list[CloudFile]:
        """
        List files in Google Drive, filtered to importable document types.

        Args:
            folder_id: Restrict to files inside this folder (Drive folder ID).
            query:     Additional free-text Drive query clause (appended with AND).

        Returns:
            List of CloudFile objects.
        """
        creds = self._get_credentials()
        if not creds:
            logger.warning("Google Drive: not authenticated")
            return []

        _, _, build, _ = _google_imports()
        service = build("drive", "v3", credentials=creds)

        # Build query
        mime_clauses = " or ".join(
            f"mimeType='{m}'" for m in _IMPORTABLE_MIME_TYPES
        )
        q_parts = [f"({mime_clauses})", "trashed=false"]

        if folder_id:
            q_parts.append(f"'{folder_id}' in parents")

        if query:
            q_parts.append(f"({query})")

        q = " and ".join(q_parts)
        logger.debug(f"Google Drive list query: {q}")

        files: list[CloudFile] = []
        page_token = None

        while True:
            kwargs = {
                "q": q,
                "pageSize": 200,
                "fields": "nextPageToken, files(id, name, size, mimeType, modifiedTime, parents)",
                "orderBy": "modifiedTime desc",
            }
            if page_token:
                kwargs["pageToken"] = page_token

            result = service.files().list(**kwargs).execute()
            for f in result.get("files", []):
                files.append(CloudFile(
                    file_id   = f.get("id", ""),
                    name      = f.get("name", ""),
                    size      = int(f.get("size", 0) or 0),
                    mime_type = f.get("mimeType", ""),
                    modified  = f.get("modifiedTime", ""),
                    path      = folder_id or "",
                ))

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        logger.info(f"Google Drive: found {len(files)} files")
        return files

    def download_file(self, file_id: str) -> tuple[bytes, str]:
        """
        Download a file from Google Drive.

        Google Workspace documents (Docs, Sheets, etc.) are exported to PDF
        automatically.

        Returns:
            (file_bytes, filename)
        """
        creds = self._get_credentials()
        if not creds:
            raise RuntimeError("Google Drive: not authenticated")

        _, _, build, _ = _google_imports()
        service = build("drive", "v3", credentials=creds)

        # Get file metadata first
        meta = service.files().get(
            fileId=file_id,
            fields="id,name,mimeType"
        ).execute()

        name      = meta.get("name", file_id)
        mime_type = meta.get("mimeType", "")

        if mime_type in _EXPORT_MAP:
            # Google Workspace doc — export as PDF
            content = self.export_google_doc(file_id, _EXPORT_MAP[mime_type])
            if not name.lower().endswith(".pdf"):
                name = f"{name}.pdf"
        else:
            # Binary download
            from googleapiclient.http import MediaIoBaseDownload
            buf = io.BytesIO()
            request = service.files().get_media(fileId=file_id)
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            content = buf.getvalue()

        return content, name

    def export_google_doc(self, file_id: str, mime_type: str = "application/pdf") -> bytes:
        """
        Export a Google Workspace document (Doc, Sheet, Slides) to the
        specified MIME type (default: PDF).

        Returns:
            Raw file bytes.
        """
        creds = self._get_credentials()
        if not creds:
            raise RuntimeError("Google Drive: not authenticated")

        _, _, build, _ = _google_imports()
        service = build("drive", "v3", credentials=creds)

        response = service.files().export_media(
            fileId=file_id,
            mimeType=mime_type,
        ).execute()

        # export_media returns bytes directly (not a MediaIoBaseDownload)
        if isinstance(response, bytes):
            return response

        # Fallback: wrap in BytesIO and use downloader
        import io as _io
        from googleapiclient.http import MediaIoBaseDownload
        buf = _io.BytesIO()
        request = service.files().export_media(fileId=file_id, mimeType=mime_type)
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()
