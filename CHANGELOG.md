# 更新日志

本文件记录「表情包喜鹊」（`astrbot_plugin_meme_magpie`）的版本变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [1.0.0] - 2026-09-03

首个发布版本。基于 [nagatoquin33/astrbot_plugin_stealer](https://github.com/nagatoquin33/astrbot_plugin_stealer) `v2.8.9`（`f5f5183`）分叉，改名为独立插件并做了下列改动。

### 改名与隔离

原插件的功能保留，但所有对外标识符都换掉了，两个插件可以在同一个 AstrBot 里共存、互不干扰：

| | 原插件 | 本插件 |
|:---|:---|:---|
| 包名 | `astrbot_plugin_stealer` | `astrbot_plugin_meme_magpie` |
| 显示名 | 表情包小偷 | 表情包喜鹊 |
| 命令前缀 | `/meme` | `/magpie`（别名 `/喜鹊`） |
| 数据目录 | `data/plugin_data/astrbot_plugin_stealer/` | `data/plugin_data/astrbot_plugin_meme_magpie/` |
| WebUI 路由 | `/astrbot_plugin_stealer/*` | `/astrbot_plugin_meme_magpie/*` |
| LLM 工具 | `search_meme` / `send_meme` / `steal_meme` | `magpie_search_meme` / `magpie_send_meme` / `magpie_steal_meme` |

配置项的键名**没有**改动，所以迁移时旧配置可以直接套用。

### 新增

**数据迁移**

- `/magpie migrate [check|apply|move] [路径]`：从 `astrbot_plugin_stealer` 迁移全部数据。
  - 不带参数 = 预演（dry-run），只报告将要迁移的内容，不写入任何数据
  - `check` = 只检测能否找到旧插件的数据目录
  - `apply` = 复制文件迁移，旧数据完整保留
  - `move` = 移动文件迁移（不可逆，旧插件将不可用）
- 迁移覆盖：分类图片、待审核图片、原图暂存、SQLite 记录（含标签 / 场景 / 描述 / 情绪 / 使用次数 / 作用域 / 来源 / 入库方式）、待审核池、去重黑名单、`categories.json`、`category_info.json`、`characters.json`、`character_info.json`、旧插件配置项。
- 迁移是幂等的：重复执行不会重复导入；文件名冲突自动改名，不覆盖已有文件；哈希重复自动跳过。
- 迁移结束输出统计报告（成功 / 跳过 / 失败 / 错误明细，错误列表上限 50 条）。
- 插件启动时若检测到旧插件数据目录存在且本插件图库为空，日志中给出一次迁移提示。
- 自动探测的旧目录名包含 `astrbot_plugin_stealer`、`astrbot_plugin_emoji_stealer`、`astrbot_plugin_meme_stealer`。

**LLM 主动收图时直接传参入库**

- `magpie_steal_meme` 新增 9 个参数：`work`（作品名）、`character`（角色名）、`action`（动作）、`desc`（描述）、`tags`、`scenes`、`emotions`、`overlay_text`（图上文字）、`category`（分类）。多值参数用逗号或顿号分隔。
- 参数可以全填、部分填或不填；未提供的字段仍交给视觉模型补齐。
- 新增配置项「LLM 偷图参数采纳策略」（`llm_steal_param_mode`）：
  - `merge`（默认）— LLM 给的字段优先，缺的用 VLM 结果补
  - `llm_first` — LLM 给齐关键字段时跳过 VLM 调用，省一次请求
  - `vlm_only` — 忽略 LLM 传参，行为与原插件一致
- `category` 参数经过分类白名单校验，非法值忽略并回退到 VLM 判定。
- **内容审核始终在最前执行**，LLM 无法通过传参绕过。
- 工具返回结果会明确回显「哪些字段由 LLM 提供、哪些由视觉模型补齐」，便于调试提示词。
- 新增 `core/processing/llm_meme_hints.py` 负责参数清洗、派生（如从 `action` 派生标签）、合并与冲突消解。

**批量识别限流器**

- 新增 `core/processing/analysis_throttle.py`：并发上限 + RPM 令牌桶 + 指数退避重试。
  - 区分「可重试」（429 / 408 / 5xx / 超时 / 连接重置等）与「重试无意义」（400 / 401 / 403 / 422、额度耗尽、Key 无效、鉴权失败等）两类错误，后者立即失败不浪费额度
  - 优先采纳上游返回的 `Retry-After`（上限 120 秒）
  - 被限流时把整条队列的发车时刻一起推后，避免多个 worker 前后脚继续撞墙
  - 退避带 ±25% 随机抖动，避免同步重试风暴
- 新增 4 个配置项：`batch_analyze_concurrency`（1~16，默认 2）、`batch_analyze_rpm`（0~600，默认 20，0 = 不限速）、`batch_analyze_max_retries`（0~8，默认 3）、`batch_analyze_retry_backoff`（1.0~10.0，默认 2.0）。
- WebUI 批量任务弹窗可现场调整并发和速率（仅对当次任务生效，不改配置），并有「恢复默认」按钮。

**批量任务进度显示与控制**

- 批量导入 / 批量重新识别改为后台任务 + 前端轮询状态，新增接口 `GET /images/batch-upload-status`。
- 进度面板显示：进度条与已处理数、当前文件名与阶段（识别中 / 入库中）、成功 / 失败 / 已识别 / 限流次数 / 重试次数、按实际速率估算的剩余时间、失败明细。
- 新增 `POST /images/batch-upload-control`，支持**暂停 / 继续 / 取消**（暂停会真正停住 worker）。
- 关闭页面不影响后台任务，重新打开面板自动接回进度。
- 任务结果最多保留 2000 条，任务记录有 TTL 自动回收。

**批量重新识别（新功能）**

- 新增 `POST /images/batch-reanalyze` 与 `GET /images/reanalyze-scan`。
- 对已入库图片重跑视觉识别，范围可选：勾选的图片 / 缺标签或缺描述的图片 / 全部图片。
- 「覆盖已有内容」默认关闭 —— 只填空字段，不动手写过的描述和标签。
- 支持数量上限（最多 5000 条），便于小范围试水。
- 出于并发安全考虑**不自动改分类**（改分类需移动文件），只在结果里给出「建议分类」由人决定。
- 结果逐项列出改动了哪些字段。

**作品（work）维度**

- 数据库 schema 升级到 v7：`emoji` 与 `emoji_pending` 表新增 `work` 列并建索引，自动迁移旧库。
- 作品名贯通：入库（含 LLM 传参与 WebUI 上传）→ 检索（BM25 文本特征、评分、语义召回）→ WebUI（筛选、编辑、批量设置、自动补全）。
- 检索评分中作品命中权重排在图上文字（22）与角色名（20）之后、描述精确匹配（15）之前，取 18。
- LLM 检索工具返回的候选列表会带上作品名，方便模型判断。
- WebUI 新增「批量设置作品」，以及基于已有作品名的输入自动补全。

**其他新增配置项**

- `image_processing_cooldown`（偷图冷却时间，秒，默认 30）—— 冷却模式此前是硬编码值，现在可配。
- `content_filtration_fail_open`（审核异常时放行，默认 `false`）—— 内容审核调用本身失败时的行为，默认拒收更安全。
- `auto_meme_intent_gate`（启用发送意图判断，默认 `true`）—— 先判断这句话适不适合配表情包，减少答非所问的乱发。
- `meme_send_delay_random` / `meme_send_delay_max`（随机延迟，默认关 / 8.0 秒）—— 发送延迟不再是固定值，更像真人。
- `meme_send_char_delay`（每字追加延迟，默认 0.3 秒）—— 按回复字数追加等待，模拟「先看完文字再发表情」。
- `storage_cleanup_strategy`（存储清理策略，默认 `balanced`）—— `conservative` 只清失效索引与临时文件 / `balanced` 再清孤立文件与缩略图 / `aggressive` 连 `raw` 原图备份一起清。

**视觉与品牌**

- 新增插件 Logo（`logo.png`，AstrBot 插件列表会自动读取插件根目录的 `logo.png`）与吉祥物插画，附矢量源文件 `assets/logo.svg`、`assets/mascot.svg`。
- 移除原插件中的第三方 IP 元素，主题改名并重做：`fallout` → `terminal`（终端风）、`minecraft` → `pixel`（像素风）；相关 CSS 变量与类名前缀一并改为 `--crt-*` / `--px-*`。

**文档与国际化**

- README / README_EN 面向公众重写：从「这是什么」讲起，含迁移三步走、限流参数建议表、配置说明、FAQ 与已修问题清单。
- i18n 三语（zh-CN / en-US / ru-RU）补齐全部 51 个配置项的描述、提示与选项标签，以及批量重新识别相关文案。
- 新增 `NOTICE`，说明衍生关系、版权归属与许可证冲突的处理方式。

### 修复

以下是原插件中发现并修掉的问题：

- **LLM 收图的入库方式记录错误**：通过 LLM 工具收录的图片，`add_method` 被写成 `auto`（自动偷取），与自动收图混在一个统计口径里。现在正确记为 `llm`。
- **批量识别失败时留下脏数据**：视觉模型返回非法分类时，原实现只把分类回退到兜底值，却保留了同一次分析产出的标签和描述，导致「分类是兜底的、标签却是乱的」。现在整份分析结果一并丢弃。
- **WebUI 搜索漏字段**：索引兜底分支只检索标签 / 描述 / 场景，搜不到图上文字、角色、作品、原始文件名、来源。现在与 SQL 分支的字段范围对齐。
- **批量导入无限并发**：原实现对上传的所有图片同时发起视觉分析（原 README 自带「高并发警告，请分批次分析」）。现在换成可配置并发 + RPM 限速 + 退避重试，不需要人肉分批。
- **内容审核失败时静默放行**：审核模型超时或额度耗尽时的行为不可控。现在默认拒收，并提供显式的放行开关。
- **死代码**：删除不再被任何调用点引用的 `steal_image_direct`。

### 变更

- 版本号从上游的 `2.8.9` 重置为 `1.0.0`（本仓库的第一个版本）。
- 许可证明确为 **AGPL-3.0**。上游 README 声称 MIT，但其仓库实际提交的 `LICENSE` 是 AGPL-3.0，GitHub 的识别结果同样是 AGPL-3.0；两处冲突时本项目按实际存在且更严格的 AGPL-3.0 履行义务。原作者署名与版权声明保留在 `LICENSE` 与 `NOTICE` 中。
- 原 README 中「高并发」的描述改为「可配置并发 + 限流」，与实际行为一致。
- `CLAUDE.md` 改名为 `AGENTS.md`，内容更新为本插件的架构说明。
- 行尾统一为 LF，新增 `.gitattributes` 固定文本文件的行尾规范化行为。

### 测试

- 新增单元测试覆盖：限流器（限速间隔、并发上限、致命错误不重试、`Retry-After` 优先、计数快照）、LLM 传参解析与合并、迁移服务（幂等性、冲突改名、配置搬运、报告统计）、批量任务与重新识别的目标收集与字段合并、作品维度的检索评分。
- 全量测试与 `ruff check --select F,E9` 在 CI 与本地均通过。

---

## 早于 1.0.0

本插件从 `astrbot_plugin_stealer` `v2.8.9` 分叉。分叉点之前的历史请参见
[上游仓库的更新日志](https://github.com/nagatoquin33/astrbot_plugin_stealer/blob/master/CHANGELOG.md)。
