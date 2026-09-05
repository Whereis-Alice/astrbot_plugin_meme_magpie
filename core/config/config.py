import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools

from ..util.normalization import normalize_character_key, normalize_label_list


# 早期版本（含上游 stealer）把内置 VLM 提示词写进了 _conf_schema.json 的 default，
# AstrBot 会把 schema 默认值原样落盘到用户配置里，于是“用户自定义提示词”永远非空，
# 内置提示词的后续改进再也到不了这些用户手上。下面是历史内置提示词（本插件 + 上游各版本、
# prompts.json 与 schema default 两种来源）的归一化 sha256 指纹，逐字命中就判定用户其实
# 没有自定义，回落到当前版本的 prompts.json。想固定自己的写法，只要让文本与历史内置版本
# 有任何一处不同（改一个字即可），指纹就不会命中。
_LEGACY_BUILTIN_PROMPT_FINGERPRINTS = frozenset(
    {
        "17d8e8c765af21bfe22450e8c9a54cf2215cf1c3e0f4e2802304c4a620a5019a",
        "195eb6e322f7fd30eaf1d7aa8597fbcde8e7681d7812c993882c82a14ad0c714",
        "314a8d4777db7aef601577d1162c13cc00f3a3f0f4c13294449ccbbd7b40e57c",
        "425be797a330a8f9da06bb792058288664d8c6190d5e88fdd7be625dccacc91f",
        "47f84a59f09f43c5451dd0e436616791badfc95f13eda938e0132092bd5aa18b",
        "490e2fdfb52110920cf82daad3b2542a09049803b7459cbd3d8775b0bc13ee26",
        "4ef8a9087c85229377091e5c6273af2da032a8c38fd696515f8fec3390d5427d",
        "5226741ed4541357c5f644df62597c861f7aed7ea543ea013d57fdc0baad70bf",
        "68a0081e9f14e54ff95a1320b3884ca6025dfb1018cc944b916bcd393009601e",
        "7c1da5f6f69203ac74ba798b12301d097e0b685a2c9a9d619e22b02a2c46d899",
        "9bb9f189a6c76f7ab12d8a6f8dcbb5ea650e4bf53db69042a66e31d2fa2d3119",
        "d2f32f99a11c8e8b082e1ae670ff0b363a1c419ab78dd2ff71d3cc758e60f12b",
        "d96b1986e796c8da2f16148d971fa947ec1fd719be9853898d3c6dd73fab14d0",
        "ed0cf59b39baf77087ac3f273611a892a404ed1f441acf2d7d33b5fd57e81c71",
    }
)

# 每个配置项每进程只提示一次，避免每次热重载配置都刷日志
_healed_prompt_keys: set[str] = set()


