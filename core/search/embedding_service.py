"""嵌入向量服务：封装 AstrBot EmbeddingProvider + FaissVecDB / numpy 降级。

参考 astrbot_plugin_livingmemory 的实现模式：
- Provider 获取：context.get_all_embedding_providers() / provider_manager.inst_map
- 向量存储：优先 FaissVecDB，不可用时降级 SQLite + numpy
- 维度校验：provider 切换时自动清理旧索引
- 回填：启动时为已有 emoji 批量补算向量
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from astrbot.api import logger

from ..processing.semantic_schema import EMBEDDING_TEXT_VERSION, build_meme_search_text


class EmbeddingService:
    """嵌入向量服务。

    两层架构（对齐 livingmemory 的 FaissVecDB 模式）：
    1. 优先 FaissVecDB（astrbot.core.db.vec_db.faiss_impl.vec_db）
    2. 降级 SQLite emoji_embedding 表 + numpy 内存矩阵
    """

    # 回填参数
    BACKFILL_BATCH_SIZE = 20

    # 单个 path 一次最多清理多少条重复向量文档（防御性上限，正常只有 1 条）
    MAX_DUPLICATE_DOCS = 50

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._provider: Any | None = None
        self._provider_dim: int = 0

        # Provider 负缓存：同一配置下未找到时不再重复探测/打印日志
        self._provider_not_found: bool = False
        self._last_enable_embedding_search: bool | None = None
        self._last_embedding_provider_id: str | None = None

        # FaissVecDB
        self._faiss_db: Any | None = None
        self._faiss_available: bool | None = None  # None=未检测

        # numpy 降级
        self._fallback_matrix: np.ndarray | None = None
        self._fallback_paths: list[str] = []
        self._fallback_dim: int = 0
        self._fallback_loaded: bool = False

    # ═══════════════════════════════════════════════════
    #  Provider（对齐 livingmemory _initialize_providers）
    # ═══════════════════════════════════════════════════

    def _embedding_enabled(self) -> bool:
        """读取插件配置中的嵌入检索开关。"""
        return getattr(self.plugin, "enable_embedding_search", True)

    def _reset_provider_state_if_changed(self) -> None:
        """配置开关或 provider ID 变化时重置 provider 探测状态。"""
        current_enable = self._embedding_enabled()
        current_id = str(getattr(self.plugin, "embedding_provider_id", "") or "").strip()
        if (
            self._last_enable_embedding_search != current_enable
            or self._last_embedding_provider_id != current_id
        ):
            self._provider = None
            self._provider_dim = 0
            self._provider_not_found = False
            self._faiss_available = None
            self._faiss_db = None
            self.invalidate_cache()
            self._last_enable_embedding_search = current_enable
            self._last_embedding_provider_id = current_id

    def _get_provider(self) -> Any | None:
        """获取 EmbeddingProvider（优先配置 ID，留空取首个）。"""
        # 开关关闭时直接跳过，不触发 provider 探测与日志
        if not self._embedding_enabled():
            return None

        self._reset_provider_state_if_changed()

        if self._provider is not None:
            return self._provider

        # 负缓存：同一配置下已确认无 provider，直接返回避免重复日志
        if self._provider_not_found:
            return None

        provider_id = getattr(self.plugin, "embedding_provider_id", None) or ""

        # 1. 按 ID 查找（对齐 livingmemory _initialize_providers）
        if provider_id:
            provider = self._find_provider_by_id(provider_id)
            if provider is not None:
                # 类型校验：get_provider_by_id 返回的可能是 chat/stt/tts/embedding
                if not self._is_embedding_provider(provider):
                    logger.warning(
                        f"[Embedding] Provider '{provider_id}' 不是 EmbeddingProvider 类型，已忽略"
                    )
                else:
                    self._provider = provider
                    self._provider_dim = self._get_provider_dim(provider)
                    logger.info(
                        f"[Embedding] 使用指定 Provider: {provider_id} (dim={self._provider_dim})"
                    )
                    return self._provider

        # 2. 取框架首个 Embedding Provider
        try:
            providers = self.plugin.context.get_all_embedding_providers()
        except Exception:
            providers = []

        if providers:
            self._provider = providers[0]
            self._provider_dim = self._get_provider_dim(self._provider)
            pid = self._extract_provider_id(self._provider)
            logger.info(f"[Embedding] 自动选择首个 Provider: {pid} (dim={self._provider_dim})")
            return self._provider

        logger.info("[Embedding] 未找到 Embedding Provider，嵌入检索不可用")
        self._provider_not_found = True
        return None

    def _find_provider_by_id(self, provider_id: str) -> Any | None:
        """按 ID 查找 provider（对齐 livingmemory 的静默查找模式）。"""
        # 静默查找：直接读 provider_manager.inst_map，避免 AstrBot 的 warning 日志
        try:
            pm = getattr(self.plugin.context, "provider_manager", None)
            inst_map = getattr(pm, "inst_map", None)
            if isinstance(inst_map, dict):
                p = inst_map.get(provider_id)
                if p is not None:
                    return p
        except Exception:
            pass

        # 回退到公开 API
        try:
            return self.plugin.context.get_provider_by_id(provider_id)
        except Exception:
            pass

        return None

    @staticmethod
    def _is_embedding_provider(provider: Any) -> bool:
        """校验 provider 是否为 EmbeddingProvider 类型（对齐 livingmemory isinstance 检查）。"""
        try:
            from astrbot.core.provider.provider import EmbeddingProvider
            return isinstance(provider, EmbeddingProvider)
        except ImportError:
            # 无法导入时退化为鸭子类型检查：有 get_embedding 方法即可
            return hasattr(provider, "get_embedding")

    @staticmethod
    def _extract_provider_id(provider: Any) -> str:
        """从 provider 实例提取 ID 字符串。"""
        cfg = getattr(provider, "provider_config", {})
        if isinstance(cfg, dict):
            return cfg.get("id", "unknown")
        return getattr(cfg, "id", "unknown")

    @staticmethod
    def _get_provider_dim(provider: Any) -> int:
        """获取 provider 的输出维度。"""
        if hasattr(provider, "get_dim"):
            try:
                return provider.get_dim()
            except Exception:
                pass
        return 0

    # ═══════════════════════════════════════════════════
    #  可用性
    # ═══════════════════════════════════════════════════

    def is_available(self) -> bool:
        """嵌入检索是否可用。"""
        if not getattr(self.plugin, "enable_embedding_search", True):
            return False

        if self._init_faiss():
            return True
        if self._init_fallback():
            return True
        return False

    # ═══════════════════════════════════════════════════
    #  FaissVecDB（对齐 livingmemory _complete_initialization）
    # ═══════════════════════════════════════════════════

    def _init_faiss(self) -> bool:
        """初始化 FaissVecDB。"""
        if self._faiss_available is not None:
            return self._faiss_available

        provider = self._get_provider()
        if provider is None:
            self._faiss_available = False
            return False

        try:
            import faiss  # noqa: F401
            from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
        except ImportError:
            logger.info("[Embedding] faiss 未安装，降级 numpy")
            self._faiss_available = False
            return False

        try:
            data_dir = self._resolve_data_dir()
            db_path = f"{data_dir}/emoji_faiss.db"
            index_path = f"{data_dir}/emoji_faiss.index"

            self._faiss_db = FaissVecDB(db_path, index_path, provider)
            self._faiss_available = True
            logger.info(f"[Embedding] FaissVecDB 就绪 (dim={self._provider_dim})")
            return True
        except Exception as e:
            logger.warning(f"[Embedding] FaissVecDB 初始化失败: {e}")
            self._faiss_available = False
            return False

    def _resolve_data_dir(self) -> str:
        """解析插件数据目录。"""
        if hasattr(self.plugin, "plugin_config"):
            return str(self.plugin.plugin_config.data_dir)
        if hasattr(self.plugin, "data_dir"):
            return str(self.plugin.data_dir)
        return "."

    # ═══════════════════════════════════════════════════
    #  numpy 降级（SQLite emoji_embedding 表）
    # ═══════════════════════════════════════════════════

    def _init_fallback(self) -> bool:
        """初始化 numpy 降级路径。"""
        provider = self._get_provider()
        if provider is None:
            return False
        self._load_fallback_matrix()
        return True  # provider 可用即就绪

    def _load_fallback_matrix(self) -> None:
        """从 SQLite emoji_embedding 表加载向量到内存矩阵。"""
        if self._fallback_loaded:
            return

        self._fallback_matrix = None
        self._fallback_paths = []
        self._fallback_dim = 0

        db = getattr(self.plugin, "db_service", None)
        if not db or not hasattr(db, "load_embeddings_by_sig"):
            self._fallback_loaded = True
            return

        rows = None
        for sig in ("fallback",):
            try:
                rows = db.load_embeddings_by_sig(sig)
                if rows:
                    break
            except Exception:
                continue

        if not rows:
            self._fallback_loaded = True
            return

        # 构建 numpy 矩阵（对齐 livingmemory 的向量加载模式）
        vectors = []
        paths = []
        dim = 0
        for r in rows:
            blob = r.get("vector")
            path = r.get("path")
            if not blob or not path:
                continue
            try:
                vec = np.frombuffer(bytes(blob), dtype=np.float32)
                d = int(r.get("dim", 0))
                if d > 0 and len(vec) == d:
                    vectors.append(vec)
                    paths.append(str(path))
                    dim = d
            except Exception:
                continue

        if vectors:
            mat = np.stack(vectors, axis=0)
            # L2 归一化（对齐 livingmemory 的余弦相似度计算）
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1.0, norms)
            self._fallback_matrix = mat / norms
            self._fallback_paths = paths
            self._fallback_dim = dim
            logger.info(f"[Embedding] numpy 降级矩阵已加载: {len(paths)} 条 (dim={dim})")
        else:
            logger.debug("[Embedding] numpy 降级矩阵为空（无有效向量）")

        self._fallback_loaded = True

    def _check_fallback_dimension(self) -> bool:
        """对齐 livingmemory _check_and_fix_dimension_mismatch：
        检查降级矩阵维度是否与当前 provider 匹配，不匹配则清空。
        """
        if self._fallback_dim == 0 or self._provider_dim == 0:
            return True  # 无法判断，继续使用
        if self._fallback_dim != self._provider_dim:
            logger.warning(
                f"[Embedding] 维度不匹配: 已存={self._fallback_dim}, "
                f"provider={self._provider_dim}。旧向量将被清除并重建。"
            )
            # 清空 SQLite 向量表
            db = getattr(self.plugin, "db_service", None)
            if db and hasattr(db, "_get_connection"):
                try:
                    with db._get_connection() as conn:
                        conn.execute("DELETE FROM emoji_embedding")
                    logger.info("[Embedding] 已清除旧向量，将在回填中重建")
                except Exception as e:
                    logger.warning(f"[Embedding] 清除旧向量失败: {e}")
            self._fallback_loaded = False
            self._fallback_matrix = None
            self._fallback_paths = []
            self._fallback_dim = 0
            return False
        return True

    # ═══════════════════════════════════════════════════
    #  初始化
    # ═══════════════════════════════════════════════════

    async def initialize(self) -> None:
        """异步初始化（对齐 livingmemory 的 initialize 流程）。"""
        # 开关关闭时完全跳过，不探测 provider、不输出日志
        if not self._embedding_enabled():
            return

        await self._upgrade_corpus_if_needed()

        # 1. Provider 就绪检查
        provider = self._get_provider()
        if provider is None:
            logger.info(
                "[Embedding] 未就绪 — 请在 AstrBot 后台配置 Embedding 模型"
            )
            return

        # 2. 尝试 FaissVecDB
        if self._init_faiss() and self._faiss_db is not None:
            try:
                await self._faiss_db.initialize()
                logger.info("[Embedding] FaissVecDB 初始化完成 ✅")
            except Exception as e:
                logger.warning(f"[Embedding] FaissVecDB 初始化失败，降级: {e}")
                self._faiss_available = False

        # 3. 降级 numpy
        if not self._faiss_available or self._faiss_available is None:
            if self._init_fallback():
                self._check_fallback_dimension()
                count = len(self._fallback_paths)
                logger.info(
                    f"[Embedding] numpy 降级方案就绪 ✅ ({count} 条向量, dim={self._fallback_dim})"
                    if count > 0 else
                    "[Embedding] numpy 降级方案就绪 ✅ — 向量库为空，新入库自动填充"
                )

    # ═══════════════════════════════════════════════════
    #  插入 / 删除
    # ═══════════════════════════════════════════════════

    def _category_info(self) -> dict[str, Any]:
        cfg = getattr(self.plugin, "plugin_config", None)
        info = getattr(cfg, "category_info", None) if cfg else None
        return info if isinstance(info, dict) else {}

    def _character_info(self) -> dict[str, Any]:
        cfg = getattr(self.plugin, "plugin_config", None)
        info = getattr(cfg, "character_info", None) if cfg else None
        return info if isinstance(info, dict) else {}

    def _build_search_text(self, entry: dict[str, Any]) -> str:
        """拼接嵌入文本：图上文字 + 角色 + 使用句 + 描述 + 多情绪。"""
        return build_meme_search_text(
            entry,
            category_info=self._category_info(),
            character_info=self._character_info(),
        )

    # ── 向量新鲜度指纹 ──

    @staticmethod
    def _text_fingerprint(text: str) -> str:
        """嵌入文本指纹：文本变了指纹就变，用来发现「内容改过但向量还是旧的」。"""
        payload = f"{EMBEDDING_TEXT_VERSION}\x00{text}".encode()
        return hashlib.sha256(payload).hexdigest()[:32]

    def _record_text_hash(self, path: str, text: str) -> None:
        """向量写入成功后记录指纹；失败只降级为 debug，不影响向量本身。"""
        db = getattr(self.plugin, "db_service", None)
        if not db or not hasattr(db, "set_embedding_hash") or not path or not text:
            return
        try:
            db.set_embedding_hash(path, self._text_fingerprint(text))
        except Exception as e:
            logger.debug(f"[Embedding] 记录向量指纹失败 {path}: {e}")

    def _forget_text_hash(self, path: str) -> None:
        """向量删除成功后清掉指纹，保持两者一致。"""
        db = getattr(self.plugin, "db_service", None)
        if not db or not hasattr(db, "delete_embedding_hash") or not path:
            return
        try:
            db.delete_embedding_hash(path)
        except Exception as e:
            logger.debug(f"[Embedding] 清除向量指纹失败 {path}: {e}")

    async def insert_emoji(self, path: str, entry: dict[str, Any]) -> bool:
        """插入单条 emoji 向量（对齐 FaissVecDB.insert 模式）。

        失败不阻塞入库流程。
        """
        # 开关关闭时跳过写入，避免触发 provider 探测
        if not self._embedding_enabled():
            return False

        text = self._build_search_text(entry)
        if not text:
            return False

        # 截断超长文本（对齐 livingmemory _MAX_CONTENT_CHARS = 4000）
        if len(text) > 4000:
            text = text[:4000]

        # FaissVecDB 路径
        if self._init_faiss() and self._faiss_db is not None:
            try:
                await self._faiss_db.insert(
                    content=text,
                    metadata={"path": path, "category": str(entry.get("category", ""))},
                )
                self._record_text_hash(path, text)
                return True
            except Exception as e:
                logger.debug(f"[Embedding] FaissVecDB 插入失败: {e}")

        # numpy 降级
        return await self._fallback_insert(path, text)

    async def delete_by_path(self, path: str) -> bool:
        """按 path 删除向量。

        返回 True 表示「调用结束后这个 path 在向量库里确实没有残留」，
        本来就没有向量也算 True；返回 False 表示出错、无法保证删干净。
        调用方拿到 False 时不要紧接着 insert —— FaissVecDB 的 insert 没有
        upsert 语义，会在库里留下新旧两条文档，让检索一直命中旧内容。
        """
        ok = True

        if self._init_faiss() and self._faiss_db is not None:
            try:
                ds = self._faiss_db.document_storage
                # 只删「查到的第一条」会让历史重复项永远清不完，这里一次多取几条删干净
                docs = await ds.get_documents(
                    metadata_filters={"path": path}, limit=self.MAX_DUPLICATE_DOCS
                )
                docs = docs or []
                for doc in docs:
                    uuid_id = doc.get("doc_id") if isinstance(doc, dict) else None
                    if not uuid_id:
                        ok = False
                        continue
                    await self._faiss_db.delete(uuid_id)
                if len(docs) >= self.MAX_DUPLICATE_DOCS:
                    ok = False
                    logger.warning(
                        f"[Embedding] 同一路径的重复向量超过 {self.MAX_DUPLICATE_DOCS} 条，"
                        f"本轮已清理一批，可能仍有残留: {path}"
                    )
            except Exception as e:
                ok = False
                logger.warning(f"[Embedding] FaissVecDB 向量删除失败 {path}: {e}")

        db = getattr(self.plugin, "db_service", None)
        if db and hasattr(db, "delete_embedding"):
            try:
                db.delete_embedding(path)
                self._fallback_loaded = False
            except Exception as e:
                ok = False
                logger.warning(f"[Embedding] SQLite 向量删除失败 {path}: {e}")

        if ok:
            self._forget_text_hash(path)
        return ok

    async def _fallback_insert(self, path: str, text: str) -> bool:
        """降级：get_embedding → upsert_embedding。"""
        provider = self._get_provider()
        if provider is None:
            return False

        try:
            vec = await provider.get_embedding(text)
            if not vec or len(vec) == 0:
                return False
        except Exception as e:
            logger.debug(f"[Embedding] get_embedding 失败: {e}")
            return False

        db = getattr(self.plugin, "db_service", None)
        if not db or not hasattr(db, "upsert_embedding"):
            return False

        try:
            blob = np.array(vec, dtype=np.float32).tobytes()
            db.upsert_embedding(path, blob, dim=len(vec), model_sig="fallback")
            self._record_text_hash(path, text)
            self._fallback_loaded = False  # 下次 search 时重载
            return True
        except Exception as e:
            logger.debug(f"[Embedding] upsert_embedding 失败: {e}")
            return False

    # ═══════════════════════════════════════════════════
    #  检索
    # ═══════════════════════════════════════════════════

    async def search(self, query: str, k: int = 80) -> list[tuple[str, float]]:
        """向量检索 top-K（对齐 FaissVecDB.retrieve 模式）。

        Returns:
            [(path, similarity_score), ...]  按相似度降序
        """
        if not query or not query.strip():
            return []

        # 截断查询（对齐 livingmemory _MAX_QUERY_CHARS = 2000）
        processed = query[:2000] if len(query) > 2000 else query

        # FaissVecDB 路径
        if self._init_faiss() and self._faiss_db is not None:
            try:
                results = await self._faiss_db.retrieve(
                    query=processed, k=k, fetch_k=k * 2, rerank=False
                )
                out: list[tuple[str, float]] = []
                for r in results:
                    data = getattr(r, "data", None)
                    if data is None:
                        continue
                    # FaissVecDB 返回的 data 是 {"id": int, "text": str, "metadata": dict}
                    if isinstance(data, dict):
                        meta = data.get("metadata", {})
                        if isinstance(meta, dict):
                            p = meta.get("path", "")
                            if p:
                                out.append((p, float(r.similarity)))
                        elif isinstance(meta, str):
                            # metadata 被序列化成了 JSON 字符串
                            try:
                                meta_dict = json.loads(meta)
                                p = meta_dict.get("path", "")
                                if p:
                                    out.append((p, float(r.similarity)))
                            except (json.JSONDecodeError, TypeError):
                                pass
                    elif isinstance(data, str):
                        # data 本身是 JSON 字符串
                        try:
                            data_dict = json.loads(data)
                            meta = data_dict.get("metadata", {})
                            if isinstance(meta, dict):
                                p = meta.get("path", "")
                            elif isinstance(meta, str):
                                meta = json.loads(meta)
                                p = meta.get("path", "") if isinstance(meta, dict) else ""
                            if p:
                                out.append((p, float(r.similarity)))
                        except (json.JSONDecodeError, TypeError):
                            pass
                return out
            except Exception as e:
                logger.warning(f"[Embedding] FaissVecDB 检索失败: {e}")

        # numpy 降级
        return await self._fallback_search(processed, k)

    async def _fallback_search(self, query: str, k: int) -> list[tuple[str, float]]:
        """降级：numpy 余弦相似度检索。"""
        try:
            self._load_fallback_matrix()
            if self._fallback_matrix is None or len(self._fallback_paths) == 0:
                return []

            provider = self._get_provider()
            if provider is None:
                return []

            # 嵌入查询
            try:
                vec = await provider.get_embedding(query)
                if not vec:
                    return []
            except Exception:
                return []

            # 余弦相似度
            qv = np.array(vec, dtype=np.float32)
            q_norm = np.linalg.norm(qv)
            if q_norm == 0:
                return []
            qv = qv / q_norm

            # 维度安全检查：不匹配则清空旧向量（对齐 livingmemory _check_and_fix_dimension_mismatch）
            if self._fallback_matrix.shape[1] != len(qv):
                logger.warning(
                    f"[Embedding] 维度不匹配: matrix={self._fallback_matrix.shape[1]}, "
                    f"query={len(qv)}。清除旧向量，将在下次回填中重建。"
                )
                self._fallback_loaded = False
                self._fallback_matrix = None
                self._fallback_paths = []
                self._fallback_dim = 0
                return []

            scores = self._fallback_matrix @ qv
            top_idx = np.argsort(scores)[::-1][:k]

            results: list[tuple[str, float]] = []
            for idx in top_idx:
                s = float(scores[idx])
                if s < 0.15:  # 低相似度截断
                    continue
                results.append((self._fallback_paths[int(idx)], s))
            return results
        except Exception as e:
            logger.warning(f"[Embedding] numpy 搜索异常: {e}")
            return []

    def invalidate_cache(self) -> None:
        """标记缓存过期。"""
        self._fallback_loaded = False
        self._fallback_matrix = None
        self._fallback_paths = []

    # ═══════════════════════════════════════════════════
    #  回填（对齐 livingmemory 的批量重建模式）
    # ═══════════════════════════════════════════════════

    async def backfill_existing(self, batch_size: int | None = None) -> int:
        """启动时批量回填缺少向量的旧 emoji。

        对齐 livingmemory index_rebuild 的批量处理模式。
        关键：回填到当前活跃的存储后端（FaissVecDB 或 SQLite），不混用。
        """
        # 开关关闭时跳过回填，避免触发 provider 探测与重复日志
        if not self._embedding_enabled():
            return 0

        if batch_size is None:
            batch_size = self.BACKFILL_BATCH_SIZE

        db = getattr(self.plugin, "db_service", None)
        if not db:
            return 0

        # 获取所有 emoji 路径
        try:
            all_paths = db.get_all_paths()
        except Exception as e:
            logger.warning(f"[Embedding] 回填失败 — 无法获取 emoji 列表: {e}")
            return 0
        if not all_paths:
            return 0

        # 判断当前活跃的存储后端，据此检查已有向量
        using_faiss = self._init_faiss() and self._faiss_db is not None
        backend = "FaissVecDB" if using_faiss else "SQLite"

        # 读「已有向量」失败时必须中止：一旦当成「一条都没有」，下面会把整个库
        # 判成缺失并重算全部向量，白烧一轮嵌入额度，而且日志上看不出异常。
        embedded = await self._read_embedded_paths(using_faiss, db, len(all_paths))
        if embedded is None:
            logger.warning(
                f"[Embedding] 回填中止 — 读不到 {backend} 里已有的向量，本轮跳过，"
                f"避免误判成缺失后重算全部 {len(all_paths)} 条；"
                f"下次重启会自动重试，也可以手动执行 rebuild_vectors 重建"
            )
            return 0

        # 索引元数据：拼嵌入文本要用，判断向量是否过期也要用
        idx: dict[str, Any] = {}
        try:
            if db.count_total() > 0:
                idx = db.get_index_cache_readonly()
        except Exception as e:
            logger.debug(f"[Embedding] 读取索引缓存失败，改为逐条回查数据库: {e}")

        missing = [p for p in all_paths if p not in embedded]
        stale, hash_only = self._detect_stale_embeddings(all_paths, embedded, idx, db)

        # 老库升级上来时指纹表是空的：只补写指纹、不调用嵌入模型，
        # 否则所有人升级后第一次重启都会被判成「全库过期」而重算一遍。
        if hash_only:
            try:
                db.set_embedding_hashes(hash_only)
                logger.info(
                    f"[Embedding] 已为 {len(hash_only)} 条已有向量补记文本指纹（没有调用模型）"
                )
            except Exception as e:
                logger.debug(f"[Embedding] 补记向量指纹失败: {e}")

        # FaissVecDB 缺的这些，SQLite 里可能还留着上一代降级方案写的向量
        if using_faiss and missing:
            try:
                sqlite_embedded = set(db.get_all_embedding_paths())
            except Exception:
                sqlite_embedded = set()
            overlap = sum(1 for p in missing if p in sqlite_embedded)
            if overlap:
                logger.info(
                    f"[Embedding] 检测到 SQLite 里有 {overlap} 条旧向量，将重新写入 FaissVecDB"
                )

        targets = missing + stale
        if not targets:
            logger.info(
                f"[Embedding] 回填跳过 — {backend} 里全部 {len(all_paths)} 条向量都是最新的"
            )
            return 0

        if stale:
            logger.info(
                f"[Embedding] 回填开始 → {backend}: 缺少向量 {len(missing)} 条，"
                f"内容改过需要更新 {len(stale)} 条（全库 {len(all_paths)} 条）"
            )
        else:
            logger.info(
                f"[Embedding] 回填开始 → {backend}: {len(missing)}/{len(all_paths)} 条缺少向量"
            )

        stale_set = set(stale)
        written = 0
        for i in range(0, len(targets), batch_size):
            batch = targets[i : i + batch_size]
            batch_written = 0
            for path in batch:
                entry = idx.get(path) or {}
                if not entry:
                    try:
                        entry = db.get_emoji(path) or {}
                    except Exception:
                        entry = {}
                text = self._build_search_text(entry)
                if not text:
                    continue
                if path in stale_set:
                    # 先删旧向量再写新的；删不掉就跳过，否则会留下新旧两条重复文档
                    if not await self.delete_by_path(path):
                        logger.warning(f"[Embedding] 旧向量删不掉，本轮跳过更新: {path}")
                        continue
                if using_faiss:
                    # 直接写入 FaissVecDB
                    ok = await self._faiss_insert(path, text, entry)
                else:
                    # 写入 SQLite
                    ok = await self._fallback_insert(path, text)
                if ok:
                    batch_written += 1
            written += batch_written
            if batch_written > 0:
                logger.info(
                    f"[Embedding] 回填进度: {min(i + batch_size, len(targets))}/{len(targets)}, "
                    f"本批 +{batch_written}"
                )

        # 刷新缓存
        if not using_faiss:
            self._fallback_loaded = False
            self._load_fallback_matrix()

        logger.info(f"[Embedding] 回填完成 → {backend}: 成功 {written}/{len(targets)}")
        return written

    async def _read_embedded_paths(
        self, using_faiss: bool, db: Any, total_paths: int
    ) -> set[str] | None:
        """读出当前后端里已经有向量的 path 集合。

        返回 None 专门表示「读取失败」，用来和「读到了但是空的」区分开：
        后者是全新安装的正常情况，应该照常全量回填；前者必须中止本轮。
        """
        if not using_faiss:
            try:
                return set(db.get_all_embedding_paths())
            except Exception as e:
                logger.debug(f"[Embedding] 读取 SQLite 向量列表失败: {e}")
                return None

        try:
            ds = self._faiss_db.document_storage
            # limit 跟着库大小走：写死上限会让超出部分每次启动都被误判成缺失
            limit = max(100000, total_paths * 2)
            existing_docs = await ds.get_documents(metadata_filters={}, limit=limit)
        except Exception as e:
            logger.debug(f"[Embedding] 读取 FaissVecDB 文档列表失败: {e}")
            return None

        embedded: set[str] = set()
        for doc in existing_docs or []:
            if not isinstance(doc, dict):
                continue
            meta = doc.get("metadata", {})
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            if isinstance(meta, dict):
                p = meta.get("path", "")
                if p:
                    embedded.add(p)
        return embedded

    def _detect_stale_embeddings(
        self,
        all_paths: list[str],
        embedded: set[str],
        idx: dict[str, Any],
        db: Any,
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """比对文本指纹，挑出「向量已过期」和「缺指纹、只需补记」两批。

        纯字符串运算，不调用嵌入模型，几千条也只是毫秒级开销。
        """
        if not hasattr(db, "get_all_embedding_hashes"):
            return [], []
        try:
            stored = db.get_all_embedding_hashes()
        except Exception as e:
            logger.debug(f"[Embedding] 读取向量指纹失败，跳过过期检查: {e}")
            return [], []

        stale: list[str] = []
        hash_only: list[tuple[str, str]] = []
        for path in all_paths:
            if path not in embedded:
                continue
            entry = idx.get(path) or {}
            if not entry:
                try:
                    entry = db.get_emoji(path) or {}
                except Exception:
                    continue
            text = self._build_search_text(entry)
            if not text:
                continue
            fingerprint = self._text_fingerprint(text)
            old = stored.get(path)
            if old is None:
                hash_only.append((path, fingerprint))
            elif old != fingerprint:
                stale.append(path)
        return stale, hash_only

    async def rebuild_vectors(self, batch_size: int | None = None) -> dict[str, Any]:
        """强制重建全部文本向量：先清空向量与指纹，再整库回填。

        用于「怀疑检索命中的还是旧描述」时的手工兜底。会按库大小重新调用
        嵌入模型，额度自行评估。
        """
        if not self._embedding_enabled():
            return {"ok": False, "reason": "disabled", "written": 0, "total": 0}

        db = getattr(self.plugin, "db_service", None)
        total = 0
        if db is not None:
            try:
                total = len(db.get_all_paths())
            except Exception:
                total = 0

        logger.info(f"[Embedding] 强制重建全部向量，共 {total} 条")
        await self._wipe_vector_store()
        written = await self.backfill_existing(batch_size=batch_size)
        return {"ok": True, "reason": "", "written": written, "total": total}

    async def _faiss_insert(self, path: str, text: str, entry: dict[str, Any]) -> bool:
        """向 FaissVecDB 插入一条。"""
        if self._faiss_db is None:
            return False
        try:
            await self._faiss_db.insert(
                content=text,
                metadata={"path": path, "category": str(entry.get("category", ""))},
            )
            self._record_text_hash(path, text)
            return True
        except Exception as e:
            logger.debug(f"[Embedding] FaissVecDB 插入失败 {path}: {e}")
            return False

    # ═══════════════════════════════════════════════════
    #  清理
    # ═══════════════════════════════════════════════════

    async def _upgrade_corpus_if_needed(self) -> None:
        """语料格式变更时清掉旧向量，让 backfill 按新文档重建。

        只动 SQLite/Faiss 索引文件，不加载本地视觉模型。
        """
        db = getattr(self.plugin, "db_service", None)
        if not db or not hasattr(db, "get_meta_value"):
            return
        try:
            current = str(db.get_meta_value("embedding_text_version") or "")
        except Exception:
            current = ""
        if current == EMBEDDING_TEXT_VERSION:
            return

        logger.info(
            f"[Embedding] 语料版本 {current or 'v1'} -> {EMBEDDING_TEXT_VERSION}，重建文本向量"
        )
        await self._wipe_vector_store()
        try:
            db.set_meta_value("embedding_text_version", EMBEDDING_TEXT_VERSION)
        except Exception as e:
            logger.debug(f"[Embedding] 写入语料版本失败: {e}")

    async def _wipe_vector_store(self) -> None:
        """清空现有向量存储：关掉并删除 Faiss 索引文件 + 清空 SQLite 向量与指纹。

        只动索引文件，不加载任何本地模型。
        """
        if self._faiss_db is not None:
            try:
                await self._faiss_db.close()
            except Exception:
                pass
            self._faiss_db = None
            self._faiss_available = None

        data_dir = Path(self._resolve_data_dir())
        for name in ("emoji_faiss.db", "emoji_faiss.index"):
            path = data_dir / name
            try:
                if path.exists():
                    path.unlink()
            except Exception as e:
                logger.debug(f"[Embedding] 删除旧 Faiss 文件失败 {path}: {e}")

        db = getattr(self.plugin, "db_service", None)
        if db is not None and hasattr(db, "clear_all_embeddings"):
            try:
                db.clear_all_embeddings()
            except Exception as e:
                logger.debug(f"[Embedding] 清空 SQLite 向量失败: {e}")

        self.invalidate_cache()

    async def close(self) -> None:
        """关闭 FaissVecDB 并重置状态。"""
        if self._faiss_db is not None:
            try:
                await self._faiss_db.close()
            except Exception:
                pass
            self._faiss_db = None
            self._faiss_available = False
