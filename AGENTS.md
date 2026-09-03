# AGENTS.md

本文件面向在此仓库中工作的 AI 编码助手与新加入的开发者，用于快速建立架构认知并避开已知陷阱。

## 项目概览

`astrbot_plugin_meme_magpie`（表情包喜鹊）是一个 AstrBot 插件：自动从群聊中收集图片，用视觉语言模型（VLM）分类打标，在对话中按情绪与语义匹配发送表情包。提供 WebUI 管理面板，并向 LLM 暴露搜索 / 发送 / 主动收图三个工具。

本插件是 [`astrbot_plugin_stealer`](https://github.com/nagatoquin33/astrbot_plugin_stealer) 的衍生作品，遵循 **AGPL-3.0**。改动记录见 `CHANGELOG.md`，衍生关系与版权声明见 `NOTICE`。

与上游相比，标识符已全面隔离，避免同时安装时冲突：

| 项目 | 本插件 |
| --- | --- |
| 包名 / 数据目录 | `astrbot_plugin_meme_magpie` |
| 命令组 | `/magpie`（别名 `喜鹊`） |
| Web API 前缀 | `/astrbot_plugin_meme_magpie/*` |
| LLM 工具名 | `magpie_search_meme` / `magpie_send_meme` / `magpie_steal_meme` |
| 前端目录 | `pages/dashboard/` |

⚠️ 配置项的 key（`_conf_schema.json` 里的字段名）**故意与上游保持一致**，以便迁移。不要为了"统一命名"去全局替换 `meme` / `send_meme` / `steal_meme` 这类子串。

## 架构

### 插件生命周期

AstrBot 以 `main.py` 为入口。`Main` 类（继承 `astrbot.api.star.Star`）在 `__init__` 中初始化所有服务并互相接线：

```
Main (main.py)
├── PluginConfig (core/config/config.py)              -- Pydantic 配置，包装 AstrBotConfig，派生所有路径
├── DatabaseService (core/db/database_service.py)     -- SQLite + WAL，表情索引，SCHEMA_VERSION = 7
├── IndexManager (core/db/index_manager.py)           -- 内存索引与 DB 的同步
├── CacheService (cache_service.py)                   -- 索引 / 图片 / 冷却期内存缓存
├── CommandHandler (core/commands/command_handler.py) -- /magpie 各子命令实现
├── EventHandler (core/events/event_handler.py)       -- 消息监听、图片下载、强制收图窗口
├── BackgroundStealQueue (core/events/background_steal_queue.py) -- 后台收图队列
├── ImageProcessorService (core/processing/image_processor_service.py) -- VLM 分类、去重、入库
├── AnalysisThrottle (core/processing/analysis_throttle.py)  -- 并发 / RPM 限流 + 指数退避重试
├── LlmMemeHints (core/processing/llm_meme_hints.py)  -- LLM 自传元数据的清洗与合并
├── MemeSelector (core/search/meme_selector.py)       -- 检索入口：BM25 预筛 + 模糊重排 + 语义向量
├── MemeSenderEngine (core/events/meme_sender_engine.py) -- 自动发送决策与冷却
├── MigrationService (core/maintenance/migration_service.py) -- 从上游插件迁移数据
├── PluginAPI (plugin_api.py)                         -- WebUI 的 38 条 Web API 路由
└── TaskScheduler (task_scheduler.py)                 -- 定时任务（清理、容量控制）
```

### 数据流

1. **收图**：`EventHandler` 监听消息 → `ImageDownloadService` 下载 → `ImageProcessorService` 算感知哈希去重、调 VLM 取分类/标签/描述/情绪 → `DatabaseService` 写 SQLite。
2. **检索**：`MemeSelector` 调 `MemeSearchEngine`（BM25 预筛 + 模糊重排）与 `MemeSmartSelectService`（语义向量），再由 `MemeSelectionStrategy` 加最近使用惩罚与随机性选出一张。
3. **发送**：`MemeSenderEngine` 在 `on_decorating_result` 拦截 LLM 回复，按冷却与概率决定是否配图，异步投递。
4. **WebUI**：`PluginAPI` 通过 `context.register_web_api()` 注册 `/astrbot_plugin_meme_magpie/*`，前端在 `pages/dashboard/`。

### 关键设计模式

- **插件实例即依赖容器**：`Main` 把 `self` 传给几乎所有服务，服务之间通过 `self.plugin.{service}` 互访。新增服务时沿用这一约定，不要引入 DI 框架。
- **异步锁**：`DatabaseService` 写操作走 `asyncio.Lock`；`ImageProcessorService` 用锁 + `_processing_hashes` 集合防止同一张图并发重复处理。
- **限流集中化**：所有批量 VLM 调用都应经过 `AnalysisThrottle.run()`，不要各自写 `asyncio.Semaphore`。
- **测试用 stub**：`tests/conftest.py` 注入假的 `astrbot.*` 模块，使测试无需真实框架。改 stub 名前先确认所有测试。

## 开发命令

所有命令都在**插件目录内**执行（即包含 `main.py` 的那一层）。

```bash
# 全部测试
python -m pytest tests -q

# 单个文件 / 单个用例
python -m pytest tests/test_database_service.py -q
python -m pytest tests/test_migration_service.py::test_apply_copies -v

# Lint：只把 F（真错误）和 E9（语法错误）当回归指标
python -m ruff check . --select F,E9

# 前端语法校验（无构建步骤，浏览器直接加载 ES module）
node --check pages/dashboard/app.js
```

`python -m ruff check .` 全量会报数百条继承自上游的风格问题（行长、引号风格等），**这些不是回归**。只有 `--select F,E9` 必须保持 clean。

`pages/dashboard/template.js` 导出的是一个 JS 模板字符串，`node --check` 需要按 module 校验：

```bash
# PowerShell
Get-Content pages/dashboard/template.js -Raw | node --input-type=module --check
```

### 本地运行

这是 AstrBot 插件，不是独立应用：

1. 安装 AstrBot。
2. 把本仓库克隆或软链到 AstrBot 的 `data/plugins/` 下，目录名必须是 `astrbot_plugin_meme_magpie`。
3. 重启 AstrBot 加载插件。
4. 在 AstrBot 里配置一个**支持视觉的**模型提供商，否则分类功能不可用。

### 依赖

`requirements.txt` 只声明必要的额外依赖（Pillow 等）。`aiohttp` / `pydantic` 等由 AstrBot 提供。不要随意引入重依赖：插件包体积需控制在 32MB 以内。

## 必须遵守的约定与陷阱

### `pages/dashboard/template.js`
整个文件是一个反引号包裹的 JS 模板字符串，**首尾恰好 2 个反引号**。因此：

- 文件内容里**绝对不能出现 `${`**（会被当成模板插值求值）。需要插值时用 Vue 的 `{{ }}`。
- 不能出现裸反引号。
- 改完必须跑上面的 module 校验。

### `_conf_schema.json`
- `type` 合法值只有：`string, text, int, float, bool, object, list, template_list, file`。写 `str` 会导致配置面板异常。
- 新增配置项必须同步三处：`_conf_schema.json`、`core/config/config.py` 的 Pydantic 字段、`.astrbot-plugin/i18n/*.json` 的 `config` 段。
- 当前共 51 个配置键。

### i18n
`.astrbot-plugin/i18n/{zh-CN,en-US,ru-RU}.json` 是嵌套 JSON：顶层 `metadata`（`display_name` / `short_desc` / `desc`）+ `config`（每个键 `{description, hint, labels}`）。枚举的 `options` 不翻译，翻的是 `labels`。缺失的键会回退到 `zh-CN`。

### LLM 工具（`@filter.llm_tool`）
AstrBot 用 `docstring_parser` 解析 docstring 生成函数调用 schema。因此：

- `Args:` 里每个参数**必须**写类型，格式 `参数名(string): 说明`。漏掉类型会让插件加载时抛 `ValueError` 直接崩。
- 支持的类型只有 `string / number / object / array / boolean`。本项目新增参数统一用 `string`，多值用逗号或顿号分隔，在 Python 侧再拆。
- schema 没有"必填"概念，所有参数在 Python 签名里都要有默认值。

### 数据库
- `SCHEMA_VERSION = 7`。改表结构要递增版本号并在 `_init_schema()` 后补一个 `_migrate_vN()`，且必须能从任意旧版本平滑升级。
- `emoji.path` 是主键，存的是**绝对路径**（`<data_dir>/categories/<分类>/<时间戳>_<hash8>.<扩展名>`）。
- v7 新增 `work`（作品名）列，`emoji` 与 `emoji_pending` 都有。新增标量列时记得同步 `_EMOJI_SCALAR_COLUMNS` / `_EMOJI_INSERT_COLUMNS` / `_PENDING_INSERT_COLUMNS` / `_hydrate_entry`。

### 前端与后端的契约
- `plugin_api.py` 在**插件根目录**（不在 `core/` 下），所有路由前缀由 `PLUGIN_NAME` 拼出。改路由必须同步改 `pages/dashboard/app.js`。
- 前端 `selectedImages` 是一个 **hash 的 Set**，不是 path。所有批量接口都传 `hashes`。

### 数据目录
- 插件数据：`data/plugin_data/astrbot_plugin_meme_magpie/`，其中 `categories/`（按分类存图）、`raw/`、`pending/`、`cache/emoji.db`。
- 插件配置：`data/config/astrbot_plugin_meme_magpie_config.json`（由 AstrBot 管理）。
- 迁移逻辑靠这两个路径定位上游数据，改路径派生规则时务必同步 `core/maintenance/migration_service.py`。

## 测试说明

- `tests/conftest.py` 在任何插件代码被导入前 patch `sys.modules`，注入假 `astrbot.*`。**在插件代码里新增 `astrbot.*` 导入时，要在 conftest 里补对应 stub**。
- 项目**没有** `pytest-asyncio`。测异步函数请用 `asyncio.run(...)`。
- 测试不得依赖真实 AstrBot 实例或外部网络。HTTP 与 VLM 调用一律 mock。
- `tests/test_plugin_api.py` 里有构造 `PluginAPI(SimpleNamespace(...))` 的范例，新增 Web API 测试照抄即可。
- 行尾统一 LF，由 `.gitattributes` 强制。提交前确认没有引入 CRLF。
