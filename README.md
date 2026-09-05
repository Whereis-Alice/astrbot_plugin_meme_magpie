<div align="center">

<img src="logo.png" alt="meme神偷" width="150">

# meme神偷 · Meme Thief

**让 AstrBot 顺手把群里刷过的表情包偷进图库，聊天时按情绪挑一张发出来。**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.24.1-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)

**语言 / Language**：[中文](README.md) · [English](README_EN.md)

</div>

---

## 这是什么

喜鹊自古是出名的贼鸟，看到亮闪闪的东西就往窝里叼。这个插件干的是同一件事——群里刷过的表情包，它顺手偷进图库，用视觉模型看懂「这张图在表达什么」，分类、打标签、抄下图上的字，然后在 Bot 回复时挑一张情绪对得上的发出去。

三件事各自独立开关，互不影响：

- **收**：监听聊天里的图片，按概率或冷却收进「待审核池」，你在 WebUI 点通过才正式入库。
- **认**：视觉模型（VLM）识别分类、标签、使用场景、图上文字、角色和作品。
- **发**：Bot 回复后按概率决定要不要配一张表情包；选哪张走语义检索 + 情绪匹配。

不想让它继续收了就 `/mp off`，已经进库的表情包照样能发。

对外的名字是 **meme神偷**（英文 Meme Thief），指令组是 `mp`——meme + pilfer，两个字母好敲，`magpie` 和 `神偷` 是完全等价的别名。仓库名和 Python 包名仍然是 `astrbot_plugin_meme_magpie`，这是**故意不改**的：AstrBot 拿它当插件目录名，同时也当数据目录名，改掉等于让老用户的表情包和数据库凭空失踪。

