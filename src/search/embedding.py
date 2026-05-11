"""语义搜索 — 本地 embedding 模型 + 余弦相似度"""

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

MODEL_NAME = "all-MiniLM-L6-v2"
SIMILARITY_THRESHOLD = 0.25

_model: Optional["SentenceTransformer"] = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model: %s...", MODEL_NAME)
        _model = SentenceTransformer(MODEL_NAME, local_files_only=True)
        logger.info("Embedding model loaded.")
    return _model


def encode(texts: list[str]) -> np.ndarray:
    """将文本列表转为归一化 embedding 矩阵 (n, 384)"""
    model = _get_model()
    embeddings = model.encode(texts, normalize_embeddings=True)
    return np.asarray(embeddings)


def search(query: str, candidates: list[dict],
           top_k: int = 20) -> list[dict]:
    """语义搜索。

    Args:
        query: 用户查询文本
        candidates: [{"tweet_id", "author_username", "text", "summary_zh",
                       "tweet_created_at", "has_link", "link_url"}, ...]
        top_k: 返回条数

    Returns:
        按相似度降序排列的结果列表，每个结果含 score 字段
    """
    if not candidates:
        return []

    search_texts = []
    for c in candidates:
        parts = []
        if c.get("text"):
            parts.append(c["text"][:500])
        if c.get("summary_zh"):
            parts.append(c["summary_zh"])
        search_texts.append(" ".join(parts))

    try:
        query_emb = encode([query])[0]
        doc_embs = encode(search_texts)
    except Exception as exc:
        logger.exception("Embedding encode failed")
        return []

    scores = np.dot(doc_embs, query_emb)

    top_indices = np.argsort(scores)[::-1][:top_k]

    results = []
    for idx in top_indices:
        s = float(scores[idx])
        if s < SIMILARITY_THRESHOLD:
            continue
        c = candidates[idx].copy()
        c["score"] = round(s, 3)
        results.append(c)
    return results
