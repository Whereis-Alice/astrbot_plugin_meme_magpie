# Cross-plugin integration

`Meme Thief` owns meme selection and metadata. Forum, image-hosting, and other
plugins can use the current AstrBot event to find the loaded plugin and follow
the sequence “search candidates → export a handle → upload”. Consumers never
need to inspect the local library or classify images again.

```python
meta = self.context.get_registered_star("astrbot_plugin_meme_magpie")
meme = getattr(meta, "star_cls", None) if meta else None
if meme is None:
    raise RuntimeError("meme_magpie is not loaded")

candidates = await meme.search_meme_candidates(
    event,
    "Hitori Gotoh happy",
    filters={"work": "Bocchi the Rock!", "tag": "guitar"},
    limit=5,
)
if not candidates:
    return

asset = await meme.export_meme_asset(candidates[0]["emoji_id"], event)
try:
    # Pass the handle to imgbed_ferry.upload_asset(event, asset, ...)
    ...
finally:
    asset.release()
```

`search_meme_candidates()` reuses the existing `MemeSelector.smart_search` and
returns an ID plus known work, character, scene, emotion, tags, and scope
metadata. It never returns a local path. Candidates belong to the current event
and expire after ten minutes by default. A weak match returns an empty list.

`export_meme_asset(emoji_id, event)` accepts only an ID from the current
candidate list, such as `emoji_1` or `1`. It returns a short-lived handle with:

- `read_bytes()` / `read_bytes_sync()` for validated image content;
- `mime_type`, `filename`, `size`, and `sha256` for upload metadata;
- `metadata` containing the known work, character, action, scenes, emotions,
  tags, and scope;
- `release()` to end the lease after use.

The default handle TTL is 120 seconds. Paths must stay inside the plugin data
directory; symlinks, missing files, oversized files, and unsupported formats are
rejected. Export repeats the send-toggle and `local` scope checks for the
current event.

Use `try_export_meme_asset()` when a consumer wants stable error dictionaries
instead of exceptions. `get_meme_integration_api()` and `integration_api` expose
the same facade; `search_candidates()` and `export_asset()` are generic aliases.

## With imgbed_ferry

`astrbot_plugin_imgbed_ferry` exposes `upload_asset(event, asset, ...)`. Pass the
`MemeAssetHandle` directly and release it when the upload completes:

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

`Meme Thief` does not publish forum posts or upload files. The consumer decides
whether to use a public URL, Markdown, or a text-only fallback.
