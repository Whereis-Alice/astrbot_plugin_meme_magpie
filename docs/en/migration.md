# Migrating from astrbot_plugin_stealer

> [← Back to README](../../README_EN.md) · [Docs index](README.md) · [中文](../migration.md)

## How the two plugins relate

This plugin is a fork of [nagatoquin33/astrbot_plugin_stealer](https://github.com/nagatoquin33/astrbot_plugin_stealer) (AGPL-3.0). Credit to the original author for the idea and the groundwork.

It is a **fully independent plugin**: package name, command prefix, data directory, WebUI routes and LLM tool names are all different, so it can live alongside the original in the same AstrBot without clobbering anything. If you are coming from stealer, one `/mp migrate apply` brings your stickers and database over, and your old data is left untouched by default.

What this fork adds on top:

| Change | Details |
|:---|:---|
| **Data migration command** | `/mp migrate` moves images, database rows, categories, character library, blocklist and config from the old plugin |
| **LLM can supply metadata directly** | When the chat model collects an image it can state "this is character X from series Y, doing Z" instead of leaving everything to the vision model |
| **Rate-limited batch analysis** | Batch import and batch re-analysis take a concurrency cap and a requests-per-minute cap, so hundreds of images will not trip upstream 429s |
| **Source work field** | A new `work` field flows through ingestion, retrieval and WebUI filtering |
| **Batch re-analysis** | Re-run vision analysis over images already in the library that are missing tags or descriptions, optionally fill-blanks-only |
| **Known facts feed the prompt** | Work and character values you already filled in are written into the analysis prompt, so the model adopts them instead of guessing — biggest win on anime art |
| **Re-analysis for the review queue** | Images waiting for review can be re-analysed in bulk as well, and there the category does get corrected |
| **Missing-description detector** | Lists every entry with an empty description in one click, so you can fill them in individually or re-run the whole batch |
| **Multi-adapter support** | LLBot / NapCat / SnowLuma differences around marketplace stickers and sticker flags are all absorbed |
| **Message-splitting plugin support** | A new `attach` delivery mode lets stickers ride along in the reply's own message chain |
| **Upstream bug fixes** | See the [CHANGELOG](../../CHANGELOG.md) |

## Migrating in three steps

`/mp migrate` brings `astrbot_plugin_stealer` data over. It is **idempotent** (running it twice does not duplicate anything) and **non-destructive** (files are copied by default; the old plugin's data stays exactly where it was), so it is safe to experiment with.

Three steps:

```
/mp migrate check     # 1. can it find the old plugin's data directory?
/mp migrate           # 2. dry run: print what *would* happen, write nothing at all
/mp migrate apply     # 3. do it (copy files, keep the originals)
```

Short on disk space? Use `/mp migrate move` instead of `apply` — it moves rather than copies, which **leaves the old plugin unusable**. Confirm with the first two steps before you do that.

If auto-detection fails, pass the path explicitly:

```
/mp migrate apply D:/astrbot/data/plugin_data/astrbot_plugin_stealer
```

**What moves**

- Every image under `categories/`, plus `pending/` review images and `raw/` staging images
- Database rows: category, tags, scenes, description, emotions, use count, scope, source, ingestion method
- Review-queue entries and the dedup blocklist
- `categories.json` / `category_info.json` / `characters.json` / `character_info.json`
- The old plugin's config values (only keys this plugin also has; new settings keep their defaults)

**What does not move**: thumbnail cache, temp files, semantic vectors. Vectors are topped up automatically on startup — you do not have to do anything. Only run `/mp rebuild_index` if the index itself looks broken (files are there but nothing shows up in search); it rebuilds the index only and **never deletes images**.

After migrating, check that "max stickers" is large enough. If you just brought in several hundred images while the cap is still a small number, everything above the cap counts as overflow. The default is `2000` and overflow only produces a log warning by default — see [Library capacity cap](configuration.md#library-capacity-cap).

You get a report at the end: how many images moved, how many were skipped (already present / file missing / duplicate hash), how many failed. Filename collisions are renamed, never overwritten.
