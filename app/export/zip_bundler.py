"""Bundle all export files + source PDFs into a ZIP."""
import os
import zipfile
from app.config import EXPORT_PATH


def create_bundle(year: str, entity_slug: str, export_files: list[str],
                  media_path: str = "/paperless/media") -> str:
    """Create a ZIP bundle of all export files plus source PDFs from paperless media.

    Args:
        year: Tax year string (e.g. "2024")
        entity_slug: Entity slug (e.g. "personal")
        export_files: List of file paths to include in the ZIP
        media_path: Path to paperless media directory (mounted volume)

    Returns:
        Absolute path to the created ZIP file.
    """
    zip_name = f"tax_{year}_{entity_slug}_complete.zip"
    dest_dir = os.path.join(EXPORT_PATH, year)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, zip_name)

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add all export files
        for fpath in export_files:
            if fpath and os.path.exists(fpath):
                zf.write(fpath, os.path.basename(fpath))

        # Add source PDFs from paperless media archive if accessible
        # Paperless stores archived docs under media/documents/archive/YYYY/
        archive_dir = os.path.join(media_path, "documents", "archive", year)
        if os.path.isdir(archive_dir):
            for root, dirs, files in os.walk(archive_dir):
                for fname in files:
                    if fname.lower().endswith(".pdf"):
                        full = os.path.join(root, fname)
                        arcname = os.path.join("source_pdfs", os.path.relpath(full, archive_dir))
                        zf.write(full, arcname)

    return dest
