# External sticker sources

> [← Back to README](../../README_EN.md) · [Docs index](README.md) · [中文](../external-sources.md)

Besides pilfering images one at a time from your groups, you can import a whole sticker pack somebody else already built. The entry point is "External sticker sources" on the dashboard's library page.

It is **read-only**: not a single byte of the source is modified, and images are copied as hosted duplicates into this plugin's data directory. No code from the source is ever executed and there is no `git clone`.

## Three kinds of source

### A local pack archive

Pick a `.zip` or `.meme-pack` in the browser; preflight runs as soon as the upload finishes.

The recommended layout is the AstrBot Meme Pack v2 export format:

```text
my-pack/
├─ manifest.json              # pack id / name / category list (recommended)
├─ meme_pack_export.json      # v2 export descriptor (optional)
├─ memes/<category>/*         # images, required
├─ semantic_metadata.json     # per-image description / tags / overlay text (optional)
└─ previews/                  # previews, skipped on import
```

Metadata files are not required. Any of `manifest.json`, `memes_data.json`, `semantic_index.json`, `semantic_metadata.json` and `meme_pack_export.json` is accepted, and a pack with none of them still imports — the category is inferred from the first directory below `memes/` (or from the image's parent directory), while descriptions and tags stay empty until you fill them in via the dashboard's "failed analysis" tool.

Recognised image formats: `png` / `jpg` / `jpeg` / `gif` / `webp` / `bmp`. Preview directories such as `previews/`, `thumbnails/` and `thumbs/` are skipped.

If [astrbot_plugin_meme_manager](https://github.com/anka-afk/astrbot_plugin_meme_manager) is installed in the same AstrBot, the packs under its `packs/<pack-id>/` directory **show up in the source list automatically**, labelled "discovered" — no manual upload needed.

The reader follows the public [AstrBot Meme Pack protocol](https://github.com/anka-afk/astrbot-meme-pack-index/blob/main/ASTRBOT_MEME_PACK_PROTOCOL_ZH.md); unknown fields are ignored safely.

### A GitHub repository

Enter `owner/repo`, or paste a URL:

```text
owner/repo
https://github.com/owner/repo
https://github.com/owner/repo/tree/v1.1.0
https://github.com/owner/repo/tree/main/packs/happy
owner/repo?ref=feature/new-pack&subpath=packs/happy
```

`/tree/<branch-or-tag>/<subpath>` selects a branch and a directory inside the repository; use the `?ref=&subpath=` form when the branch name itself contains `/`. Without a ref, the default branch is resolved through the GitHub API.

The repository is downloaded as a bounded ZIP archive into `external_sources/github_cache/` and then handed to the same pack reader, so the layout requirements are identical to a local pack (`manifest.json` + `memes/` at the root or below the selected subpath).

### An HTTP catalog

An HTTPS endpoint returning a sticker catalog as JSON. The top level may be an array, or an object whose item array is named `items`, `memes`, `data` or `results`:

```json
{
  "id": "community-reactions",
  "name": "Community Reactions",
  "version": "2026.09",
  "license": "CC-BY-4.0",
  "attribution": "Example Community",
  "items": [
    {
      "id": "happy-001",
      "url": "https://cdn.example.org/memes/happy-001.webp",
      "category": "happy",
      "description": "A delighted reaction",
      "visible_text": "yesss",
      "tags": ["celebration", "agreement"],
      "scenes": ["reacting to good news"],
      "emotions": ["happy", "excited"],
      "character": "Lucia",
      "work": "Some Series",
      "license": "CC-BY-4.0",
      "attribution": "Artist Name"
    }
  ],
  "next_cursor": "page-2"
}
```

Common fields accept aliases:

| Meaning | Accepted field names |
|:---|:---|
| Item ID | `id` / `external_id` / `key` |
| Image URL | `url` / `image_url` / `source_url` / `src` |
| Category | `category` / `emotion` |
| Description | `description` / `desc` |
| Overlay text | `visible_text` / `overlay_text` |
| Scenes | `scenes` / `scene` |
| Character | `character` / `role` |
| Work / series | `work` / `series` / `source_work` |
| Attribution | `attribution` / `author` |

Top-level `license` / `attribution` become defaults for items that omit them. Image URLs may be absolute or relative to the catalog endpoint. For pagination return `next_cursor` or `next`; the plugin re-requests the same endpoint with `?cursor=<value>`. A repeated cursor stops pagination safely, and there is a 100-page ceiling.

Endpoints that need authentication can be registered with four extra request headers: `Accept`, `Authorization`, `User-Agent`, `X-API-Key`. **Header values are stored in the plugin database** (otherwise later syncs could not authenticate), but the source-list API always redacts them and provenance records never carry credential-like fields — look after your AstrBot data directory accordingly.

## How an import runs

1. **Preflight** reads the catalog without writing anything. It reports the item count, total size, categories, licence notice, and shows a sample of the contents. If the import would exceed "maximum number of stickers", you get a warning right here.
2. **Category mapping** — the source's category names on the left, the local category each one lands in on the right. Leave it on "auto" to match by name and synonyms; anything unmatched falls into the fallback category.
3. **Options** — send to Pending Review first, visibility scope (including session limits such as `group:123456`), assign one character to the whole batch.
4. **Per-image write** — read within the byte limit → decode and format-check with Pillow → pixel limit → SHA-256 deduplication → atomic copy into the target category or Pending Review.
5. **Result** — four counters: imported, sent to review, skipped as duplicate, failed.

Imports record the description, overlay text, tags, scenes, emotions, original filename, dimensions, format, licence, attribution and source URL. An image already present in the library or the review pool (matched by hash) is not copied a second time; it is simply linked to the source — **a duplicate counts as a duplicate, not a failure**.

### Three things that may not match your expectations

**Imports do not call the vision model.** Running a VLM over several hundred images is slow and expensive, and a good pack ships descriptions anyway, so the plugin reuses whatever the pack provides; anything the pack left blank stays blank. To fill the gaps, use the library toolbar's "failed analysis" detector afterwards to list entries without a description and hand the whole batch to batch re-analysis, which is rate-limited and will not hammer your provider.

**Character names are not written unconditionally.** A character name from the pack is only written to the database when it matches a character you have **already registered**; anything else is kept in the provenance record only, so your character list stays clean. To tag a whole batch with a new character, tick "assign one character to the whole batch" and type the new name — the plugin registers it for you. Filling in an unknown character *without* ticking that box rejects the whole batch rather than creating it silently.

**External images are exempt from capacity eviction.** Their `retention_class` is `external`, so the "maximum number of stickers" cleanup skips them. Importing a big pack therefore cannot push out the stickers you collected yourself — but the reverse is also true: if external images fill your disk, the capacity cap will not reclaim them and you have to delete them yourself. Favourites still count towards capacity; they are merely evicted last.

Also note that when the plugin's content filtration is enabled, external imports **always** go to Pending Review and the dialog's toggle is locked. A full review pool rejects the import outright instead of dropping items midway.

## Registered sources and incremental sync

A successful import registers the source in the "registered sources" list. From then on:

- **Sync** re-reads the source and imports only what is new. Items seen previously but missing from the new catalog are **never deleted**; they are flagged `stale` in provenance (the source dropped the image, your copy remains). A failed sync does not flag anything stale.
- **Forget** removes only the registry and provenance rows. **Not one imported sticker is deleted.**

Only one import/sync job may run at a time, and a paused job still holds that slot. Jobs can be paused, resumed and cancelled; cancelling keeps whatever was already imported. The job panel shows the phase (queued → reading catalog → importing → finalising), progress, the four counters and an ETA. A single job stops after 20 accumulated errors so a thoroughly broken source cannot spam hundreds of log lines.

Browser uploads are staged in `external_sources/uploads/` and cleaned up after 24 hours, except while a job still references them.

## Safety limits

- Only `https://` is allowed unless you explicitly enable "allow plaintext HTTP sources".
- Loopback, private, link-local, multicast and reserved destinations are rejected — both literal addresses and DNS results, on the initial request and on every redirect. URLs containing `user:pass@` are refused.
- A cross-origin redirect drops `Authorization` and `X-API-Key` so credentials are never forwarded to a third party.
- Archive members are rejected for absolute paths, `..` traversal, symlinks, too many members, oversized compressed/uncompressed totals, oversized single files, and empty image packs.
- Catalog body size, per-image size, item count, pixel count, page count, redirect count and concurrent background jobs are all bounded.
- Local paths passed through the Web API must stay inside AstrBot's plugin-data directory, so this cannot become arbitrary file reading.
- Endpoints and provenance returned to the frontend are redacted.

## Related settings

Configuration page, "External sticker sources" group:

| Setting | Default | Notes |
|:---|:---|:---|
| Enable external sticker sources | `true` | Master switch; when off every related route refuses, already-imported stickers are unaffected |
| Allow plaintext HTTP sources | `false` | Only needed for a self-hosted LAN service |
| Imported images go to Pending Review | `false` | Can also be ticked per import |
| Maximum items per source | `2000` | The excess is truncated and reported in preflight |
| Maximum bytes per image | 32 MiB | Larger images are skipped and counted as failures |
| Maximum archive size | 1 GiB | The size of the archive itself |
| Maximum uncompressed size | 4 GiB | Blocks zip bombs |
| Maximum pixels per image | 40 million (~6300×6300) | Blocks huge images that would exhaust memory while decoding |

## Troubleshooting

**Preflight failed with a message I cannot parse.** The low-level source readers emit English messages so they are easy to paste into an issue; the frontend prepends a localised prefix. The prefix comes from the plugin, the rest is the reader's own wording.

**GitHub downloads are slow or time out.** Downloads use GitHub's archive endpoint, and a large repository simply takes a while. If your machine needs a proxy, the AstrBot process itself has to use it (`HTTPS_PROXY` environment variable); the plugin has no separate proxy setting.

**Will importing the same source twice create duplicates?** No. Deduplication is by SHA-256 of the image content, so the same image is stored once no matter how many sources it came from; the extra rows only record "this image also belongs to that source".
