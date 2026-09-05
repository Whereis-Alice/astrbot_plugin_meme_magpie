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

The plugin is called **Meme Thief** (Chinese: meme神偷) and its command group is `mp` — meme + pilfer, two letters that are quick to type; `magpie` and `神偷` are fully equivalent aliases. The repository and Python package are still named `astrbot_plugin_meme_magpie`, and that is **deliberate**: AstrBot uses that name for both the plugin directory and the data directory, so renaming it would make every existing user's library and database vanish.

This plugin is a fork of [nagatoquin33/astrbot_plugin_stealer](https://github.com/nagatoquin33/astrbot_plugin_stealer) (AGPL-3.0) — credit to the original author for the idea and the groundwork. It is a **fully independent plugin**: package name, commands, data directory, WebUI routes and LLM tool names are all different, so it can live alongside the original in the same AstrBot without clobbering anything. How the two relate, and what this fork adds, is in the [migration guide](docs/en/migration.md).

## Features

| Feature | Details |
|:---|:---|
| **Automatic collection** | Watches chat images; probability or cooldown mode, plus a one-line force-collect mode |
| **Review queue** | Auto-collected images wait for human approval, so random screenshots do not pollute the library |
| **Vision analysis** | The VLM returns structured data: category, tags, usage scenes, on-image text, emotions, character, source work |
| **Semantic search** | Text embeddings + on-image text + usage scenes; falls back to BM25 when embeddings are unavailable |
| **Emotion matching** | Extracts search terms and an emotion prior from the bot's own reply; the reply text is never modified |
| **LLM tools** | During conversation the model can search, send and collect stickers itself, filling in work and character as it goes |
| **WebUI dashboard** | Review queue + library: bulk actions, batch import, batch re-analysis, missing-description detector, duplicate cleanup, storage maintenance |
| **Rate-limited batches** | Concurrency and requests-per-minute are configurable, so hundreds of images will not trigger upstream 429s |
| **External sources** | Import whole packs from an archive / a GitHub repository / a JSON catalog, with incremental sync |
| **Data migration** | One command brings over astrbot_plugin_stealer's images, database, categories, character library, blocklist and settings |
| **Chat filtering** | Separate allow/block lists for collecting and sending, with `group:<id>` and `user:<id>` entries |
| **Adapters & i18n** | QQ marketplace stickers work across LLBot / NapCat / SnowLuma; UI in 中文 / English / Русский |

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

> **About the `/` prefix**: `/` is only AstrBot's factory-default wake prefix. You can change it to `!`, `#` or `.` in AstrBot's settings, or clear it entirely (then you simply send `mp status`). This document always writes `/` — substitute whatever you actually use; direct messages and @-mentions need no prefix at all. Not sure which prefix is active? Send `/mp help` and the plugin prints the full list using **your** effective prefix. Details in [Commands](docs/en/commands.md).

### 4. Open the dashboard

AstrBot plugin details page → "Meme Thief Dashboard". No extra port, no extra password.

## Common commands

Command group `mp`, aliases `magpie` and `神偷`. Subcommands only — prepend your wake prefix, e.g. `/mp status`.

| Command | Description |
|:---|:---|
| `status` | Runtime status and library statistics |
| `on` / `off` | Enable / disable collection |
| `auto_on` / `auto_off` | Enable / disable automatic sending |
| `偷` | 30-second force-collect mode; every image posted gets taken |
| `list [category] [per_page] [page]` | List collected stickers |
| `delete <index\|filename>` | Delete a sticker |
| `blacklist <index\|filename>` | Delete and blocklist it so it is never collected again |
| `migrate [check\|apply\|move] [path]` | Migrate data from astrbot_plugin_stealer |
| `help` / `帮助` | Print the full subcommand list using your current wake prefix |

Allow/block lists, scopes, capacity control, index rebuilds and tag health checks are all in [Commands](docs/en/commands.md).

## Migrating from the original plugin

`/mp migrate` brings `astrbot_plugin_stealer`'s data over. The whole thing is **idempotent** (running it twice imports nothing twice) and **non-destructive** (files are copied by default, the old plugin's data stays put):

