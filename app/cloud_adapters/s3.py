"""
Amazon S3 cloud adapter.

Reads credentials from DB settings:
  s3_access_key, s3_secret_key, s3_bucket, s3_region

Dependencies:
    boto3  (pip install boto3)
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from app.cloud_adapters.base import CloudAdapter, CloudFile

logger = logging.getLogger(__name__)

# Only surface these file types from S3
_IMPORTABLE_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif",
    ".doc", ".docx", ".xls", ".xlsx", ".csv",
}


def _ext_to_mime(ext: str) -> str:
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


class S3Adapter(CloudAdapter):
    """AWS S3 read-only adapter backed by DB credential storage."""

    def __init__(self):
        self._client = None

    # ── credentials ────────────────────────────────────────────────────────────

    def _load_credentials(self) -> dict:
        """Return credential dict from DB settings."""
        from app import db
        settings = db.get_settings()
        return {
            "access_key": settings.get("s3_access_key", "").strip(),
            "secret_key": settings.get("s3_secret_key", "").strip(),
            "bucket":     settings.get("s3_bucket", "").strip(),
            "region":     settings.get("s3_region", "us-east-1").strip(),
        }

    def is_configured(self) -> bool:
        """Return True if access_key, secret_key, and bucket are all set."""
        creds = self._load_credentials()
        return bool(creds["access_key"] and creds["secret_key"] and creds["bucket"])

    def _get_client(self):
        """Return a boto3 S3 client, creating it if needed."""
        import boto3
        creds = self._load_credentials()
        return boto3.client(
            "s3",
            aws_access_key_id     = creds["access_key"],
            aws_secret_access_key = creds["secret_key"],
            region_name           = creds["region"],
        )

    # ── CloudAdapter interface ─────────────────────────────────────────────────

    def is_authenticated(self) -> bool:
        """Return True if credentials are configured and the bucket is accessible."""
        if not self.is_configured():
            return False
        try:
            creds = self._load_credentials()
            client = self._get_client()
            client.head_bucket(Bucket=creds["bucket"])
            return True
        except Exception as e:
            logger.debug(f"S3 auth check failed: {e}")
            return False

    def get_auth_url(self, redirect_uri: str) -> str:
        """
        S3 uses static API keys rather than OAuth.

        This method is a no-op stub that satisfies the CloudAdapter interface.
        Configure credentials via Settings (s3_access_key / s3_secret_key).
        """
        return ""

    def complete_auth(self, code: str, redirect_uri: str) -> bool:
        """
        S3 does not use OAuth flows. Credentials are set directly in Settings.
        This method always returns True as a no-op.
        """
        return True

    def list_files(
        self,
        folder_id: str = None,
        query: str = "",
    ) -> list[CloudFile]:
        """
        List S3 objects matching the given prefix, filtered to importable types.

        Args:
            folder_id: S3 key prefix to filter by (e.g. "tax-docs/2024/").
                       If None or empty, lists all objects in the bucket.
            query:     Additional substring filter applied to object keys.

        Returns:
            List of CloudFile objects.
        """
        if not self.is_configured():
            logger.warning("S3: credentials not configured")
            return []

        creds = self._load_credentials()
        client = self._get_client()
        bucket = creds["bucket"]
        prefix = folder_id or ""

        files: list[CloudFile] = []
        paginator = client.get_paginator("list_objects_v2")

        try:
            kwargs: dict = {"Bucket": bucket}
            if prefix:
                kwargs["Prefix"] = prefix

            page_iter = paginator.paginate(**kwargs)

            for page in page_iter:
                for obj in page.get("Contents", []):
                    key      = obj.get("Key", "")
                    filename = os.path.basename(key)
                    ext      = os.path.splitext(filename)[1].lower()

                    if ext not in _IMPORTABLE_EXTENSIONS:
                        continue

                    if query and query.lower() not in key.lower():
                        continue

                    last_modified = obj.get("LastModified")
                    modified_str = last_modified.isoformat() if last_modified else ""

                    files.append(CloudFile(
                        file_id   = key,          # S3 key serves as the ID
                        name      = filename,
                        size      = obj.get("Size", 0),
                        mime_type = _ext_to_mime(ext),
                        modified  = modified_str,
                        path      = key,
                    ))

        except Exception as e:
            logger.error(f"S3 list_files failed (bucket={bucket}, prefix='{prefix}'): {e}")

        logger.info(f"S3: found {len(files)} files in bucket '{bucket}' prefix '{prefix}'")
        return files

    def download_file(self, key: str) -> tuple[bytes, str]:
        """
        Download an S3 object by its key.

        Args:
            key: Full S3 object key (e.g. "tax-docs/2024/receipt.pdf").

        Returns:
            (file_bytes, filename)
        """
        if not self.is_configured():
            raise RuntimeError("S3 credentials not configured.")

        creds = self._load_credentials()
        client = self._get_client()
        bucket = creds["bucket"]
        filename = os.path.basename(key) or key

        try:
            response = client.get_object(Bucket=bucket, Key=key)
            content = response["Body"].read()
        except Exception as e:
            logger.error(f"S3 download_file failed (key={key}): {e}")
            raise

        logger.debug(f"S3: downloaded {len(content)} bytes from s3://{bucket}/{key}")
        return content, filename

    # ── convenience method (not part of CloudAdapter ABC) ────────────────────

    def list_prefixes(self, parent_prefix: str = "") -> list[str]:
        """
        List common prefixes (sub-folder equivalents) under parent_prefix.

        Useful for navigating a bucket that uses folder-like key structure.

        Returns:
            List of prefix strings.
        """
        if not self.is_configured():
            return []

        creds = self._load_credentials()
        client = self._get_client()
        bucket = creds["bucket"]

        prefixes = []
        try:
            resp = client.list_objects_v2(
                Bucket=bucket,
                Prefix=parent_prefix,
                Delimiter="/",
            )
            for cp in resp.get("CommonPrefixes", []):
                prefixes.append(cp.get("Prefix", ""))
        except Exception as e:
            logger.error(f"S3 list_prefixes failed: {e}")

        return prefixes
