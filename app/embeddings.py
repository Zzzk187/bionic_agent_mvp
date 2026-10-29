"""Local multilingual text embeddings backed by FastEmbed/ONNX (no API key)."""
from __future__ import annotations
import hashlib
import os
import threading

DEFAULT_MODEL = os.getenv(
    "BIONIC_EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)

# multilingual-e5* 系模型训练时带任务前缀，其他（如 paraphrase-*）不需要；
# 加错前缀会明显掉点，所以按模型名判断。
PREFIXED_MODELS = ("multilingual-e5",)


def _needs_prefix(model_name: str) -> bool:
    return any(token in model_name for token in PREFIXED_MODELS)


class EmbeddingUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Model instance management.
#
# The FastEmbed model is ~100MB and downloads from the network on first use.
# Two problems must be avoided:
#   1. A first semantic search blocking on a download and tripping the MCP
#      client timeout (the only real defect seen in acceptance: the first
#      recall_memories call timed out at -32001 while the model downloaded).
#   2. Concurrent calls each constructing / downloading the model.
#
# Fix: serialize construction with a lock (one download, cached instance) and
# expose a background warmup + readiness probe so the server can preload the
# model at startup and the recall path can degrade to keyword instantly instead
# of blocking while the model is still loading.
# ---------------------------------------------------------------------------
_model_lock = threading.Lock()
_model_cache: dict[str, object] = {}
_model_state: dict = {"status": "idle"}   # idle | pending | ready | failed
_warmup_started = False
_warmup_lock = threading.Lock()


def _build(model_name: str):
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise EmbeddingUnavailable(
            "缺少 fastembed 依赖。请在项目目录执行：python -m pip install -r requirements.txt"
        ) from exc
    try:
        return TextEmbedding(model_name=model_name)
    except Exception as exc:
        raise EmbeddingUnavailable(
            f"Embedding 模型初始化失败：{exc}. 首次运行需要联网下载模型；请检查网络/代理后重试。"
        ) from exc


def _model(model_name: str = DEFAULT_MODEL):
    """Return a cached model instance, constructing (and downloading) at most once."""
    inst = _model_cache.get(model_name)
    if inst is not None:
        _model_state["status"] = "ready"
        return inst
    with _model_lock:
        inst = _model_cache.get(model_name)
        if inst is not None:
            _model_state["status"] = "ready"
            return inst
        inst = _build(model_name)
        _model_cache[model_name] = inst
        _model_state["status"] = "ready"
        _model_state.pop("error", None)
        return inst


def _warmup_worker(model_name: str) -> None:
    try:
        _model(model_name)
        _model_state["status"] = "ready"
        _model_state.pop("error", None)
    except Exception as exc:  # download / load failed
        _model_state["status"] = "failed"
        _model_state["error"] = str(exc)[:500]


def ensure_warmup(model_name: str = DEFAULT_MODEL) -> None:
    """Idempotently start a background download/load of the embedding model so the
    first semantic search does not block on model fetch. Safe to call repeatedly
    and from any request path.

    A previously *failed* warmup (e.g. the first-use network download failed) is
    retried: a later call (or the next request) re-launches the worker instead of
    being silently swallowed by the one-shot `_warmup_started` flag. The only
    states that suppress a relaunch are `pending` (download already in flight) and
    `ready` (model cached)."""
    global _warmup_started
    with _warmup_lock:
        status = _model_state.get("status")
        if status in ("pending", "ready"):
            return
        if _warmup_started and status != "failed":
            return
        _warmup_started = True
        _model_state["status"] = "pending"
        _model_state.pop("error", None)
    threading.Thread(target=_warmup_worker, args=(model_name,), daemon=True).start()


def model_state() -> dict:
    """Snapshot of the warmup state: status + optional error."""
    return dict(_model_state)


def is_ready(model_name: str = DEFAULT_MODEL) -> bool:
    """True once the model is downloaded/loaded and semantic search will not block."""
    return _model_state.get("status") == "ready"

def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def embed_passages(texts: list[str], model_name: str = DEFAULT_MODEL) -> list[list[float]]:
    if not texts:
        return []
    model = _model(model_name)
    try:
        # multilingual-e5 models are trained with these task prefixes.
        prefix = "passage: " if _needs_prefix(model_name) else ""
        return [list(map(float, vector)) for vector in model.embed([prefix + t for t in texts])]
    except Exception as exc:
        raise EmbeddingUnavailable(f"文本向量生成失败：{exc}") from exc

def embed_query(query: str, model_name: str = DEFAULT_MODEL) -> list[float]:
    model = _model(model_name)
    try:
        prefix = "query: " if _needs_prefix(model_name) else ""
        return list(map(float, next(iter(model.query_embed(prefix + query)))))
    except Exception as exc:
        raise EmbeddingUnavailable(f"查询向量生成失败：{exc}") from exc

def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x*y for x, y in zip(a,b))
    na = sum(x*x for x in a) ** 0.5
    nb = sum(y*y for y in b) ** 0.5
    return dot / (na*nb) if na and nb else 0.0