```
/mp migrate check     # step 1: can it find the old data directory?
/mp migrate           # step 2: dry run — prints what would happen, writes nothing
/mp migrate apply     # step 3: do it (copies files, old data preserved)
```

What moves, what does not, passing the path manually, and using `move` when disk is tight: see the [migration guide](docs/en/migration.md).

## Key settings

Everything is edited on the AstrBot plugin config page. The knobs people actually touch:

| Setting | Default | Notes |
|:---|:---|:---|
| Vision model | empty | Empty means AstrBot's global image-captioning model |
| Enable collection | `false` | Master switch for collecting, same as `/mp on` |
| Collect probability | `0.3` | Chance each image is taken in probability mode |
| Require review | `true` | Turn it off and images skip the review queue |
| Send probability | `0.2` | Chance of attaching a sticker to a reply |
| **Max stickers** | `2000` | Hard library cap. Above it, the oldest entries **and their files are permanently deleted**; `0` = unlimited |
| Batch concurrency / RPM | `2` / `20` | The rate-limit valves for batch analysis; lower them if you get 429s |

> ⚠️ **"Max stickers" is the only mechanism in the plugin that deletes your data on its own.** It is the first field of the "Capacity and safety" group at the very top of the config page. By default it only warns, but read [Library capacity cap](docs/en/configuration.md#library-capacity-cap) before you lower it.

Everything else — send delays, emotion-analysis modes, retrieval presets, storage policy, allow/block lists — is in [Configuration](docs/en/configuration.md).

## The dashboard

Open it from the plugin details page ("Meme Thief Dashboard"), no extra port required. It has two areas: the **review queue**, where you approve or delete one by one or in bulk and can fix category, tags, description, character and work before approving; and the **library**, filterable by category / work / keyword with four sort orders and bulk edits. Plus single upload, batch import, batch re-analysis, missing-description detection, external-source imports, duplicate cleanup, storage maintenance and category management — in three themes and three languages.

> **Careful**: deleting a category in the WebUI also deletes every image file inside it.

Details in [The dashboard](docs/en/webui.md).

## Documentation

| Document | What is inside |
|:---|:---|
| [Commands](docs/en/commands.md) | Every subcommand, plus what the command prefix should actually be |
| [Configuration](docs/en/configuration.md) | Key settings by group, including the capacity cap that really does delete files |
| [The dashboard](docs/en/webui.md) | Review queue and library, batch import, batch re-analysis, rate limits and progress |
| [External sticker sources](docs/en/external-sources.md) | Importing whole packs from archives / GitHub / JSON catalogs, incremental sync, safety limits |
| [LLM-driven sticker usage](docs/en/llm-tools.md) | The three LLM tools, letting the model fill in work/character, how known facts reach the prompt |
| [Migrating from the original plugin](docs/en/migration.md) | How the two plugins relate, the three-step migration, what moves and what does not |
| [Platform and protocol adapters](docs/en/platforms.md) | LLBot / NapCat / SnowLuma marketplace-sticker differences, message-splitting plugins |
| [FAQ](docs/en/faq.md) | Frequently asked questions |
| [CHANGELOG](CHANGELOG.md) | What changed in each release |

## Where the data lives

```
data/plugin_data/astrbot_plugin_meme_magpie/
├── categories/<category>/    # stickers in the library
├── pending/                  # awaiting review
├── raw/                      # original staging copies
├── cache/emoji.db            # SQLite (entries, tags, vectors, blocklist)
├── cache/                    # thumbnail cache
├── temp/                     # temp files
├── plugin_stealer_split_compat/  # only appears once "compat path" is on, see docs/en/platforms.md
├── categories.json           # category list
├── category_info.json        # category descriptions
├── characters.json           # character list
└── character_info.json       # character descriptions
```

Back it up by archiving that one directory.

## Notes

- **A vision model is mandatory**; without one, nothing that involves recognition works.
- "Max stickers" is a hard cap that really deletes files. Read [Library capacity cap](docs/en/configuration.md#library-capacity-cap) before lowering it.
- Deleting a category in the WebUI deletes the image files inside it.
- With "send as GIF" enabled, every send re-encodes the animation. Peak memory is budget-capped but still higher than sending the original file — leave it off if memory is tight.
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
