<div align="center">

<img src="logo.png" alt="Meme Magpie" width="150">

# Meme Magpie · 表情包喜鹊

**Lets AstrBot squirrel away the stickers people post in chat, then pick a mood-appropriate one when replying.**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.24.1-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)

**Language / 语言**: [English](README_EN.md) · [中文](README.md)

</div>

---

## What is this

Magpies collect shiny things. So does this plugin: stickers that scroll past in chat get filed away, a vision model works out what each one is expressing, tags it, transcribes any text printed on the image, and later the bot picks one that matches the mood of its reply.

Three independent switches, none of them coupled:

- **Collect** — watch chat images and store them in a review queue, gated by probability or cooldown. Nothing enters the library until you approve it in the WebUI.
- **Understand** — a vision model (VLM) fills in category, tags, usage scenes, on-image text, character and source work.
- **Send** — after the bot replies, roll the dice on whether to attach a sticker; which one gets picked comes from semantic search plus emotion matching.

Done collecting? `/magpie off`. Everything already in the library still works.

### Relationship to astrbot_plugin_stealer

This plugin is a fork of [nagatoquin33/astrbot_plugin_stealer](https://github.com/nagatoquin33/astrbot_plugin_stealer) (AGPL-3.0). Credit to the original author for the idea and the groundwork.

It is a **fully independent plugin**: package name, command prefix, data directory, WebUI routes and LLM tool names are all different, so it can live alongside the original in the same AstrBot without clobbering anything. If you are coming from stealer, one `/magpie migrate apply` brings your stickers and database over, and your old data is left untouched by default.

What this fork adds on top:

| Change | Details |
|:---|:---|
| **Data migration command** | `/magpie migrate` moves images, database rows, categories, character library, blocklist and config from the old plugin |
| **LLM can supply metadata directly** | When the chat model collects an image it can state "this is character X from series Y, doing Z" instead of leaving everything to the vision model |
| **Rate-limited batch analysis** | Batch import and batch re-analysis take a concurrency cap and a requests-per-minute cap, so hundreds of images will not trip upstream 429s |
| **Source work field** | A new `work` field flows through ingestion, retrieval and WebUI filtering |
| **Batch re-analysis** | Re-run vision analysis over images already in the library that are missing tags or descriptions, optionally fill-blanks-only |
| **Upstream bug fixes** | See [What got fixed](#what-got-fixed) |

## Features

| Feature | Details |
|:---|:---|
| **Automatic collection** | Watches chat images; probability or cooldown mode; dedicated sticker-detection strategy for the QQ official bot platform |
| **Review queue** | Auto-collected images wait for human approval, so random screenshots do not pollute the library |
| **Vision analysis** | VLM returns structured JSON: category, 0–3 tags, 1–2 usage scenes, on-image text, emotions |
| **Semantic search** | On-image text + usage scenes + text embeddings; category only adds score, never hard-filters; falls back to BM25 when embeddings are unavailable |
| **Emotion matching** | Extracts search terms and an emotion prior from the bot's reply. The reply text itself is never modified |
| **LLM-driven usage** | During conversation the model can search, send and collect stickers on its own |
| **Character & work library** | Manually file "who is this / what is it from", independent of emotion categories |
| **WebUI dashboard** | Review queue + library: browse, edit, sort, bulk actions, batch import, batch re-analysis, storage cleanup |
| **Chat filtering** | Separate allow/block lists for collecting and sending, with `group:<id>` and `user:<id>` entries |
| **Duplicate cleanup** | Perceptual hashing (pHash) finds visually identical images |
| **Localized UI** | 中文 / English / Русский |

## Getting started

### 1. Configure a vision model first

The plugin needs a VLM to understand images; **without one it is essentially non-functional**. Either:

- configure an image-captioning model globally in AstrBot and the plugin will use it, or
- point the plugin's own "vision model" setting (`vision_provider_id`) at a provider.

An embedding model is optional: it makes retrieval sharper, and without one the plugin silently falls back to BM25 keyword search.

### 2. Install

Search for `astrbot_plugin_meme_magpie` in the AstrBot plugin marketplace, or install from the repository URL:

```
https://github.com/Whereis-Alice/astrbot_plugin_meme_magpie
```

### 3. Three commands to get going

```
/magpie on        # start collecting
/magpie auto_on   # start attaching stickers to replies
/magpie status    # see how many you have
```

The command prefix is `/magpie`; the Chinese alias `/喜鹊` also works.

### 4. Open the dashboard

AstrBot plugin details page → "Sticker Manager". No extra port, no extra password.

## Migrating from the original plugin

`/magpie migrate` brings `astrbot_plugin_stealer` data over. It is **idempotent** (running it twice does not duplicate anything) and **non-destructive** (files are copied by default; the old plugin's data stays exactly where it was), so it is safe to experiment with.

Three steps:

```
/magpie migrate check     # 1. can it find the old plugin's data directory?
/magpie migrate           # 2. dry run: report what would move, write nothing
/magpie migrate apply     # 3. do it (copy files, keep the originals)
```

Short on disk space? Use `/magpie migrate move` instead of `apply` — it moves rather than copies, which **leaves the old plugin unusable**. Confirm with the first two steps before you do that.

If auto-detection fails, pass the path explicitly:

```
/magpie migrate apply D:/astrbot/data/plugin_data/astrbot_plugin_stealer
```

**What moves**

- Every image under `categories/`, plus `pending/` review images and `raw/` staging images
- Database rows: category, tags, scenes, description, emotions, use count, scope, source, ingestion method
- Review-queue entries and the dedup blocklist
- `categories.json` / `category_info.json` / `characters.json` / `character_info.json`
- The old plugin's config values (only keys this plugin also has; new settings keep their defaults)

**What does not move**: thumbnail cache, temp files, embedding vectors. Run `/magpie rebuild_index` once after migrating.

You get a report at the end: how many images moved, how many were skipped (already present / file missing / duplicate hash), how many failed. Filename collisions are renamed, never overwritten.

## LLM-driven sticker usage

Three tools the model can call on its own, no command needed:

| Tool | Purpose |
|:---|:---|
| `magpie_search_meme` | Search for candidates and return category, work, character, scenes, scope, use count |
| `magpie_send_meme` | Send one of the candidates; failures come back with an explicit reason so the model can retry |
| `magpie_steal_meme` | Collect an image from the current message; leave `image_ref` empty to take the first image |

### Letting the model fill in the metadata

Vision models are bad at "this is Hitori Gotoh from Bocchi the Rock clutching her head" — that kind of fandom detail is where sticker plugins usually fall over. So `magpie_steal_meme` exposes parameters that let **the conversational LLM write what it already knows straight into the database**:

| Parameter | Meaning |
|:---|:---|
| `work` | Source work, e.g. "Bocchi the Rock" |
| `character` | Character name, e.g. "Hitori Gotoh" |
| `action` | What is happening in the image, e.g. "clutching head" |
| `desc` | One-line description |
| `tags` | Keywords, comma-separated |
| `scenes` | Situations it fits, comma-separated |
| `emotions` | Emotion words, comma-separated |
| `overlay_text` | Text printed on the image |
| `category` | Explicit category (must be an existing one; invalid values are ignored and fall back) |

All of them, some of them, or none — whatever the model omits is left to the vision pass. In practice you say something like:

> Grab that one, it's Bocchi from Bocchi the Rock screaming into her hands

and the model calls the tool with `work`, `character` and `action` filled in, which is a step change in library quality over guessing from pixels.

Which side wins is controlled by **`llm_steal_param_mode`**:

| Value | Behaviour | Use when |
|:---|:---|:---|
| `merge` (default) | LLM-supplied fields win; anything missing is filled by the vision pass | Almost always |
| `llm_first` | If the LLM supplied the key fields, **skip the vision call entirely** | Saving tokens, and you trust the chat model |
| `vlm_only` | Ignore LLM parameters, use vision analysis only | You want the vision model to decide everything |

Content moderation always runs first regardless of mode — **the LLM cannot bypass it**.

## Batch import and batch re-analysis

Both live in the WebUI library page and share the same rate limiter.

### Batch import

Upload dozens or hundreds of images at once. Two mutually exclusive modes:

- **Pick a category** — files land in that category, no vision calls, fastest path.
- **Auto-analyse** — leave the category empty and the VLM classifies and tags each image.

You can also set a character and a source work for the whole batch up front.

### Batch re-analysis

Re-runs vision analysis over images already in the library. Useful when early imports have no tags, or when you have switched to a better vision model and want everything re-labelled.

- **Scope**: selected images / images missing tags or descriptions / everything
- **Overwrite existing values**: off by default, so it only fills blanks and never touches descriptions you wrote by hand
- **Item limit**: cap the batch size and test the waters first
- Re-analysis deliberately **does not change categories** — that would mean moving files, which is risky under concurrency. It reports a *suggested* category and leaves the decision to you.

### Picking concurrency and rate so you do not get 429ed

The limiter does three things: caps **concurrency** (requests in flight), enforces an **RPM token bucket** (average requests per minute), and does **backoff retries** — on 429 / 5xx / timeout it waits and retries, and pushes the whole queue back so the other workers do not immediately hit the same wall.

The concurrency and rate fields in the dialog are **per-task overrides**; they do not change your plugin config. The config values are just what the dialog pre-fills.

Match them to your upstream quota:

| Situation | Concurrency | RPM |
|:---|:---:|:---:|
| Free / trial tier, 429s easily | 1 | 6–10 |
| Typical paid API (OpenAI, Qwen, Zhipu entry tiers) | 2–3 | 20–60 |
| High quota or self-hosted inference | 4–8 | 120–300 |
| No limit wanted (local model) | 8–16 | 0 (0 = unlimited, concurrency still applies) |

When in doubt, keep the default 2 / 20: a few hundred images finish in about an hour and almost never trip a limit. If you do get throttled, the "rate limited" and "retried" counters on the progress panel start climbing — that is your cue to dial down.

Two more settings live in the plugin config: `batch_analyze_max_retries` (default 3) and `batch_analyze_retry_backoff` (default 2.0, i.e. roughly 1s → 2s → 4s with jitter). A `Retry-After` header from upstream takes priority over the computed backoff. Errors that can never succeed on retry — exhausted quota, invalid key, auth failure — fail immediately instead of burning credit.

### Progress display

Once a task is running the panel shows:

- Progress bar and processed/total counts; the bar changes colour while paused
- Current filename and phase (analysing / storing)
- Success / failed / analysed / rate-limited / retried counters
- Estimated time remaining, derived from the observed throughput
- Per-failure detail: which image, and why
- Re-analysis tasks additionally report how many fields changed and which images have a suggested category change

Tasks can be **paused, resumed and cancelled** at any time; pausing genuinely stops the workers rather than just the UI. Closing the tab does not stop the task — reopen the panel and the progress reattaches.

## Commands

Prefix `/magpie`, Chinese alias `/喜鹊`.

### Available to everyone

| Command | Description |
|:---|:---|
| `status` | Runtime status and library statistics |
| `list [category] [per_page] [page]` | List collected stickers (10 per page, pages start at 1) |
| `emotion_stats` | Emotion-analysis statistics and current mode |

### Admin only

| Command | Description |
|:---|:---|
| `on` / `off` | Enable / disable collection |
| `auto_on` / `auto_off` | Enable / disable automatic sending |
| `偷` | 30-second force-collect mode; every image posted gets taken |
| `natural_analysis <on\|off>` | Switch between the two emotion-analysis modes |
| `clear_emotion_cache` | Clear the emotion-analysis cache |
| `delete <index\|filename>` | Delete a sticker |
| `blacklist <index\|filename>` | Delete and blocklist it so it is never collected again |
| `scope <index\|filename> <public\|local>` | Set scope; `local` restricts sending to the source chat |
| `clean [force]` | Clean up unclassified staging images |
| `capacity` | Run a capacity-control pass now |
| `tag_stats [N]` | Tag health check: frequent tags, noisy rare tags, untagged entries (N defaults to 15) |
| `rebuild_index` | Rebuild the retrieval index |
| `migrate [check\|apply\|move] [path]` | Migrate data from astrbot_plugin_stealer |
| `group show` | Show the collect/send list configuration |
| `group <send\|steal> priority <wl\|bl>` | Which list wins when both match |
| `group <send\|steal> <wl\|bl> <add\|del\|clear> [group:<id>\|user:<id>]` | Manage the lists |

## Configuration

Everything is editable in the AstrBot plugin config page. The notable ones:

### Collection

| Setting | Default | Description |
|:---|:---|:---|
| Enable collection | `false` | Master switch |
| Collection mode | `probability` | `probability` or `cooldown` |
| Collection chance | `0.3` | Probability each image is taken |
| Cooldown seconds | `30` | Minimum gap between collections in cooldown mode |
| Content moderation | `false` | Filters inappropriate images; costs one extra model call |
| Fail open on moderation error | `false` | If the moderation call itself fails: reject (default, safer) or allow |
| Review queue capacity | `200` | Collection pauses when the queue is full and resumes as you approve |
| Require review for auto-collected images | `true` | Turn off to bypass the review queue |
| Sticker detection (QQ Official) | `cdn_only` | QQ official images carry no OneBot sticker flag: `all_images` / `cdn_only` / `gif_only` |
| LLM parameter policy | `merge` | See [Letting the model fill in the metadata](#letting-the-model-fill-in-the-metadata) |

### Sending

| Setting | Default | Description |
|:---|:---|:---|
| Auto-send stickers | `true` | Master switch |
| Intent gate | `true` | Checks whether the reply is a good fit for a sticker at all; cuts down on non-sequitur sends |
| Cancel pending sticker on new message | `true` | A new message in the same chat cancels a sticker still waiting out its delay |
| Send probability | `0.2` | 0.0 – 1.0 |
| Send as GIF | `false` | Feels more like a real sticker, but forcing very large images to GIF spikes memory |
| Fixed delay (s) | `5.0` | Keeps out of the way of message-splitting plugins; 0 sends immediately |
| Randomize delay | `false` | Pick a random delay between the fixed and maximum values |
| Per-character delay (s) | `0.3` | Extra wait scaled by reply length, simulating "read the text, then react" |
| Smart selection | `true` | Composite scoring; off means purely random |

### Emotion analysis

| Setting | Default | Description |
|:---|:---|:---|
| Extract search terms | `true` | `true` = LLM mode (recommended): a light model pulls search terms and an emotion prior out of the reply. `false` = passive mode: search with the raw reply text, no extra call |
| Emotion analysis provider | `""` | Empty uses the session's default model |
| Emotion analysis prompt | built-in | Empty uses the built-in template |

Neither mode modifies the bot's actual reply text; the only difference is whether an extra lightweight call happens.

### Models and retrieval

| Setting | Default | Description |
|:---|:---|:---|
| Vision model | `""` | Empty uses the global image-captioning model |
| Enable embedding search | `true` | Vector similarity; falls back to BM25 when off or unavailable |
| Embedding provider ID | `""` | Empty uses the first embedding provider |
| Similarity weight preset | `balanced` | balanced / keyword-first / semantic-first / strict, instead of hand-tuning five weights |

### Batch analysis

| Setting | Default | Range | Description |
|:---|:---|:---|:---|
| Concurrency | `2` | 1–16 | Images in flight to the vision model |
| Requests per minute | `20` | 0–600 | 0 disables rate limiting. When unsure, take the documented RPM and cut it by 30% |
| Max retries | `3` | 0–8 | Retry ceiling for throttling and transient errors |
| Retry backoff base | `2.0` | 1.0–10.0 | Multiplier between retries |

### Storage and chat filtering

| Setting | Default | Description |
|:---|:---|:---|
| Max stickers | `100` | Oldest entries are pruned past this; raise it if you want a big library |
| Cleanup strategy | `balanced` | `conservative` stale index + temp only / `balanced` also orphan files and thumbnails / `aggressive` also the `raw` originals (smallest footprint, no way back to the original file) |
| Send / collect allow and block lists | `[]` | `group:<id>` or `user:<id>`; both lists can be active at once |
| List priority | `whitelist_first` | Who wins when both match |
| VLM prompts (plain / with moderation) | built-in | Empty uses the bundled `prompts.json` templates |

## The dashboard

Plugin details page → "Sticker Manager". Two areas:

- **Review queue** — where auto-collected images land. Approve or delete individually or in bulk, and edit category, tags, description, character and work before approving.
- **Library** — filter by category, work or keyword; four sort orders (most sent / recently sent / newest / oldest, all done in SQL); bulk category change, delete, scope, character/work assignment and source-scope repair.

Plus: single upload (with optional AI analysis), batch import, batch re-analysis, duplicate cleanup, storage maintenance (scan and clear stale index entries, orphan files, thumbnails, temp files) and category management.

Three themes (auto / terminal / pixel) and three languages; your choice is remembered.

> **Careful**: deleting a category in the WebUI deletes every image file in it.

## Where the data lives

```
data/plugin_data/astrbot_plugin_meme_magpie/
├── categories/<category>/    # stickers in the library
├── pending/                  # awaiting review
├── raw/                      # original staging copies
├── cache/emoji.db            # SQLite (entries, tags, vectors, blocklist)
├── cache/                    # thumbnail cache
├── temp/                     # temp files
├── categories.json           # category list
├── category_info.json        # category descriptions
├── characters.json           # character list
└── character_info.json       # character descriptions
```

Back it up by archiving that one directory.

## FAQ

**Do I have to uninstall astrbot_plugin_stealer?**
Not technically. The two plugins use different data directories, command prefixes and WebUI routes, so they coexist fine. But **both will collect and send**, so you would get duplicates. After migrating, turn off the old plugin's collect/send switches or uninstall it.

**Can migration damage the old plugin's data?**
`apply` only reads from the old directory and writes to the new one — not a byte of the original changes, so you can always fall back. Only `move` relocates files, and that one is irreversible.

**Images are being collected but have no tags. Why?**
Almost always a vision-model problem. Check the AstrBot log for errors and confirm the vision model setting or global captioning model works. Untagged images already in the library can be fixed with batch re-analysis.

**Batch analysis keeps returning 429.**
Drop concurrency to 1 and RPM to 6–10. If it still throttles, your quota is genuinely tight: use the item limit to process a few dozen at a time.

**Stickers come too often / too rarely.**
Adjust the send probability. If they arrive at odd moments, make sure the intent gate is on.

**Is an embedding model required?**
No. Without one it uses BM25 keyword search; only cases where wording differs but meaning matches will be weaker.

**Is 100 stickers not a bit low?**
It is a conservative default — raise it. Note that exceeding the cap prunes the oldest entries, so check before lowering it.

**Will it collect someone's private screenshot?**
Yes — it has no idea what privacy is. That is why human review is on by default, why there is a `local` scope (sendable only in the chat it came from), and why there is a blocklist. Keep review enabled and use the collect blocklist to exclude sensitive chats.

## What got fixed

Relative to upstream `astrbot_plugin_stealer`:

- **Wrong ingestion method for LLM collection** — images collected through the LLM tool were recorded with `add_method = auto`, mixing them in with automatic collection. Now recorded as `llm`.
- **Dirty data left behind on batch analysis failure** — when the vision model returned an invalid category, upstream reverted only the category and kept the tags and description from the same bad response. Now the whole analysis result is discarded together.
- **WebUI search missed fields** — the index fallback path searched only tags, description and scenes, so on-image text, character, work and original filename were unsearchable. Now aligned with the SQL path.
- **Unbounded batch concurrency** — upstream's own README carried a "high concurrency warning, analyse in batches". Replaced with configurable concurrency, an RPM token bucket and backoff retries, so no manual batching is needed.
- **Silent pass-through when moderation failed** — behaviour when the moderation model timed out or ran out of quota was not controllable. It now rejects by default, with an explicit opt-in to fail open.
- **Dead code removed** — the unused `steal_image_direct` path is gone.

## Notes

- **A vision model is mandatory**; without one, nothing that involves recognition works.
- Deleting a category in the WebUI deletes the image files inside it.
- With "send as GIF" enabled, forcing very large images to GIF causes memory spikes — leave it off on small machines.
- Sticker content and copyright belong to their creators. Follow your platform's rules and do not use this to spread prohibited content.

## Licence and credits

**AGPL-3.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

> Upstream's README says MIT, but the `LICENSE` file actually committed to that repository is AGPL-3.0 (and GitHub detects it as AGPL-3.0). Where the two disagree, a downstream project can only rely on the licence that is actually present and stricter, so this fork is AGPL-3.0. You may use, modify and redistribute it freely, but distributing a modified version — including offering it as a network service — requires publishing the source.

Forked from [nagatoquin33/astrbot_plugin_stealer](https://github.com/nagatoquin33/astrbot_plugin_stealer). Original copyright belongs to nagatoquin33; modifications in this fork are copyright Whereis-Alice. The original idea drew on maibot's sticker collection and meme_manager's tag-injection approach.

---

<div align="center">

If you find it useful, a ⭐ is appreciated.

Questions and bug reports: [Issues](https://github.com/Whereis-Alice/astrbot_plugin_meme_magpie/issues).

</div>