本插件基于 [nagatoquin33/astrbot_plugin_stealer](https://github.com/nagatoquin33/astrbot_plugin_stealer) 二次开发（AGPL-3.0），感谢原作者把这套玩法做出来。它是一个**完全独立的插件**：包名、指令、数据目录、WebUI 路由、LLM 工具名都换过了，可以和原插件装在同一个 AstrBot 里共存，不会互相覆盖数据。两者的关系、以及这一版多出来的东西见[迁移文档](docs/migration.md)。

## 核心功能

| 功能 | 说明 |
|:---|:---|
| **自动收集** | 监听聊天图片，概率 / 冷却两种模式，也能一句话进入强制收录 |
| **待审核池** | 自动收来的图先进审核区，人工点通过才入库，避免图库被随手截图污染 |
| **视觉识别** | VLM 输出结构化结果：分类、标签、使用场景、图上文字、情绪、角色、作品 |
| **语义检索** | 文本嵌入向量 + 图上文字 + 使用场景匹配，嵌入不可用时自动降级 BM25 |
| **情绪匹配** | 从 Bot 这条回复里提取情绪和检索词，挑一张情绪对得上的图 |
| **LLM 工具** | 对话中 LLM 可以自己检索、发送、收录表情包，并顺手写清作品与角色 |
| **WebUI 面板** | 审核区 + 图库双区，批量导入、批量重新识别、识别失败检测、重复图清理、存储维护 |
| **批量识别限流** | 并发数与每分钟请求上限可调，几百张图连续识别也不会打爆上游触发 429 |
| **外部表情包源** | 从压缩包 / GitHub 仓库 / JSON 接口整份导入别人做好的表情包，可增量同步 |
| **数据迁移** | 一条指令搬走 astrbot_plugin_stealer 的图片、数据库、分类、角色库、黑名单和配置 |
| **群聊过滤** | 收和发各有独立黑白名单，支持 `group:群号` 和 `user:QQ号` |
| **多协议端 / 多语言** | QQ 商城表情在 LLBot / NapCat / SnowLuma 上的差异全部兜住；界面支持中文 / English / Русский |

## 快速开始

### 1. 先配好视觉模型

插件靠 VLM 读懂图片，**没有视觉模型基本等于不能用**。两种配法任选其一：

- 在 AstrBot 后台配好「图片描述模型」，插件会自动拿来用；
- 或者在插件配置里单独指定「视觉模型」（`vision_provider_id`）。

嵌入模型（Embedding）是可选项：配了检索更准，没配会自动降级成 BM25 关键词检索，功能不受影响。

### 2. 安装

在 AstrBot 插件市场搜索 `astrbot_plugin_meme_magpie`，或者在插件管理页面用仓库地址安装：

```
https://github.com/Whereis-Alice/astrbot_plugin_meme_magpie
```

### 3. 三条命令跑起来

```
/mp on        # 开始收表情包
/mp auto_on   # 开启聊天时自动发表情包
/mp status    # 看看现在收了多少
```

> **关于 `/` 这个前缀**：它只是 AstrBot 的出厂默认唤醒前缀，你可以改成 `!`、`#`、`.`，也可以整个清空（清空后直接发 `mp status`）。本文档一律按默认的 `/` 书写，请自行替换成你实际用的那个；私聊或者 @机器人 的场合本来就不需要前缀。不确定的话发一次 `/mp help`，插件会按**你实际生效的前缀**打印完整清单。详见[指令列表](docs/commands.md)。

### 4. 打开管理面板

AstrBot 插件详情页 →「表情神偷管理面板」。不用额外开端口，不用另设密码。

## 常用指令

指令组是 `mp`，别名 `magpie` 和 `神偷`。下表只写子命令，实际发送时记得带上你的唤醒前缀，例如 `/mp status`。

| 指令 | 说明 |
|:---|:---|
| `status` | 运行状态和表情包统计 |
| `on` / `off` | 开启 / 关闭表情包收集 |
| `auto_on` / `auto_off` | 开启 / 关闭自动发送 |
| `偷` | 进入 30 秒强制收录模式，期间发的图片一律收下 |
| `list [分类] [每页数量] [页码]` | 列出已收集的表情包 |
| `delete <序号\|文件名>` | 删除表情包 |
| `blacklist <序号\|文件名>` | 删除并加入黑名单，之后不再重复收录 |
| `migrate [check\|apply\|move] [路径]` | 从 astrbot_plugin_stealer 迁移数据 |
| `help` / `帮助` | 按你当前的唤醒前缀打印完整子命令清单 |

黑白名单、作用域、容量清理、索引重建、标签体检等完整清单见[指令列表](docs/commands.md)。

## 从原插件迁移数据

`/mp migrate` 把 `astrbot_plugin_stealer` 的数据搬到本插件。整个过程是**幂等的**（重复执行不会重复导入）、**非破坏性的**（默认复制文件，旧插件数据原样保留），可以放心先试：

```
/mp migrate check     # 第一步：看看能不能找到旧插件的数据目录
/mp migrate           # 第二步：预演，只打印「如果执行会发生什么」，一个字节都不写
/mp migrate apply     # 第三步：真的搬（复制文件，旧数据保留）
```

搬什么、不搬什么、手动指定路径、磁盘紧张时改用 `move`，见[迁移文档](docs/migration.md)。

## 关键配置

配置项都在 AstrBot 插件配置页里改，最常动的是这几个：

| 配置项 | 默认 | 说明 |
|:---|:---|:---|
| 视觉模型 | 空 | 留空自动使用 AstrBot 全局图片描述模型 |
| 开启表情包偷取功能 | `false` | 收图总开关，等同于 `/mp on` |
| 偷图概率 | `0.3` | 概率模式下每张图被收下的概率 |
| 自动偷取需人工审核 | `true` | 关掉就直接入库，不经审核区 |
| 表情包发送概率 | `0.2` | Bot 回复后配一张表情包的概率 |
| **最大表情包数量** | `2000` | 图库硬上限，超出时会**永久删除**最旧的条目和图片文件；填 `0` 不限制 |
| 批量识别并发数 / 每分钟请求上限 | `2` / `20` | 批量识别的限流阀门，被 429 就往下调 |

> ⚠️ **「最大表情包数量」是插件里唯一会主动删你数据的机制**，位置在配置页最顶部「容量与安全」分组的第一项。默认只告警不删，但把它调小之前请先读[表情库容量上限](docs/configuration.md#表情库容量上限)。

其余配置项（发图延迟、情绪识别模式、检索预设、存储清理策略、黑白名单等）见[配置说明](docs/configuration.md)。

## WebUI 管理面板

插件详情页点「表情神偷管理面板」进入，不用额外开端口。分两个区：**审核区**逐张或批量通过 / 删除，通过前能直接改分类、标签、描述、角色、作品；**图库**按分类 / 作品 / 关键词筛选，四种排序，支持批量改分类、批量删除、批量设作用域和角色 / 作品。另有单张上传、批量导入、批量重新识别、识别失败检测、外部表情包源导入、重复图清理、存储维护和分类管理，三套主题、三种语言。

> **注意**：WebUI 里删除一个分类会同时删掉该分类下所有图片文件，操作前想清楚。

详见[WebUI 管理面板](docs/webui.md)。

## 完整文档

| 文档 | 内容 |
|:---|:---|
| [指令列表](docs/commands.md) | 全部子命令，以及唤醒前缀到底该写什么 |
| [配置说明](docs/configuration.md) | 按分组列出关键配置项，含会真的删图的表情库容量上限 |
| [WebUI 管理面板](docs/webui.md) | 审核区与图库、批量导入、批量重新识别、限流与进度显示 |
| [外部表情包源](docs/external-sources.md) | 整份导入别人做好的表情包，增量同步与安全边界 |
| [LLM 主动用图](docs/llm-tools.md) | 三个 LLM 工具、让模型自己填作品 / 角色、已知信息如何进提示词 |
| [从原插件迁移数据](docs/migration.md) | 两个插件的关系、迁移三步走、搬什么与不搬什么 |
| [平台与协议端支持](docs/platforms.md) | LLBot / NapCat / SnowLuma 商城表情差异、和分段回复插件配合 |
| [常见问题](docs/faq.md) | FAQ |
| [CHANGELOG](CHANGELOG.md) | 每个版本改了什么 |

## 数据存放在哪

```
data/plugin_data/astrbot_plugin_meme_magpie/
├── categories/<分类名>/     # 正式入库的表情包
├── pending/                 # 待审核图片
├── raw/                     # 原图暂存
├── cache/emoji.db           # SQLite 数据库（记录、标签、向量、黑名单）
├── cache/                   # 缩略图缓存
├── temp/                    # 临时文件
├── plugin_stealer_split_compat/  # 仅在开启「兼容路径」后出现，见 docs/platforms.md
├── categories.json          # 分类列表
├── category_info.json       # 分类说明
├── characters.json          # 角色列表
└── character_info.json      # 角色说明
```

备份的话把整个目录打包就行。

## 注意事项

- **必须配置视觉模型**，否则识别相关的功能全都不工作。
- 「最大表情包数量」是会真的删文件的硬上限，调小之前请先看[表情库容量上限](docs/configuration.md#表情库容量上限)。
- WebUI 删除分类会连带删除该分类下的所有图片文件。
- 开启「以 GIF 格式发送」时，每次发图都要把动图重新编码一遍，峰值内存虽有预算封顶，仍高于直接发原图，内存紧张的机器建议关闭。
- 表情包内容和版权归原作者所有，请遵守所在平台的规则，不要用于传播违规内容。

## 许可证与致谢

本项目以 **AGPL-3.0** 许可证开源，详见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。

> 上游仓库的 README 写的是 MIT，但仓库里实际放的 `LICENSE` 文件是 AGPL-3.0（GitHub 识别结果也是 AGPL-3.0）。两者冲突时，作为下游项目只能按**实际存在、且更严格**的 AGPL-3.0 履行义务，所以本项目也是 AGPL-3.0。这意味着你可以自由使用、修改和分发，但分发修改版（包括以网络服务形式提供）时需一并提供源码。

基于 [nagatoquin33/astrbot_plugin_stealer](https://github.com/nagatoquin33/astrbot_plugin_stealer) 二次开发，原始版权归 nagatoquin33 所有；本次修改部分版权归 Whereis-Alice 所有。原插件的玩法灵感来自 maibot 的表情包偷取和 meme_manager 的标签注入思路。

---

<div align="center">

觉得好用就点个 ⭐ Star。

有问题欢迎提 [Issue](https://github.com/Whereis-Alice/astrbot_plugin_meme_magpie/issues)。

</div>
