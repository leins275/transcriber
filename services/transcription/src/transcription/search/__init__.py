"""Hybrid search over the meetings vault.

``index_db.py`` owns the rebuildable SQLite index (FTS5 + sqlite-vec);
``indexer.py`` walks the vault into it; ``hybrid.py`` fuses the retrieval
channels (pure logic); ``service.py`` answers queries. Nothing in this
package imports an ML library -- embedding goes through the
``transcription.llm`` seam.
"""
