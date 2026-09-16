# 跨插件联动接口

`meme神偷` 是表情包的选择器和元数据来源。论坛、图床等插件可以通过当前
AstrBot 事件拿到已加载的插件实例，再按“搜索候选 → 导出句柄 → 上传”的流程
使用资源；调用方不需要读取本地目录，也不需要重新做图片分类。

```python
meta = self.context.get_registered_star("astrbot_plugin_meme_magpie")
meme = getattr(meta, "star_cls", None) if meta else None
if meme is None:
    raise RuntimeError("meme_magpie 未加载")

candidates = await meme.search_meme_candidates(
    event,
    "后藤一里 开心",
    filters={"work": "孤独摇滚", "tag": "吉他"},
    limit=5,
)
if not candidates:
    # 上层可以改用纯文字或请求确认
    return

asset = await meme.export_meme_asset(candidates[0]["emoji_id"], event)
try:
    # 交给 imgbed_ferry.upload_asset(event, asset, ...)
    ...
finally:
    asset.release()
```

`search_meme_candidates()` 复用现有 `MemeSelector.smart_search`，返回候选编号和
作品、角色、场景、情绪、标签、作用域等元数据。返回值不包含本地路径；候选只绑定
当前事件，默认 10 分钟后失效。匹配不足时返回空列表。

`export_meme_asset(emoji_id, event)` 只接受当前候选列表里的编号（例如 `emoji_1`
或 `1`）。它返回短期资源句柄，句柄提供：

- `read_bytes()` / `read_bytes_sync()`：读取经格式、大小和 SHA-256 校验的图片内容；
- `mime_type`、`filename`、`size`、`sha256`：上传所需的内容信息；
- `metadata`：作品、角色、动作、场景、情绪、标签和作用域等已知字段；
- `release()`：使用完成后立即释放租约。

资源句柄的默认 TTL 为 120 秒，插件关闭时全部失效。路径必须位于插件数据目录内，
拒绝符号链接、缺失文件、超大文件和不支持的图片格式。导出时会再次检查当前会话
的发送开关和 `local` 作用域权限。

如果调用方希望用错误码处理失败，可以调用 `try_export_meme_asset()`，成功时返回
`{"success": True, "asset": handle, ...}`，失败时返回带 `error` 和 `message` 的字典。
也可以通过 `get_meme_integration_api()` / `integration_api` 获取同一套门面；其中
`export_asset()`、`search_candidates()` 是不带业务前缀的别名。

## 与图床联动

`astrbot_plugin_imgbed_ferry` 提供 `upload_asset(event, asset, ...)`。调用方应把
`MemeAssetHandle` 直接传给图床，并在上传结束后释放句柄：

```python
ferry_meta = self.context.get_registered_star("astrbot_plugin_imgbed_ferry")
ferry = getattr(ferry_meta, "star_cls", None) if ferry_meta else None
result = await ferry.upload_asset(
    event,
    asset,
    folder="astrbook/memes",
    compress=True,
    output_format="markdown",
)
if not result["success"]:
    raise RuntimeError(result["code"])
markdown = result["markdown"]
```

`meme神偷` 不负责论坛发帖和图床上传；上层插件决定是否使用公网 URL、Markdown
图片或回退到纯文字。
