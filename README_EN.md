<div align="center">

<img src="logo.png" alt="Meme Thief" width="150">

# Meme Thief · meme神偷

**Lets AstrBot pilfer the stickers people post in chat, then send a mood-appropriate one when replying.**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.24.1-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)

**Language / 语言**: [English](README_EN.md) · [中文](README.md)

</div>

---

## What is this

Magpies are notorious thieves — anything shiny goes straight into the nest. This plugin does the same: stickers that scroll past in chat get pilfered into your library, a vision model works out what each one is expressing, tags it, transcribes any text printed on the image, and later the bot picks one that matches the mood of its reply.

Three independent switches, none of them coupled:

- **Collect** — watch chat images and store them in a review queue, gated by probability or cooldown. Nothing enters the library until you approve it in the WebUI.
- **Understand** — a vision model (VLM) fills in category, tags, usage scenes, on-image text, character and source work.
- **Send** — after the bot replies, roll the dice on whether to attach a sticker; which one gets picked comes from semantic search plus emotion matching.

Done collecting? `/mp off`. Everything already in the library still works.

### About the name

The plugin is called **Meme Thief** (Chinese: meme神偷) and its command group is `mp` — meme + pilfer, two letters that are quick to type.

Version 1.0 shipped as "Meme Magpie", so the old `magpie` and the Chinese `神偷` are kept as aliases. All three are equivalent:

```
/mp status
/magpie status
/神偷 status
```

The repository and Python package are still named `astrbot_plugin_meme_magpie`, and that is **deliberate**: AstrBot uses the `name` field of `metadata.yaml` both as the plugin directory name and as the data directory `data/plugin_data/<name>`. Renaming it would make every existing user's sticker library and database vanish, so that layer stays exactly as it was.

### Relationship to astrbot_plugin_stealer

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
| **WebUI dashboard** | Review queue + library: browse, edit, sort, bulk actions, batch import, batch re-analysis, missing-description detector, storage cleanup |
| **Chat filtering** | Separate allow/block lists for collecting and sending, with `group:<id>` and `user:<id>` entries |
| **Duplicate cleanup** | Perceptual hashing (pHash) finds visually identical images |
| **Localized UI** | 中文 / English / Русский |
| **QQ marketplace stickers** | Paid QQ marketplace stickers (`mface`) get collected too, with adapters for LLBot, NapCat and SnowLuma |
| **Message-splitting friendly** | Stickers can ride along in the bot's own reply chain so plugins like outputpro_split can lay them out |

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
/mp on        # start collecting
/mp auto_on   # start attaching stickers to replies
/mp status    # see how many you have
```

> **About the `/` prefix**: `/` is only AstrBot's factory-default wake prefix. You can change it to `!`, `#` or `.` in AstrBot's settings, or clear it entirely (then you simply send `mp status`). This document always writes `/` — substitute whatever you actually use. Direct messages and @-mentions need no prefix at all.
>
> Not sure which prefix is active? Every command example the plugin prints in chat is rendered with **your** effective prefix, so you can copy it straight out of the reply. Sending `/mp help` prints the full subcommand list.

### 4. Open the dashboard

AstrBot plugin details page → "Meme Thief Dashboard". No extra port, no extra password.

## Migrating from the original plugin

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