def _prompt_fingerprint(text: str) -> str:
    """归一化后取 sha256：统一换行、去掉行尾空白与首尾空行，避免编辑器差异造成误判。"""
    normalized = "\n".join(
        line.rstrip() for line in str(text).replace("\r\n", "\n").strip().split("\n")
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_legacy_builtin_prompt(text: str) -> bool:
    """判断这段自定义提示词是否只是历史内置提示词的逐字副本。"""
    return _prompt_fingerprint(text) in _LEGACY_BUILTIN_PROMPT_FINGERPRINTS


class PluginConfig(BaseModel):
    # === 基础功能 ===
    steal_meme: bool = False
    steal_mode: str = "probability"  # "probability" 或 "cooldown"
    steal_chance: float = 0.3  # 概率模式下的偷图概率
    auto_send_meme: bool = True
    meme_chance: float = 0.2
    send_meme_as_gif: bool = False
    meme_send_char_delay: float = 0.3
    meme_send_delay: float = 5.0
    meme_send_delay_random: bool = False
    meme_send_delay_max: float = 8.0
    auto_meme_intent_gate: bool = True
    auto_meme_cancel_on_new_message: bool = True

    # === 自动表情的投递方式 ===
    # separate：表情作为独立的一条消息发出（默认，和以往行为一致）
    # attach：把表情追加到机器人这条回复的消息链末尾，这样分段回复类插件
    #         （如 outputpro_split）能看见它并一起编排
    auto_meme_delivery_mode: str = "separate"
    auto_meme_attach_timeout: float = 10.0  # attach 模式下等待选图的秒数，超时回退 separate
    auto_meme_attach_compat_split: bool = False  # 见 _conf_schema.json 里的说明

    # === 群聊过滤 ===
    steal_target_whitelist: list[str] = []
    steal_target_blacklist: list[str] = []
    send_target_whitelist: list[str] = []
    send_target_blacklist: list[str] = []
    steal_target_filter_mode: str = "whitelist_first"
    send_target_filter_mode: str = "whitelist_first"

    # === QQ 官方平台 ===
    # QQ_Official（QQ 官方机器人平台）消息中的大表情/表情包是普通图片附件，
    # 无 OneBot 的 sub_type 标记。收录模式（单选）：
    # - cdn_only：仅收录带表情 CDN 特征的图片（默认）
    # - all_images：该平台消息中的图片附件全部按表情收录（普通图片也会被收）
    # - gif_only：仅收录 GIF 格式的图片（QQ 表情包多为 GIF，可过滤静态图/普通图）
    qqofficial_steal_mode: str = "cdn_only"

    # === 模型配置 ===
    vision_provider_id: str = ""

    # === LLM 主动偷图时的参数优先级 ===
    # 主对话 LLM 往往已经从上下文知道“群里在聊哪部番、这是谁”，
    # 比轻量 VLM 看图猜角色准得多。三种策略：
    # - merge：LLM 传的字段优先，缺的部分交给 VLM 补（默认，最稳）
    # - llm_first：LLM 信息足够（有分类 + 至少一个语义字段）就不再调 VLM，省额度
    # - vlm_only：完全忽略 LLM 传参，保持旧行为
    llm_steal_param_mode: str = "merge"

    # === 批量识别限流（WebUI 批量导入 / 批量重识别）===
    # 上游模型服务商普遍有 RPM 限制，一口气刷几百张图很容易吃 429。
    batch_analyze_concurrency: int = 2  # 同时调用视觉模型的并发数
    batch_analyze_rpm: int = 20  # 每分钟最大请求数（0 = 不限）
    batch_analyze_max_retries: int = 3  # 遇 429 / 超时的重试次数
    batch_analyze_retry_backoff: float = 2.0  # 重试退避基数（秒），指数增长

    # === 内部常量/高级配置 ===
    # 表情库上限；0 = 不限制。超出上限时会「永久删除」最旧的表情包（收藏除外），
    # 所以默认值给得宽松：宁可多占点磁盘，也不要静悄悄把用户攒的图删掉。
    max_reg_num: int = 2000
    # 后台每小时的容量巡检是否直接删文件。默认只在日志里告警，删不删由用户决定。
    capacity_auto_cleanup: bool = False
    content_filtration: bool = False  # 内容审核开关
    content_filtration_fail_open: bool = False
    storage_cleanup_strategy: str = "balanced"
    image_processing_cooldown: int = 30
    enable_natural_emotion_analysis: bool = True  # 情绪识别模式
    emotion_analysis_provider_id: str = ""  # 情绪分析专用模型
    smart_meme_selection: bool = True  # 智能表情包选择

    # === 待审核池 / 嵌入检索 ===
    steal_pool_capacity: int = 200  # 待审核池容量上限，到达即暂停自动偷取
    enable_embedding_search: bool = True  # 启用嵌入向量检索；不可用时降级 BM25
    embedding_provider_id: str = ""  # 嵌入模型；留空则尝试框架首个 embedding provider

    # === 智能选择：文字距离融合权重（预设见 _conf_schema.json _smart_section）===
    sim_weight_preset: str = "balanced"  # balanced / keyword / semantic / strict
    sim_weight_ngram: float = 0.28  # 兼容旧配置保留，不再单独暴露
    sim_weight_cosine: float = 0.25
    sim_weight_substring: float = 0.12
    sim_weight_char: float = 0.08
    sim_weight_edit: float = 0.27
    sim_negation_penalty: float = 0.25

    # 文字距离融合权重预设（键名对齐 configure_similarity）
    SIM_WEIGHT_PRESETS: ClassVar[dict[str, dict[str, float]]] = {
        "balanced": {
            "ngram": 0.28,
            "cosine": 0.25,
            "substring": 0.12,
            "char": 0.08,
            "edit": 0.27,
            "negation": 0.25,
        },
        "keyword": {
            "ngram": 0.15,
            "cosine": 0.10,
            "substring": 0.35,
            "char": 0.15,
            "edit": 0.25,
            "negation": 0.20,
        },
        "semantic": {
            "ngram": 0.35,
            "cosine": 0.35,
            "substring": 0.05,
            "char": 0.05,
            "edit": 0.20,
            "negation": 0.30,
        },
        "strict": {
            "ngram": 0.20,
            "cosine": 0.10,
            "substring": 0.20,
            "char": 0.20,
            "edit": 0.30,
            "negation": 0.35,
        },
    }

    # === 自定义提示词（VLM 分类 + LLM 小模型情绪分析） ===
    custom_meme_classification_prompt: str = ""
    custom_meme_classification_with_filter_prompt: str = ""
    emotion_analysis_prompt: str = ""

    # === 内化常量（不再暴露给用户） ===
    DO_REPLACE: ClassVar[bool] = True  # 达到上限始终替换旧表情
    ENABLE_RAW_CLEANUP: ClassVar[bool] = True  # raw 始终自动清理
    RAW_CLEANUP_INTERVAL: ClassVar[int] = 30  # 清理周期(分钟)，固定
    ENABLE_CAPACITY_CONTROL: ClassVar[bool] = True  # 始终启用容量控制
    CAPACITY_CONTROL_INTERVAL: ClassVar[int] = 60  # 容量检查周期(分钟)，固定
    RAW_RETENTION_MINUTES: ClassVar[int] = 60  # 原始图片保留时间(分钟)，固定

    # === 分类信息 ===
    categories: list[str] = []
    category_info: dict[str, dict[str, str]] = {}
    characters: list[str] = []
    character_info: dict[str, dict[str, str]] = {}

    # === 待审核池 ===
    # 自动偷取时是否进入待审核池等待人工通过。
    # True：进入 pending，需在 WebUI 审核区通过后才入库（默认，安全）。
    # False：跳过审核，自动入库（issue #89，方便"看到就收"的用户）。
    audit_required: bool = True

    # WebUI 默认主题：auto/dark/light/pixel/terminal。页面内切换后写入 KV，优先于该项。
    webui_theme: str = "auto"

    # === 外部表情包源（Meme Pack 压缩包 / GitHub 仓库 / JSON 目录接口）===
    # 这些上限直接决定一次导入能吃掉多少磁盘和内存，默认值按"小机器也扛得住"设定。
    external_sources_enabled: bool = True  # 总开关；关掉后外部源相关接口一律拒绝
    external_source_allow_http: bool = False  # 是否允许明文 http:// 源（默认只允许 https）
    external_source_default_review: bool = False  # 导入的图默认先进待审核池
    external_source_max_items: int = 2000  # 单个源最多读取多少张图
    external_source_max_image_bytes: int = 33_554_432  # 单张图字节上限（32 MiB）
    external_source_max_archive_bytes: int = 1_073_741_824  # 压缩包体积上限（1 GiB）
    external_source_max_uncompressed_bytes: int = 4_294_967_296  # 解压后总字节上限（4 GiB）
    external_source_max_pixels: int = 40_000_000  # 单张图像素上限，挡解压炸弹

    # === 内部状态 (不作为 Pydantic 字段) ===
    # 使用 PrivateAttr 或在 __init__ 中设置且不包含在 __annotations__ 中
    # 但 Pydantic v1/v2 处理方式不同。这里使用 __private_attributes__ 机制或直接忽略

    # 忽略额外字段（Pydantic v2 model_config）
    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    # === 常量 ===
    # 使用 ClassVar 标注，避免被 Pydantic 识别为字段
    DEFAULT_CATEGORIES: ClassVar[list[str]] = [
        "happy",
        "sad",
        "angry",
        "shy",
        "surprised",
        "troll",
        "cry",
        "confused",
        "embarrassed",
        "love",
        "disgust",
        "fear",
        "excitement",
        "tired",
        "sigh",
        "thank",
        "dumb",
    ]

    DEFAULT_CATEGORY_INFO: ClassVar[dict[str, dict[str, str]]] = {
        "happy": {"name": "开心", "desc": "快乐、愉悦、满足、好心情"},
        "sad": {"name": "难过", "desc": "悲伤、沮丧、失落、emo"},
        "angry": {"name": "生气", "desc": "愤怒、恼火、不满、暴躁"},
        "shy": {"name": "害羞", "desc": "羞涩、不好意思、腼腆"},
        "surprised": {"name": "惊讶", "desc": "意外、震惊、惊奇、啊？"},
        "troll": {"name": "整活", "desc": "调皮、搞怪、发癫、抽象"},
        "cry": {"name": "哭哭", "desc": "哭泣、流泪、委屈、破防"},
        "confused": {"name": "困惑", "desc": "迷茫、不解、疑惑、问号脸"},
        "embarrassed": {"name": "尴尬", "desc": "社死、窘迫、为难、脚趾抠地"},
        "love": {"name": "喜欢", "desc": "喜爱、爱慕、宠溺、心动"},
        "disgust": {"name": "嫌弃", "desc": "厌恶、反感、讨厌、yue"},
        "fear": {"name": "害怕", "desc": "恐惧、担心、紧张、怂"},
        "excitement": {"name": "兴奋", "desc": "激动、亢奋、嗨、上头"},
        "tired": {"name": "困倦", "desc": "疲惫、困、无力、想躺"},
        "sigh": {"name": "无奈", "desc": "叹气、摆烂、算了、心累"},
        "thank": {"name": "感谢", "desc": "道谢、感恩、收到、爱了"},
        "dumb": {"name": "无语", "desc": "呆住、傻眼、离谱、沉默"},
    }

    DEFAULT_CATEGORY_ALIASES: ClassVar[dict[str, str]] = {
        "开心": "happy",
        "高兴": "happy",
        "快乐": "happy",
        "哈哈": "happy",
        "笑": "happy",
        "难过": "sad",
        "伤心": "sad",
        "emo": "sad",
        "沮丧": "sad",
        "失落": "sad",
        "生气": "angry",
        "愤怒": "angry",
        "恼火": "angry",
        "暴躁": "angry",
        "害羞": "shy",
        "不好意思": "shy",
        "腼腆": "shy",
        "惊讶": "surprised",
        "震惊": "surprised",
        "意外": "surprised",
        "搞怪": "troll",
        "整活": "troll",
        "发癫": "troll",
        "抽象": "troll",
        "哭": "cry",
        "大哭": "cry",
        "哭哭": "cry",
        "委屈": "cry",
        "破防": "cry",
        "困惑": "confused",
        "疑惑": "confused",
        "迷茫": "confused",
        "问号": "confused",
        "尴尬": "embarrassed",
        "社死": "embarrassed",
        "为难": "embarrassed",
        "喜欢": "love",
        "喜爱": "love",
        "爱": "love",
        "心动": "love",
        "嫌弃": "disgust",
        "厌恶": "disgust",
        "反感": "disgust",
        "yue": "disgust",
        "害怕": "fear",
        "恐惧": "fear",
        "紧张": "fear",
        "怂": "fear",
        "兴奋": "excitement",
        "激动": "excitement",
        "嗨": "excitement",
        "上头": "excitement",
        "疲惫": "tired",
        "困": "tired",
        "困倦": "tired",
        "想睡": "tired",
        "无奈": "sigh",
        "叹气": "sigh",
        "摆烂": "sigh",
        "算了": "sigh",
        "感谢": "thank",
        "谢谢": "thank",
        "多谢": "thank",
        "感恩": "thank",
        "无语": "dumb",
        "傻眼": "dumb",
        "离谱": "dumb",
        "沉默": "dumb",
    }

    def __init__(self, config: AstrBotConfig | None, context: Context | None = None):
        # 1. 初始化 Pydantic 模型
        # config 可能是 AstrBotConfig (dict-like) 或 None
        initial_data = config if config else {}
        super().__init__(**initial_data)

        # 2. 保存 AstrBotConfig 引用以便回写
        # 使用 object.__setattr__ 绕过 Pydantic 的 setattr 检查
        object.__setattr__(self, "_data", config)
        object.__setattr__(self, "_plugin_name", "astrbot_plugin_meme_magpie")

        # 3. 初始化路径和目录
        data_dir = Path(StarTools.get_data_dir(self._plugin_name)).resolve()
        object.__setattr__(self, "data_dir", data_dir)
        object.__setattr__(self, "categories_path", data_dir / "categories.json")
        object.__setattr__(self, "raw_dir", data_dir / "raw")
        object.__setattr__(self, "categories_dir", data_dir / "categories")
        object.__setattr__(self, "cache_dir", data_dir / "cache")
        object.__setattr__(self, "pending_dir", data_dir / "pending")
        object.__setattr__(self, "category_info_path", data_dir / "category_info.json")
        object.__setattr__(self, "characters_path", data_dir / "characters.json")
        object.__setattr__(self, "character_info_path", data_dir / "character_info.json")

        # 确保目录存在
        self.ensure_base_dirs()

        self._load_category_state()
        self._migrate_category_config()
        self._migrate_legacy_prompt_config()
        self._refresh_target_policy_cache()

    def _read_json_file(self, path: Path):
        try:
            if not path.exists():
                return None
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"[Config] JSON 解析失败 {path}: {e}")
            return None
        except Exception as e:
            logger.debug(f"[Config] 读取文件失败 {path}: {e}")
            return None

    def _write_json_file(self, path: Path, data: Any) -> bool:
        """写入 JSON 文件。

        Args:
            path: 文件路径
            data: 要写入的数据

        Returns:
            bool: 是否写入成功
        """
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except PermissionError as e:
            logger.error(f"[Config] 权限不足，无法写入文件 {path}: {e}")
            return False
        except OSError as e:
            logger.error(f"[Config] 写入文件失败 {path}: {e}")
            return False
        except Exception as e:
            logger.error(f"[Config] 写入 JSON 文件时发生未知错误 {path}: {e}")
            return False

    def _load_category_state(self) -> None:
        stored_categories = self._read_json_file(self.categories_path)
        stored_info = self._read_json_file(self.category_info_path)

        config_categories = None
        config_info = None
        if isinstance(self._data, dict):
            if "categories" in self._data:
                config_categories = self._data.get("categories")
            if "category_info" in self._data:
                config_info = self._data.get("category_info")

        categories = (
            stored_categories
            if isinstance(stored_categories, list) and stored_categories
            else config_categories
            if isinstance(config_categories, list) and config_categories
            else list(self.DEFAULT_CATEGORIES)
        )
        info = (
            stored_info
            if isinstance(stored_info, dict)
            else config_info
            if isinstance(config_info, dict)
            else {}
        )

        # 使用 BaseModel.__setattr__ 绕过自定义 __setattr__ 中的写文件逻辑，
        # 避免初始化期间重复写文件（最后统一写一次即可）
        BaseModel.__setattr__(self, "categories", list(categories))
        merged_info = dict(self.DEFAULT_CATEGORY_INFO)
        merged_info.update(info)
        BaseModel.__setattr__(self, "category_info", merged_info)
        self.save_categories()
        self.save_category_info()
        self._load_character_state()

    def get_categories(self) -> list[str]:
        """返回当前分类列表；为空时回退到 DEFAULT_CATEGORIES。

        各 service 读取分类时统一走这个方法，避免在多处重复
        `self.categories or DEFAULT_CATEGORIES` 模板。
        """
        cats = list(self.categories or [])
        if not cats:
            cats = list(self.DEFAULT_CATEGORIES)
        return cats

    def get_vlm_categories(self) -> list[str]:
        """给 VLM 的分类列表，不含 other。"""
        return [key for key in self.get_categories() if key != "other"]

    def closest_category(self, raw: str) -> str:
        """把无法识别的分类名收到最接近的已有情绪类，不再使用 other。"""
        known = self.get_vlm_categories()
        if not known:
            known = [key for key in self.DEFAULT_CATEGORIES if key != "other"]
        raw_l = str(raw or "").strip().lower()
        if not raw_l:
            return "confused" if "confused" in known else known[0]
        strict = self.normalize_category_strict(raw_l)
        if strict and strict != "other" and strict in known:
            if strict != "other":
                return strict
        info_map = self.category_info or self.DEFAULT_CATEGORY_INFO
        for key in known:
            info = info_map.get(key) or {}
            name = str(info.get("name") or "").strip().lower()
            desc = str(info.get("desc") or "").strip().lower()
            if name and (raw_l == name or raw_l in name or name in raw_l):
                return key
            if raw_l and raw_l in desc:
                return key
        return "confused" if "confused" in known else known[0]

    def _migrate_legacy_prompt_config(self) -> None:
        """把落盘的旧版内置提示词清空，恢复“留空 = 跟随内置提示词”的语义。

        旧版 _conf_schema.json 把内置提示词写成了配置项默认值，AstrBot 会把 schema
        默认值原样存进用户配置。留着不动的话，配置页看上去像是用户自己写了一大段提示词，
        实际只是历史默认值，而且会一直挡住内置提示词的后续改进。这里只清理与历史内置
        文本逐字一致的副本，用户改过任何一处都不会被动。
        """
        if not isinstance(self._data, dict):
            return
        cleared: list[str] = []
        for _out_key, config_key, _bundled_key in self._PROMPT_SOURCES:
            saved = self._data.get(config_key)
            if not isinstance(saved, str) or not saved.strip():
                continue
            if not _is_legacy_builtin_prompt(saved):
                continue
            self._data[config_key] = ""
            setattr(self, config_key, "")
            cleared.append(config_key)
        if not cleared:
            return
        if hasattr(self._data, "save_config"):
            try:
                self._data.save_config()
            except Exception as e:
                logger.warning(f"[Config] 清理旧版内置提示词副本失败（不影响本次运行）: {e}")
                return
        logger.info(
            "[Config] 已清空配置项 "
            + "、".join(cleared)
            + "：内容只是旧版内置提示词的副本，现在改为跟随插件内置提示词（会随版本更新改进）。"
            "想用自己的提示词，直接在配置页填写即可。"
        )

    def _migrate_category_config(self) -> None:
        if not isinstance(self._data, dict):
            return
        removed = False
        if "categories" in self._data:
            del self._data["categories"]
            removed = True
        if "category_info" in self._data:
            del self._data["category_info"]
            removed = True
        if removed and hasattr(self._data, "save_config"):
            self._data.save_config()

    def __setattr__(self, key: str, value: Any):
        super().__setattr__(key, value)

        if key in self._TARGET_POLICY_CONFIG_KEYS:
            self._refresh_target_policy_cache()

        if key in ("categories", "category_info"):
            if key == "categories":
                self.save_categories()
            else:
                self.save_category_info()
        if key in ("characters", "character_info"):
            if key == "characters":
                self.save_characters()
            else:
                self.save_character_info()

    def update_config(self, updates: dict) -> bool:
        """批量更新配置项。

        Args:
            updates: 配置更新字典

        Returns:
            bool: 是否更新成功
        """
        try:
            fields = getattr(type(self), "model_fields", None)
            if fields is None:
                fields = getattr(type(self), "__fields__", {})
            valid_updates: dict[str, Any] = {}
            for key, value in updates.items():
                if fields is not None and key not in fields:
                    logger.debug(f"[Config] 忽略已移除的配置键: {key}")
                    continue
                valid_updates[key] = value
                setattr(self, key, value)

            # 回写到 AstrBotConfig
            if hasattr(self, "_data") and self._data is not None:
                if hasattr(self._data, "save_config"):
                    self._data.save_config(valid_updates)
                elif isinstance(self._data, dict):
                    self._data.update(valid_updates)
            return True
        except Exception as e:
            logger.error(f"更新配置失败: {e}")
            return False

    _TARGET_POLICY_CONFIG_KEYS: ClassVar[set[str]] = {
        "send_target_whitelist",
        "send_target_blacklist",
        "send_target_filter_mode",
        "steal_target_whitelist",
        "steal_target_blacklist",
        "steal_target_filter_mode",
    }

    def _normalize_target_collection(self, values: list[str] | None) -> frozenset[str]:
        normalized: set[str] = set()
        for value in values or []:
            target = self.normalize_target_entry(value)
            if target:
                normalized.add(target)
        return frozenset(normalized)

    def _refresh_target_policy_cache(self) -> None:
        object.__setattr__(
            self,
            "_target_policy_cache",
            {
                "send": {
                    "whitelist": self._normalize_target_collection(self.send_target_whitelist),
                    "blacklist": self._normalize_target_collection(self.send_target_blacklist),
                    "mode": self._normalize_filter_mode(self.send_target_filter_mode),
                },
                "steal": {
                    "whitelist": self._normalize_target_collection(self.steal_target_whitelist),
                    "blacklist": self._normalize_target_collection(self.steal_target_blacklist),
                    "mode": self._normalize_filter_mode(self.steal_target_filter_mode),
                },
            },
        )

    def save_categories(self) -> None:
        self._write_json_file(self.categories_path, self.categories)

    def save_category_info(self) -> None:
        self._write_json_file(self.category_info_path, self.category_info)

    def _load_character_state(self) -> None:
        stored_characters = self._read_json_file(self.characters_path)
        stored_info = self._read_json_file(self.character_info_path)
        characters = (
            list(stored_characters)
            if isinstance(stored_characters, list) and stored_characters
            else []
        )
        info = stored_info if isinstance(stored_info, dict) else {}
        BaseModel.__setattr__(
            self,
            "characters",
            normalize_label_list(characters, allow_duplicates=True),
        )
        BaseModel.__setattr__(self, "character_info", dict(info))
        self.save_characters()
        self.save_character_info()

    def save_characters(self) -> None:
        self._write_json_file(self.characters_path, self.characters)

    def save_character_info(self) -> None:
        self._write_json_file(self.character_info_path, self.character_info)

    @staticmethod
    def normalize_character_key(value: str) -> str:
        return normalize_character_key(value)

    def get_characters(self) -> list[str]:
        return [key for key in (self.characters or []) if key]

    def get_character_info_list(self) -> list[dict[str, str]]:
        info_map = self.character_info or {}
        result: list[dict[str, str]] = []
        for key in self.get_characters():
            info = info_map.get(key, {}) if isinstance(info_map, dict) else {}
            result.append(
                {
                    "key": key,
                    "name": str(info.get("name", "") or key),
                    "desc": str(info.get("desc", "") or ""),
                }
            )
        return result

    def ensure_category_dir(self, category: str) -> Path:
        category_dir = self.categories_dir / str(category)
        category_dir.mkdir(parents=True, exist_ok=True)
        return category_dir

    def ensure_category_dirs(self, categories: list[str] | None) -> None:
        if not categories:
            return
        for category in categories:
            self.ensure_category_dir(category)

    def ensure_raw_dir(self) -> Path:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        return self.raw_dir

    def ensure_cache_dir(self) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir

    def ensure_pending_dir(self) -> Path:
        """确保待审核池目录存在，并返回其路径。"""
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        return self.pending_dir

    def ensure_base_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.categories_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)

    def normalize_category_strict(self, category: str) -> str | None:
        """严格归一化情绪分类。"""
        if not category:
            return None

        category = category.lower().strip()

        # 1. 直接匹配当前配置的分类列表（包括用户自定义分类）
        if category in self.categories:
            return category

        # 2. 匹配默认分类（兜底）
        if category in self.DEFAULT_CATEGORIES:
            return category

        # 3. 别名查找
        return self.DEFAULT_CATEGORY_ALIASES.get(category)

    def get_keyword_map(self) -> dict[str, str]:
        """获取关键词映射表。"""
        return self.DEFAULT_CATEGORY_ALIASES

    _PROMPT_SOURCES: ClassVar[tuple[tuple[str, str, str], ...]] = (
        (
            "emoji_classification_prompt",
            "custom_meme_classification_prompt",
            "EMOJI_CLASSIFICATION_PROMPT",
        ),
        (
            "emoji_classification_with_filter_prompt",
            "custom_meme_classification_with_filter_prompt",
            "EMOJI_CLASSIFICATION_WITH_FILTER_PROMPT",
        ),
    )

    def get_prompts(self, default_prompts: dict[str, str] | None = None) -> dict[str, str]:
        """获取 VLM 分类提示词；用户自定义非空时优先，为空回退到插件自带 prompts.json。

        自定义内容如果只是历史内置提示词的逐字副本（多半是旧版把内置文本写成了配置
        默认值、被 AstrBot 落盘留下来的），按“未自定义”处理并打一条日志，
        否则内置提示词的改进永远到不了这些用户。
        """
        default_prompts = default_prompts or {}
        result = {
            "emoji_classification_prompt": "",
            "emoji_classification_with_filter_prompt": "",
        }

        for out_key, config_key, bundled_key in PluginConfig._PROMPT_SOURCES:
            custom = str(getattr(self, config_key, "") or "").strip()
            if custom and _is_legacy_builtin_prompt(custom):
                if config_key not in _healed_prompt_keys:
                    _healed_prompt_keys.add(config_key)
                    logger.info(
                        f"[Config] 配置项 {config_key} 的内容与旧版内置提示词逐字一致，"
                        "已按“未自定义”处理并改用当前版本的内置提示词；"
                        "若确实想固定自己的写法，把这段文本改动一处即可。"
                    )
                custom = ""
            if custom:
                result[out_key] = custom
            elif default_prompts:
                result[out_key] = str(default_prompts.get(bundled_key, "") or "")

        return result

    def get_category_info(self) -> list[dict[str, str]]:
        categories = self.categories or list(self.DEFAULT_CATEGORIES)
        info_map = self.category_info or {}

        result: list[dict[str, str]] = []
        for key in categories:
            info = info_map.get(key, {}) if isinstance(info_map, dict) else {}
            name = str(info.get("name", "") or key)
            desc = str(info.get("desc", "") or "")
            result.append({"key": str(key), "name": name, "desc": desc})
        return result

    def get_group_id(self, event: AstrMessageEvent) -> str:
        """获取群号。"""
        try:
            return event.get_group_id()
        except Exception:
            return ""

    def get_user_id(self, event: AstrMessageEvent) -> str:
        try:
            user_id = event.get_sender_id()
            if user_id:
                return str(user_id).strip()
        except Exception:
            pass

        for attr in ("sender_id", "user_id"):
            try:
                value = getattr(event, attr, None)
            except Exception:
                value = None
            if value:
                return str(value).strip()

        try:
            message_obj = getattr(event, "message_obj", None)
            sender = getattr(message_obj, "sender", None) if message_obj else None
            user_id = getattr(sender, "user_id", None) if sender is not None else None
            if user_id:
                return str(user_id).strip()
        except Exception:
            pass

        return ""

    def get_event_target(self, event: AstrMessageEvent) -> tuple[str, str]:
        group_id = self.get_group_id(event)
        if group_id:
            return "group", str(group_id).strip()

        user_id = self.get_user_id(event)
        if user_id:
            return "user", str(user_id).strip()

        return "", ""

    def get_event_targets(self, event: AstrMessageEvent) -> list[str]:
        targets: list[str] = []
        seen: set[str] = set()

        group_id = self.get_group_id(event)
        if group_id:
            normalized = self.normalize_target_entry(group_id, "group")
            if normalized and normalized not in seen:
                seen.add(normalized)
                targets.append(normalized)

        user_id = self.get_user_id(event)
        if user_id:
            normalized = self.normalize_target_entry(user_id, "user")
            if normalized and normalized not in seen:
                seen.add(normalized)
                targets.append(normalized)

        return targets

    @staticmethod
    def normalize_target_entry(value: object, default_scope: str = "group") -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""

        lowered = raw.lower()
        for prefix, scope in (
            ("group:", "group"),
            ("g:", "group"),
            ("群:", "group"),
            ("user:", "user"),
            ("u:", "user"),
            ("qq:", "user"),
            ("好友:", "user"),
            ("私聊:", "user"),
        ):
            if lowered.startswith(prefix):
                target_id = raw[len(prefix) :].strip()
                return f"{scope}:{target_id}" if target_id else ""

        if ":" in raw:
            scope, target_id = raw.split(":", 1)
            scope = scope.strip().lower()
            target_id = target_id.strip()
            if scope in {"group", "user"} and target_id:
                return f"{scope}:{target_id}"

        return f"{default_scope}:{raw}" if raw else ""

    def _get_action_lists(self, action: str) -> tuple[list[str], list[str]]:
        policy = self._get_action_policy(action)
        return (sorted(policy["whitelist"]), sorted(policy["blacklist"]))

    @staticmethod
    def _normalize_filter_mode(value: object) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"blacklist_first", "blacklist", "bl", "black"}:
            return "blacklist_first"
        return "whitelist_first"

    def _get_action_filter_mode(self, action: str) -> str:
        return str(self._get_action_policy(action)["mode"])

    def _get_action_policy(self, action: str) -> dict[str, object]:
        cache = getattr(self, "_target_policy_cache", None)
        if not isinstance(cache, dict):
            self._refresh_target_policy_cache()
            cache = getattr(self, "_target_policy_cache", {})
        return cache.get(str(action or "").strip().lower(), {}) or {}

    def is_action_allowed(self, action: str, event: AstrMessageEvent) -> bool:
        targets = self.get_event_targets(event)
        if not targets:
            return True
        return self._is_normalized_targets_allowed(action, targets)

    def is_targets_allowed(self, action: str, target_entries: list[str]) -> bool:
        normalized_targets: list[str] = []
        seen: set[str] = set()
        for entry in target_entries or []:
            normalized = self.normalize_target_entry(entry)
            if normalized and normalized not in seen:
                seen.add(normalized)
                normalized_targets.append(normalized)

        if not normalized_targets:
            return True

        return self._is_normalized_targets_allowed(action, normalized_targets)

    def _is_normalized_targets_allowed(self, action: str, normalized_targets: list[str]) -> bool:
        if not normalized_targets:
            return True

        policy = self._get_action_policy(action)
        whitelist = policy.get("whitelist", frozenset())
        blacklist = policy.get("blacklist", frozenset())
        filter_mode = str(policy.get("mode", "whitelist_first"))
        whitelist_hit = any(target in whitelist for target in normalized_targets)
        blacklist_hit = any(target in blacklist for target in normalized_targets)

        if filter_mode == "blacklist_first":
            if blacklist_hit:
                return False
            if whitelist:
                return whitelist_hit
            return True

        if whitelist_hit:
            return True
        if blacklist_hit:
            return False
        if whitelist:
            return False
        return True

    def is_target_allowed(self, action: str, target_entry: str) -> bool:
        return self.is_targets_allowed(action, [target_entry])

    def is_group_allowed(self, group_id: str) -> bool:
        """检查群组是否允许。"""
        if not group_id:
            return True

        return self.is_target_allowed("send", f"group:{group_id}")
