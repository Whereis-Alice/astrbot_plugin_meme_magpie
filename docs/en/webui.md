# The dashboard

> [← Back to README](../../README_EN.md) · [Docs index](README.md) · [中文](../webui.md)

Plugin details page → "Meme Thief Dashboard". Two areas:

- **Review queue** — where auto-collected images land. Approve or delete individually or in bulk, and edit category, tags, description, character and work before approving.
- **Library** — filter by category, work or keyword; four sort orders (most sent / recently sent / newest / oldest, all done in SQL); bulk category change, delete, scope, character/work assignment and source-scope repair.

Plus: single upload (with optional AI analysis), batch import, batch re-analysis, missing-description detection, external-source imports, duplicate cleanup, storage maintenance (scan and clear stale index entries, orphan files, thumbnails, temp files) and category management.

Three themes (auto / terminal / pixel) and three languages; your choice is remembered.

Rapid filter/category/character changes only apply the latest request's result.
Category keys must be safe single directory names. Existing valid Chinese names and
case are preserved without automatic renaming or migration. Path separators, device
names and linked directories are rejected; category updates validate the whole batch
before saving.

> **Careful**: deleting a category in the WebUI deletes every image file in it.

## Cross-page select-all, page size and clearing the library

Both areas support **cross-page selection**: once batch mode is on, paging never drops what you already selected, and the bottom bar keeps showing "Selected N" plus a clear-selection button.

- **Select page** — toggles the current page and accumulates across pages;
- **Select all N** — takes a one-shot snapshot of every result that matches the current filter (keyword, category, character, favourites…). Changing filters afterwards never widens or narrows the selection behind your back;
- **Per page** — pick auto / 60 / 120 / 240 in the pager; the choice is remembered and shared by the library and the review queue. Auto fits to your window, as before.

"Clear library" in the library toolbar wipes the whole library in one shot (image files and thumbnail cache included) and bypasses the selection UI. It asks twice, and the second step requires typing `DELETE`. It only touches the real library — the review queue, the blacklist and external-source records are untouched. Files that cannot be removed keep their index row so storage cleanup can retry later.

## Backup export and restore

"Export" in the library toolbar packs the library into a `magpie-backup` archive for periodic backups or moving everything to another machine:

- **Scope**: the whole library, or just the current filter (search / category / character / favourites);
- **Optional sections**: review queue and blacklist, each its own checkbox, both off by default;
- **What is inside**: images under per-category folders plus a full metadata JSON — description, tags, scenes, emotions, overlay text, character, work, favourites, use count, last used, created/reviewed timestamps, perceptual hash, scope and origin, retention class;
- **Download**: packing runs in the background with a progress bar; the file lives in `exports/` for 2 hours before cleanup.

Restore uses the same channel: **External sources → upload archive**. When a `magpie-backup` pack is detected, three switches appear:

- **Fill blank fields only** — for images that already exist, fill missing description / tags and never overwrite anything (off by default, purely additive);
- **Restore review queue** — put the archived pending items back; anything beyond the pool capacity is skipped with a note in the progress view;
- **Restore blacklist** — write the archived blacklist hashes back.

New categories from the backup are created automatically and characters are registered automatically, so restoring on a fresh machine needs no manual config. Duplicates are skipped by content hash, so importing the same pack twice never creates duplicates. The archive also carries a compatibility subset (`semantic_metadata.json`) so older plugin versions or other tools can still read descriptions and tags.

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
- **Apply category changes**: off by default; when enabled, detected categories are applied directly, moving the file and updating the index when the category changes
- **Item limit**: cap the batch size and test the waters first

Applying category changes is independent of overwriting labels: you can keep a hand-written description while still filing an old failed analysis into the right category. Before each write, the item is relocated by hash; file moves and index updates are serialized so concurrent workers cannot race for the same target path. Semantic fields and the category migrate in one database transaction.

Changing the vision model updates both actual requests and analysis-cache signatures;
when unset, the framework's current default is read for each resolution. Empty JSON,
missing required classification fields and invalid text retry within the existing budget
and fail explicitly rather than becoming fabricated successful classifications.
Valid moderation rejections and complete legacy pipe-delimited responses remain supported.

**The review queue can be re-analysed too**: pending images support the same bulk or single re-run, with the same scopes. A pending item's category is only a database field, so it is corrected under the existing rules without moving files; whether a library category changes automatically is controlled by the switch above.

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
- Re-analysis tasks additionally report how many fields changed, which images have a suggested category change, and which categories were changed automatically

Tasks can be **paused, resumed and cancelled** at any time; pausing genuinely stops the workers rather than just the UI. Closing the tab does not stop the task — reopen the panel and the progress reattaches.

## External sticker sources

The library page can also import a whole pack somebody else built — a local `.zip` / `.meme-pack`, a GitHub repository, or an HTTPS endpoint returning a catalog as JSON. Preflight shows the item count and categories before anything is written; a successful import registers the source so later additions can be pulled in with one click.

That area has enough rules and safety limits to deserve its own page: [External sticker sources](external-sources.md).
