"""
ChromaDB-backed vector store for Financial AI Analyzer.

Uses sentence-transformers for local embedding (no external API dependency).
Falls back gracefully if chromadb or sentence-transformers are not installed.

Persistent storage at /app/data/chroma/.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_CHROMA_DIR = os.path.join(os.environ.get("DATA_DIR", "/app/data"), "chroma")

# ── Lazy imports — fail gracefully if packages not present ───────────────────

_chromadb = None
_SentenceTransformer = None
_CHROMA_AVAILABLE = False
_EMBED_AVAILABLE = False


def _ensure_imports() -> bool:
    """Try to import chromadb and sentence-transformers. Return True if both available."""
    global _chromadb, _SentenceTransformer, _CHROMA_AVAILABLE, _EMBED_AVAILABLE

    if not _CHROMA_AVAILABLE:
        try:
            import chromadb as _cb
            _chromadb = _cb
            _CHROMA_AVAILABLE = True
        except ImportError:
            logger.warning(
                "chromadb not installed — vector search disabled. "
                "Install with: pip install chromadb"
            )
            return False

    if not _EMBED_AVAILABLE:
        try:
            from sentence_transformers import SentenceTransformer as _ST
            _SentenceTransformer = _ST
            _EMBED_AVAILABLE = True
        except ImportError:
            logger.warning(
                "sentence-transformers not installed — vector search disabled. "
                "Install with: pip install sentence-transformers"
            )
            return False

    return True


# ── Embedding model cache ─────────────────────────────────────────────────────

_embed_model = None
_EMBED_MODEL_NAME = os.environ.get(
    "EMBEDDING_MODEL", "all-MiniLM-L6-v2"
)  # Small, fast, good quality


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        logger.info(f"Loading sentence-transformer model: {_EMBED_MODEL_NAME}")
        _embed_model = _SentenceTransformer(_EMBED_MODEL_NAME)
        logger.info("Embedding model loaded")
    return _embed_model


def _embed(text: str) -> list:
    """Generate embedding vector for text. Returns list of floats."""
    model = _get_embed_model()
    vec = model.encode(text, convert_to_numpy=True)
    return vec.tolist()


# ── ChromaDB client cache ─────────────────────────────────────────────────────

_chroma_client = None


def _get_chroma_client():
    global _chroma_client
    if _chroma_client is None:
        os.makedirs(_CHROMA_DIR, exist_ok=True)
        _chroma_client = _chromadb.PersistentClient(path=_CHROMA_DIR)
    return _chroma_client


# ── VectorStore class ─────────────────────────────────────────────────────────

class VectorStore:
    """
    Document vector store backed by ChromaDB with sentence-transformer embeddings.

    Each collection holds documents for a specific scope (e.g. "financial_docs").
    Metadata filtering is supported on entity_slug and tax_year.

    If chromadb or sentence-transformers are not installed, all operations
    degrade gracefully: writes are no-ops and searches return empty lists.
    """

    def __init__(self, collection_name: str = "financial_docs"):
        self.collection_name = collection_name
        self._collection = None

    def _get_collection(self):
        """Return (or lazily create) the ChromaDB collection."""
        if self._collection is None:
            if not _ensure_imports():
                return None
            client = _get_chroma_client()
            self._collection = client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    # ── Write operations ──────────────────────────────────────────────────────

    def embed_document(
        self,
        doc_id: int,
        title: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """
        Embed and store a document in the vector store.

        Args:
            doc_id: Paperless document ID (used as the vector store document ID)
            title: Document title
            content: Full text content to embed
            metadata: Optional dict of filterable metadata. Supported keys:
                entity_slug, tax_year, doc_type, category, vendor, amount, date

        Returns True on success, False if vector store is unavailable.
        """
        coll = self._get_collection()
        if coll is None:
            return False

        # Build text to embed: title + first 2000 chars of content
        text_to_embed = f"{title}\n\n{content[:2000]}" if content else title
        if not text_to_embed.strip():
            logger.debug(f"Skipping embed for doc {doc_id} — no text")
            return False

        try:
            vec = _embed(text_to_embed)
        except Exception as e:
            logger.error(f"Embedding failed for doc {doc_id}: {e}")
            return False

        # Build metadata (ChromaDB requires str/int/float/bool values)
        meta = {
            "doc_id": doc_id,
            "title": title or "",
            "entity_slug": "",
            "tax_year": "",
            "doc_type": "",
            "category": "",
            "vendor": "",
        }
        if metadata:
            for k, v in metadata.items():
                if v is not None:
                    meta[k] = str(v) if not isinstance(v, (int, float, bool)) else v

        try:
            coll.upsert(
                ids=[str(doc_id)],
                embeddings=[vec],
                documents=[text_to_embed[:10000]],
                metadatas=[meta],
            )
            logger.debug(f"Embedded document {doc_id} into collection '{self.collection_name}'")
            return True
        except Exception as e:
            logger.error(f"ChromaDB upsert failed for doc {doc_id}: {e}")
            return False

    def search(
        self,
        query: str,
        entity_slug: Optional[str] = None,
        tax_year: Optional[str] = None,
        doc_type: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 10,
    ) -> list:
        """
        Semantic search over stored documents.

        Args:
            query: Natural language search query
            entity_slug: Filter to a specific entity slug
            tax_year: Filter to a specific tax year
            doc_type: Filter to a specific document type
            category: Filter to income/expense/deduction/asset/other
            limit: Maximum number of results to return

        Returns list of dicts, each with:
          doc_id, title, entity_slug, tax_year, doc_type, category, vendor,
          distance (lower = more similar)
        """
        coll = self._get_collection()
        if coll is None:
            return []

        if not query.strip():
            return []

        try:
            vec = _embed(query)
        except Exception as e:
            logger.error(f"Embedding failed for query: {e}")
            return []

        # Build ChromaDB where clause
        where: Optional[dict] = None
        conditions = []
        if entity_slug:
            conditions.append({"entity_slug": {"$eq": entity_slug}})
        if tax_year:
            conditions.append({"tax_year": {"$eq": str(tax_year)}})
        if doc_type:
            conditions.append({"doc_type": {"$eq": doc_type}})
        if category:
            conditions.append({"category": {"$eq": category}})

        if len(conditions) == 1:
            where = conditions[0]
        elif len(conditions) > 1:
            where = {"$and": conditions}

        try:
            kwargs = {
                "query_embeddings": [vec],
                "n_results": min(limit, max(1, coll.count())),
                "include": ["metadatas", "distances", "documents"],
            }
            if where:
                kwargs["where"] = where

            results = coll.query(**kwargs)
        except Exception as e:
            logger.error(f"ChromaDB query failed: {e}")
            return []

        # Flatten results
        output = []
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        documents = results.get("documents", [[]])[0]

        for meta, dist, doc_text in zip(metadatas, distances, documents):
            output.append({
                "doc_id": meta.get("doc_id"),
                "title": meta.get("title", ""),
                "entity_slug": meta.get("entity_slug", ""),
                "tax_year": meta.get("tax_year", ""),
                "doc_type": meta.get("doc_type", ""),
                "category": meta.get("category", ""),
                "vendor": meta.get("vendor", ""),
                "distance": round(dist, 4),
                "snippet": doc_text[:200] if doc_text else "",
            })

        return output

    def delete_document(self, doc_id: int) -> bool:
        """
        Remove a document from the vector store.

        Returns True on success, False if not found or store unavailable.
        """
        coll = self._get_collection()
        if coll is None:
            return False
        try:
            coll.delete(ids=[str(doc_id)])
            logger.debug(f"Deleted doc {doc_id} from vector store")
            return True
        except Exception as e:
            logger.warning(f"Vector store delete failed for doc {doc_id}: {e}")
            return False

    def get_stats(self) -> dict:
        """
        Return vector store statistics.

        Returns dict with:
          available (bool), collection_name, total_documents,
          by_entity (dict), by_year (dict), by_category (dict)
        """
        coll = self._get_collection()
        if coll is None:
            return {
                "available": False,
                "collection_name": self.collection_name,
                "total_documents": 0,
                "by_entity": {},
                "by_year": {},
                "by_category": {},
                "error": "chromadb or sentence-transformers not installed",
            }

        try:
            total = coll.count()
            stats = {
                "available": True,
                "collection_name": self.collection_name,
                "total_documents": total,
                "by_entity": {},
                "by_year": {},
                "by_category": {},
            }

            if total == 0:
                return stats

            # Fetch all metadata for aggregation
            # ChromaDB doesn't support GROUP BY, so we do it in Python
            results = coll.get(include=["metadatas"])
            for meta in (results.get("metadatas") or []):
                e = meta.get("entity_slug") or "unknown"
                y = meta.get("tax_year") or "unknown"
                c = meta.get("category") or "unknown"
                stats["by_entity"][e] = stats["by_entity"].get(e, 0) + 1
                stats["by_year"][y] = stats["by_year"].get(y, 0) + 1
                stats["by_category"][c] = stats["by_category"].get(c, 0) + 1

            return stats
        except Exception as e:
            logger.error(f"Vector store stats failed: {e}")
            return {
                "available": False,
                "collection_name": self.collection_name,
                "total_documents": 0,
                "by_entity": {},
                "by_year": {},
                "by_category": {},
                "error": str(e),
            }

    def is_available(self) -> bool:
        """Return True if the vector store backend is functional."""
        return _ensure_imports() and self._get_collection() is not None

    def reindex_document(
        self,
        doc_id: int,
        title: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Delete and re-embed a document (useful after content/metadata updates)."""
        self.delete_document(doc_id)
        return self.embed_document(doc_id, title, content, metadata)


# ── Module-level default instance ────────────────────────────────────────────

_default_store: Optional[VectorStore] = None


def get_store(collection_name: str = "financial_docs") -> VectorStore:
    """Return the default module-level VectorStore instance."""
    global _default_store
    if _default_store is None or _default_store.collection_name != collection_name:
        _default_store = VectorStore(collection_name)
    return _default_store


# ── Convenience top-level functions ──────────────────────────────────────────

def embed_document(
    doc_id: int,
    title: str,
    content: str,
    metadata: Optional[dict] = None,
) -> bool:
    return get_store().embed_document(doc_id, title, content, metadata)


def search(
    query: str,
    entity_slug: Optional[str] = None,
    tax_year: Optional[str] = None,
    limit: int = 10,
) -> list:
    return get_store().search(query, entity_slug=entity_slug, tax_year=tax_year, limit=limit)


def delete_document(doc_id: int) -> bool:
    return get_store().delete_document(doc_id)


def get_stats() -> dict:
    return get_store().get_stats()