After migrating, check that "max stickers" is large enough. If you just brought in several hundred images while the cap is still a small number, everything above the cap counts as overflow. The default is `2000` and overflow only produces a log warning by default — see [Library capacity cap](#library-capacity-cap).

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

### Known facts go into the analysis prompt

Metadata you supply is not just stored — it is also written into the vision model's prompt. Analysis requests gain a block like this:

```
<known_facts>
The following was provided by the user or by the upstream chat model and is
confirmed correct. Use it as-is; do not contradict or rewrite it:
- Work: Bocchi the Rock!
- Character: Hitori Gotoh
- Action: clutching her head, screaming
description and scenes must build on the facts above rather than inventing a
different account. tags should only add visual keywords not already listed.
</known_facts>
```

The model then spends its effort describing the image instead of guessing who is in it. All three entry points are wired up:

- **LLM collection** — whatever the model passed as `work` / `character` / `action` / `overlay_text`
- **Single-image analysis in the dashboard** — the work and character in the dialog are sent along automatically
- **Batch re-analysis** — each image reuses the work and character already on record, so the model never re-guesses what is already known

To drop a hint, submit that field empty. The analysis cache is keyed by the known facts, so results with and without hints never contaminate each other, and overriding the built-in template with a custom prompt does not disable the feature (when the template has no `{known_facts}` placeholder, the block is inserted at a sensible spot automatically).

## Batch import and batch re-analysis

Both live in the WebUI library page and share the same rate limiter.

### Batch import

Upload dozens or hundreds of images at once. Two mutually exclusive modes:

- **Pick a category** — files land in that category, no vision calls, fastest path.
- **Auto-analyse** — leave the category empty and the VLM classifies and tags each image.

You can also set a character and a source work for the whole batch up front.

### Batch re-analysis

Re-runs vision analysis over images already in the library. Useful when early imports have no tags, or when you have switched to a better vision model and want everything re-labelled.

- **Scope**: selected images / images missing tags or descriptions / only those with no description / everything
- **Overwrite existing values**: off by default, so it only fills blanks and never touches descriptions you wrote by hand
- **Item limit**: cap the batch size and test the waters first
- Re-analysis deliberately **does not change categories** — that would mean moving files, which is risky under concurrency. It reports a *suggested* category and leaves the decision to you.

**The review queue can be re-analysed too**: pending images support the same bulk or single re-run, with the same three scopes. The one difference is that **a pending item's category does get corrected** — there it is only a database field, and changing it moves no files. Library categories map to real directories, which is why those stay advisory.

### Finding entries with no description

Analysis fails once in a while — a timeout, an upstream rate limit, or a model that returns nothing all leave behind a record with an image but no description. The description is what search leans on most, so an empty one effectively makes that image unfindable.

Both the library and the review queue have a **Missing description** button in the toolbar. It lists every entry whose description is empty, i.e. everything showing *No description* in the list. Each row offers three ways out:

- write a description yourself and hit **Save Only**;
- click **Analyze this one** to re-run just that image — the result is **placed in the input box first**, so nothing is written to the database until you confirm it;
- or hand the whole set to batch re-analysis in one click.

When the review queue is very large only the most recent slice is checked; the dialog says how many it looked at, and reopening it continues down the list.

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

Command group `mp`, aliases `magpie` and `神偷`. The table lists subcommands only — prepend your wake prefix when you send them, e.g. `/mp status` (default prefix) or `!mp status` if you changed it to `!`.

### Available to everyone

| Command | Description |
|:---|:---|
| `status` | Runtime status and library statistics |
| `list [category] [per_page] [page]` | List collected stickers (10 per page, pages start at 1) |
| `emotion_stats` | Emotion-analysis statistics and current mode |
| `help` / `帮助` | Print the full subcommand list using your current wake prefix |

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
| `clean` | Empty the `raw` staging directory; stickers already in the library are untouched |
| `capacity` | Run a capacity-control pass now. **This permanently deletes the oldest stickers above the cap** (favourites are kept; use `status` if you only want to read the numbers) |
| `tag_stats [N]` | Tag health check: frequent tags, noisy rare tags, untagged entries (N defaults to 15) |
| `rebuild_index` | Rebuild the retrieval index. Index only — it **never deletes images** |
| `rebuild_vectors` | Wipe and rebuild the semantic search vectors. Use it when you edited a description but search still returns the old one; says so and does nothing if no embedding model is configured |
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
| Send as GIF | `false` | Feels more like a real sticker; the cost is re-encoding the animation on every send. The conversion keeps at most 30 frames and is capped by total pixels (very large animations automatically keep fewer), peaking around 15MB for a 512-square animation. Leave it off if memory is tight |
| Fixed delay (s) | `5.0` | Keeps out of the way of message-splitting plugins; 0 sends immediately |
| Randomize delay | `false` | Pick a random delay between the fixed and maximum values |
| Per-character delay (s) | `0.3` | Extra wait scaled by reply length, simulating "read the text, then react" |
| Smart selection | `true` | Composite scoring; off means purely random |
| Auto sticker delivery mode | `separate` | `separate` sends its own message / `attach` rides along in the reply's message chain, see [Working with message-splitting plugins](#working-with-message-splitting-plugins) |
| `attach` selection timeout (s) | `10.0` | `attach` mode only; on timeout it falls back to a separate message delivered asynchronously, so the reply is never held up |
| Emit compat path for splitters | `false` | `attach` mode only, see [What is that "compat path" switch for](#what-is-that-compat-path-switch-for) |

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
| Max stickers | `2000` | Library cap; `0` = unlimited. Going over the cap **permanently deletes** the oldest entries (favourites excluded) — see [Library capacity cap](#library-capacity-cap) |
| Auto-clean over capacity | `false` | Off: overflow only logs a warning and nothing is deleted. On: the background job prunes the oldest every hour |
| Cleanup strategy | `balanced` | `conservative` stale index + temp only / `balanced` also orphan files and thumbnails / `aggressive` also the `raw` originals (smallest footprint, no way back to the original file) |
| Send / collect allow and block lists | `[]` | `group:<id>` or `user:<id>`; both lists can be active at once |
| List priority | `whitelist_first` | Who wins when both match |
| VLM prompts (plain / with moderation) | built-in | Empty uses the bundled `prompts.json` templates |

### Library capacity cap

This is the only mechanism in the plugin that deletes your data on its own, so it gets its own section:

- "Max stickers" is a hard cap. Above it, the oldest entries go first and **the image files go with them — there is no undo** (entries marked as favourite are never touched).
- Default `2000`, plenty for almost everyone. Set `0` for unlimited (bounded only by your disk).
- By default **nothing is deleted automatically**: going over the cap only writes one warning to the log (search for `容量控制`) telling you the current count and the overflow. Run `/mp capacity` to actually prune, or turn on "auto-clean over capacity" to have it done hourly.
- `/mp status` only reads the numbers. `/mp capacity` is the one that deletes.

> **In 1.3.0 and earlier this setting defaulted to `100`**, the hourly background job pruned automatically, and `/mp rebuild_index` pruned as a side effect too. If stickers ever vanished on you in an older version, this was almost certainly why — grep the log for `容量控制` to confirm. Since 1.4.0 the default is 2000 and overflow only warns.

## Platform and protocol adapters

The plugin itself is platform-agnostic — collecting and sending images works anywhere. The one thing that needs special care is **QQ marketplace stickers** (the paid packs you have to download): every OneBot adapter ships them differently, so they get dedicated handling.

| Adapter | How marketplace stickers arrive | Sticker flag field | `summary` label |
|:---|:---|:---|:---|
| **LLBot** | A separate `mface` segment, which AstrBot does not turn into an image component | `subType` only (camelCase) | Not sent |
| **NapCat** | Folded into a normal `image` segment, without even a `sub_type` key | `sub_type` only (snake_case) | Usually empty |
| **SnowLuma** | Folded into a normal `image` segment, with `sub_type` set to `0` | `sub_type` only | Always present |

What that means in practice:

- **Collecting** — marketplace stickers are read straight from the raw OneBot segments rather than from AstrBot's image components (otherwise LLBot's `mface` segment is lost entirely). Deciding "this is a sticker, not a photo" looks at `sub_type` / `subType` / `summary` and also at marketplace-only fields such as `emoji_id`, `emoji_package_id` and `key` — that last layer is what catches SnowLuma's `sub_type: 0`. The `emoji_id`, the pack id and the label are stored with the entry so the source can be traced later.
- **Sending** — when an image goes out *as a sticker*, `summary`, `sub_type` and `subType` are all written at once, so every adapter renders it correctly and quietly ignores the keys it does not know.

This assumes OneBot's `messageFormat` is `array` (the default). Set it to `string` and the raw message becomes one CQ-code string, at which point marketplace stickers cannot be read at all.

Other platforms (Telegram, the official QQ bot API) are handled as ordinary images with no loss of function. The official QQ bot API has no OneBot sticker flag, so the separate "sticker collection mode (QQ_Official)" setting decides which images qualify.

## Working with message-splitting plugins

With a plugin like [astrbot_plugin_outputpro_split](https://github.com/Whereis-Alice/astrbot_plugin_outputpro_split) installed — the kind that chops one long reply into several messages — this plugin's sticker is by default **a separate message**, invisible to the splitter and therefore outside its image and sticker layout rules. Switching "auto sticker delivery mode" to `attach` fixes that:

| Value | Behaviour | Use when |
|:---|:---|:---|
| `separate` (default) | The sticker is its own message, exactly as before | No splitter installed, or you want the safest compatibility |
| `attach` | The sticker is appended to the end of the reply's message chain | You want the splitter to lay out text and sticker together |

The cost of `attach` is that selection has to finish before the reply goes out, so replies get slightly slower. A timeout guards this (10 s by default): on timeout, on no match, on any failure at all, it falls back to `separate` asynchronous delivery — **the reply itself is never blocked, and the worst case is a sticker that arrives a moment late**. `attach` also stands down when the reply is about to be rendered as one long image (AstrBot's text-to-image), since a sticker sitting at the end of the chain would be drawn into the picture.

### What is that "compat path" switch for

The way `outputpro_split` recognises "this image is a sticker" is by looking for `plugin_stealer` in the image path — that is the original plugin's directory name, and this plugin naturally uses a different one.

Turn on "emit compat path for splitters" and the sticker about to be attached is first hard-linked (copied if the link fails, e.g. across volumes) into a compat directory whose name contains that string, and attached from there, which the splitter recognises correctly. The original file and the path recorded in the library are untouched, and the compat directory keeps only the 64 most recent files, so disk use is negligible.

It is a stopgap: the cleaner fix is for upstream to add `astrbot_plugin_meme_magpie` to its own list of keywords, and then this switch can go back off.

## The dashboard

Plugin details page → "Meme Thief Dashboard". Two areas:

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
├── plugin_stealer_split_compat/  # only appears once "compat path" is on, see the splitter section
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

**My server only has 1GB (or 512MB) of RAM. Will analysing hundreds of images blow it up?**
No, but keep concurrency low. Peak memory per image is now decoupled from image size: a 512-square 60-frame animation takes about 17MB, a 1920×480 120-frame animation about 22MB, a 4000×4000 still about 45MB (before 1.3.0 those three were 89MB, 331MB and 185MB). A batch peaks at roughly *per-image peak × concurrency*, so on a small box leave batch concurrency at the default 2 or drop it to 1 — hundreds of images will just take longer, not run you out of memory. Also note that “send as GIF” adds one more encode at send time, so leave it off when memory is tight.

**Stickers come too often / too rarely.**
Adjust the send probability. If they arrive at odd moments, make sure the intent gate is on.

**Is an embedding model required?**
No. Without one it uses BM25 keyword search; only cases where wording differs but meaning matches will be weaker.

**I edited a description but search still returns the old text?**
It should not happen — editing a description updates the vector immediately, and startup tops up anything stale by text fingerprint. If you do hit it (misaligned vectors left over from an older version), run `/mp rebuild_vectors` once.

**What is the sticker limit? Will it delete my images?**
`2000` by default, `0` means unlimited. By default going over the cap **only warns and deletes nothing** — you have to run `/mp capacity` yourself, or turn on "auto-clean over capacity".

**Dozens of stickers disappeared without a word?**
Grep the log for `容量控制` first. In 1.3.0 and earlier the cap defaulted to 100 and the hourly background job pruned the oldest entries above it, logging a single INFO line that is easy to miss — people who migrated several hundred images from the old plugin hit this most often. 1.4.0 raises the default to 2000 and no longer prunes by default; see [Library capacity cap](#library-capacity-cap).

**Will it collect someone's private screenshot?**
Yes — it has no idea what privacy is. That is why human review is on by default, why there is a `local` scope (sendable only in the chat it came from), and why there is a blocklist. Keep review enabled and use the collect blocklist to exclude sensitive chats.

**Marketplace stickers are not being collected on LLBot / NapCat / SnowLuma?**
First check that OneBot's `messageFormat` is `array` and not `string` — the plugin needs raw message segments to see marketplace stickers at all. The differences between the three adapters themselves are already handled; details in [Platform and protocol adapters](#platform-and-protocol-adapters).

**Stickers look out of place now that I run a message-splitting plugin?**
Set "auto sticker delivery mode" to `attach`, and if needed also turn on "emit compat path for splitters". See [Working with message-splitting plugins](#working-with-message-splitting-plugins).

## What got fixed

Relative to upstream `astrbot_plugin_stealer`:

- **The tag-statistics command wiped the staging directory, while the cleanup command never worked** — upstream's `clean` method lost its definition line, so its whole body fell into the preceding method. `clean` therefore raised `AttributeError`, and the read-only `tag_stats` emptied the `raw` staging directory on every run (which may still hold images queued for analysis). The two are now separate, with regression tests pinning them apart.
- **Wrong ingestion method for LLM collection** — images collected through the LLM tool were recorded with `add_method = auto`, mixing them in with automatic collection. Now recorded as `llm`.
- **Dirty data left behind on batch analysis failure** — when the vision model returned an invalid category, upstream reverted only the category and kept the tags and description from the same bad response. Now the whole analysis result is discarded together.
- **WebUI search missed fields** — the index fallback path searched only tags, description and scenes, so on-image text, character, work and original filename were unsearchable. Now aligned with the SQL path.
- **Unbounded batch concurrency** — upstream's own README carried a "high concurrency warning, analyse in batches". Replaced with configurable concurrency, an RPM token bucket and backoff retries, so no manual batching is needed.
- **Silent pass-through when moderation failed** — behaviour when the moderation model timed out or ran out of quota was not controllable. It now rejects by default, with an explicit opt-in to fail open.
- **Dead code removed** — the unused `steal_image_direct` path is gone.

1.2.0 fixed these as well — the first two are upstream's, the last two were our own oversights:

- **The single-image analysis endpoint masked clear errors as a 500** — the temp-file variable was initialised inside the `try` block while `finally` reads it. Any early return (vision service not configured, request body failing to parse) made `finally` raise `UnboundLocalError`, so the frontend saw a bare 500 instead of the genuinely useful "vision service unavailable" message.
- **Sending *as a sticker* only wrote `summary`** — LLBot reads camelCase `subType` only, NapCat and SnowLuma read snake_case `sub_type` only, so some adapters rendered a sticker as a plain image. All three keys are now written together.
- **"Work" typed in the review queue was not saved** — the pending-update endpoint's writable-field allowlist was missing `work` and `overlay_text`, so filling them in and hitting save silently dropped them. This dates back to 1.0.0, when the work dimension was introduced.
- **Re-analysis suggested a category change redundantly** — the suggestion still appeared when it matched the current category, or when that change was already part of the same update.

1.3.0 fixed these too:

- **Still GIFs always failed analysis** — some upstream gateways only accept a whitelist of image formats (png / jpeg / webp and friends) and answer a GIF with `mime type is not supported`. The old code treated that as an ordinary failure and retried into the same wall, burning quota. Non-whitelisted formats are now converted to PNG before upload, and if a request is still rejected on mime grounds it is transcoded locally and retried once without consuming the retry budget. "Format not supported" is also no longer lumped in with genuine rate limiting.
- **Analysing high-frame-rate animations exhausted memory** — the old implementation decoded up to 60 frames into full-size RGBA and held them all in a list; a 1920×480 animation needed 300MB+ for that step alone. It now runs in two passes: 32×32 thumbnail fingerprints pick the most distinct frames, then only the chosen 12 are decoded and released right after being pasted. See the memory FAQ entry for measured peaks. PNG `optimize=True` is also gone — it burned 0.6–0.8s to save 12% of the bytes and was the other reason older versions stuttered on animations.
- **Converting to GIF at send time doubled memory** — the old code held both an RGBA copy of every frame and the palette copy Pillow builds while saving (5 bytes per pixel combined). Frames are now quantized as soon as they are read and released as encoding proceeds, with budgets on both frame count and total pixels. A 512-square 60-frame animation went from 41MB to 17MB with no quality change.
- **Half-filled dialogs vanished on a stray click** — the sticker-management and tag-editing dialogs used to close on any click on the backdrop or an Esc press, dropping you back to the main page and losing everything you had typed. Once a dialog has content, both the backdrop and Esc now just shake it and point you at the Cancel button. Three related bugs went with it: releasing a text drag outside the dialog closed it, Esc inside a confirm box fell through to the dialog underneath, and the preview layer could not be dismissed after cancelling an edit.
- **The batch re-analysis dialog overflowed its panel** — enough options pushed the buttons out of view. The body scrolls now and the actions stick to the bottom; the dialog also opens on the first scope that actually has items, and zero-item scopes are greyed out.

1.4.0 fixed these too (the first one matters most, and it is inherited from upstream):

- **Stickers silently disappeared** — upstream's storage cap defaults to 100, and the hourly background job deleted everything above it, files included, logging a single INFO line. Anyone who migrated several hundred images from the old plugin lost some without touching anything and with no way to tell why. Worse, upstream's own docs tell you to run `rebuild_index` after migrating, and that command ran the same pruning pass on completion. Now: the default cap is 2000, `0` means unlimited, the background job only warns unless you explicitly enable "auto-clean over capacity", `rebuild_index` never deletes images, and real deletions log at WARNING with the file names. See [Library capacity cap](#library-capacity-cap).
- **Orphan cleanup could wipe the whole library** — "file on disk with no database row" depends entirely on reading the database completely. If the data directory is not mounted or has been moved, the old logic treated every image as garbage. A single pass that would remove more than 20% (and at least 20 files) now only warns. Path comparison is also case- and separator-normalised: on Windows `D:\a\b.png` and `d:/a/b.png` are the same file, and the old code called it an orphan.
- **Edited descriptions still returned the old text** — updating a description deleted only the first matching vector, so duplicates left over from earlier versions stayed in the store and kept matching; a failed delete still wrote the new vector, leaving one image with two competing descriptions. It now deletes every duplicate in one go and skips the write with a warning if it cannot. A text-fingerprint table (schema v8) was added as well, so startup only re-embeds entries whose description actually changed and upgrading from an older version does not burn a full pass of embedding quota.
- **The WebUI showed fewer images than exist** — the library list used the image hash as its list key, so the same image filed under two categories collided and Vue rendered only one of them. It uses the file name now.

## Notes

- **A vision model is mandatory**; without one, nothing that involves recognition works.
- Deleting a category in the WebUI deletes the image files inside it.
- "Max stickers" is a hard cap that really deletes files. Read [Library capacity cap](#library-capacity-cap) before lowering it.
- With "send as GIF" enabled, every send re-encodes the animation. Peak memory is budget-capped now (see [Sending](#sending)) but still higher than sending the original file — leave it off if memory is tight.
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
