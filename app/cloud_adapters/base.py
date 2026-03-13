"""Abstract base class for cloud storage adapters."""
from abc import ABC, abstractmethod
from typing import Optional


class CloudFile:
    def __init__(self, file_id: str, name: str, size: int = 0,
                 mime_type: str = "", modified: str = "", path: str = ""):
        self.file_id = file_id
        self.name = name
        self.size = size
        self.mime_type = mime_type
        self.modified = modified
        self.path = path

    def to_dict(self) -> dict:
        return {
            "id": self.file_id, "name": self.name, "size": self.size,
            "mime_type": self.mime_type, "modified": self.modified, "path": self.path
        }


class CloudAdapter(ABC):
    """Base class for cloud storage integrations."""

    @abstractmethod
    def is_authenticated(self) -> bool:
        pass

    @abstractmethod
    def get_auth_url(self, redirect_uri: str) -> str:
        pass

    @abstractmethod
    def complete_auth(self, code: str, redirect_uri: str) -> bool:
        pass

    @abstractmethod
    def list_files(self, folder_id: str = None, query: str = "") -> list[CloudFile]:
        pass

    @abstractmethod
    def download_file(self, file_id: str) -> tuple[bytes, str]:
        """Return (file_bytes, filename)."""
        pass
