# Configuration

> [← Back to README](../../README_EN.md) · [Docs index](README.md) · [中文](../configuration.md)

Everything is editable in the AstrBot plugin config page. The notable ones:

## Collection

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
| LLM parameter policy | `merge` | See [LLM-driven sticker usage](llm-tools.md#letting-the-model-fill-in-the-metadata) |

## Sending

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
| Auto sticker delivery mode | `separate` | `separate` sends its own message / `attach` rides along in the reply's message chain, see [Working with message-splitting plugins](platforms.md#working-with-message-splitting-plugins) |
| `attach` selection timeout (s) | `10.0` | `attach` mode only; on timeout it falls back to a separate message delivered asynchronously, so the reply is never held up |
| Emit compat path for splitters | `false` | `attach` mode only, see [What is that "compat path" switch for](platforms.md#what-is-that-compat-path-switch-for) |

## Emotion analysis

| Setting | Default | Description |
|:---|:---|:---|
| Extract search terms | `true` | `true` = LLM mode (recommended): a light model pulls search terms and an emotion prior out of the reply. `false` = passive mode: search with the raw reply text, no extra call |
| Emotion analysis provider | `""` | Empty uses the session's default model |
| Emotion analysis prompt | built-in | Empty uses the built-in template |

Neither mode modifies the bot's actual reply text; the only difference is whether an extra lightweight call happens.

## Models and retrieval

| Setting | Default | Description |
|:---|:---|:---|
| Vision model | `""` | Empty uses the global image-captioning model |
| Enable embedding search | `true` | Vector similarity; falls back to BM25 when off or unavailable |
| Embedding provider ID | `""` | Empty uses the first embedding provider |
| Similarity weight preset | `balanced` | balanced / keyword-first / semantic-first / strict, instead of hand-tuning five weights |

## Batch analysis

| Setting | Default | Range | Description |
|:---|:---|:---|:---|
| Concurrency | `2` | 1–16 | Images in flight to the vision model |
| Requests per minute | `20` | 0–600 | 0 disables rate limiting. When unsure, take the documented RPM and cut it by 30% |
| Max retries | `3` | 0–8 | Retry ceiling for throttling and transient errors |
| Retry backoff base | `2.0` | 1.0–10.0 | Multiplier between retries |

## Storage and chat filtering

| Setting | Default | Description |
|:---|:---|:---|
| Max stickers | `2000` | Library cap; `0` = unlimited. Going over the cap **permanently deletes** the oldest entries (favourites excluded). Lives in the "Capacity and safety" group at the very top of the config page — see [Library capacity cap](#library-capacity-cap) |
| Auto-clean over capacity | `false` | Off: overflow only logs a warning and nothing is deleted. On: the background job prunes the oldest every hour |
| Cleanup strategy | `balanced` | `conservative` stale index + temp only / `balanced` also orphan files and thumbnails / `aggressive` also the `raw` originals (smallest footprint, no way back to the original file) |
| Send / collect allow and block lists | `[]` | `group:<id>` or `user:<id>`; both lists can be active at once |
| List priority | `whitelist_first` | Who wins when both match |
| VLM prompts (plain / with moderation) | built-in | Empty uses the bundled `prompts.json` templates |

## Library capacity cap

This is the only mechanism in the plugin that deletes your data on its own, so it gets its own section:

- **Where to change it**: the very first group on the AstrBot plugin config page, "=== Capacity and safety (important) ===". "Max stickers" is the first field in it.
- "Max stickers" is a hard cap. Above it, the oldest entries go first and **the image files go with them — there is no undo** (entries marked as favourite are never touched).
- Default `2000`, plenty for almost everyone. Set `0` for unlimited (bounded only by your disk).
- By default **nothing is deleted automatically**: going over the cap only writes one warning to the log (search for `容量控制`) telling you the current count and the overflow. Run `/mp capacity` to actually prune, or turn on "auto-clean over capacity" to have it done hourly.
- The plugin also runs this check once at startup and says so in the log if you are over the cap (warning only, nothing is deleted).
- `/mp status` only reads the numbers. `/mp capacity` is the one that deletes.

> ⚠️ **Upgrading never rewrites a config you have already saved.** AstrBot only fills in defaults for keys that are missing, so a value already stored in your config file is never replaced by a new default.
>
> Early versions capped the library at just 100 stickers and let the background job prune the overflow on its own. If you have been upgrading in place, open the top of the config page and confirm the current number with your own eyes — especially right after migrating several hundred images from the old plugin.
