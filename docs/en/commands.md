# Commands

> [← Back to README](../../README_EN.md) · [Docs index](README.md) · [中文](../commands.md)

## About the command prefix

`/` is only AstrBot's factory-default wake prefix. You can change it to `!`, `#` or `.` in AstrBot's settings, or clear it entirely (then you simply send `mp status`). This documentation always spells commands with `/` — substitute whatever you actually use. In direct messages, or when you @ the bot, no prefix is needed at all.

Not sure which prefix is yours? Every command example the plugin prints in its own replies is rendered with **the prefix that is actually in effect**, so copying from a reply always works. Send `/mp help` once and it reprints the whole subcommand list using your real prefix.

## Three equivalent spellings

```
/mp status
/magpie status
/神偷 status
```

`mp` is the primary command group (meme + pilfer, two letters to type), `magpie` is an alias kept from the plugin's earlier name, and `神偷` is the Chinese alias. The three are exactly equivalent.

Command group `mp`, aliases `magpie` and `神偷`. The table lists subcommands only — prepend your wake prefix when you send them, e.g. `/mp status` (default prefix) or `!mp status` if you changed it to `!`.

## Available to everyone

| Command | Description |
|:---|:---|
| `status` | Runtime status and library statistics |
| `list [category] [per_page] [page]` | List collected stickers (10 per page, pages start at 1) |
| `emotion_stats` | Emotion-analysis statistics and current mode |
| `help` / `帮助` | Print the full subcommand list using your current wake prefix |

## Admin only

| Command | Description |
|:---|:---|
| `on` / `off` | Enable / disable collection |
| `auto_on` / `auto_off` | Enable / disable automatic sending |
| `偷 [character] [work]` | Collect all images the caller posts in this chat for 60 seconds; explicit labels take priority |
| `natural_analysis <on\|off>` | Switch between the two emotion-analysis modes |
| `clear_emotion_cache` | Clear the emotion-analysis cache |
| `delete <index\|filename>` | Delete a sticker |
| `blacklist <index\|filename>` | Delete and blocklist it so it is never collected again |
| `scope <index\|filename> <public\|local>` | Set scope; `local` restricts sending to the source chat |
| `clean` | Empty the `raw` staging directory; stickers already in the library are untouched |
| `capacity` | Run a capacity-control pass now. **This permanently deletes excess stickers using the selected strategy** (oldest first by default; favourites are kept; use `status` if you only want to read the numbers) |
| `tag_stats [N]` | Tag health check: frequent tags, noisy rare tags, untagged entries (N defaults to 15) |
| `rebuild_index` | Rebuild the retrieval index. Index only — it **never deletes images** |
| `rebuild_vectors` | Wipe and rebuild the semantic search vectors. Use it when you edited a description but search still returns the old one; says so and does nothing if no embedding model is configured |
| `migrate [check\|apply\|move] [path]` | Migrate data from astrbot_plugin_stealer |
| `group show` | Show the collect/send list configuration |
| `group <send\|steal> priority <wl\|bl>` | Which list wins when both match |
| `group <send\|steal> <wl\|bl> <add\|del\|clear> [group:<id>\|user:<id>]` | Manage the lists |

## A minute of collection with character and work labels

```text
/mp 偷
/mp 偷 Isla "Plastic Memories"
/mp 偷 character=Isla work="Plastic Memories"
/mp 偷 作品="Plastic Memories" 角色=Isla
```

Both labels are optional. One positional name means the character; to set only the work, use `work=name` or `作品=名称`. Quote names containing spaces. Named fields may be given in either order.

The window accepts every image the command caller posts in the current chat for 60 seconds, including multiple messages and multiple images per message, without a fixed item count. Each user and chat has a separate window. Running the command again refreshes that caller's window and labels; already accepted images keep the labels captured when they arrived.

Accepted images are queued using the batch-analysis concurrency and RPM settings. Processing continues after the window closes and may take longer than a minute. Explicit character and work labels take priority, while vision fills the remaining metadata. Forced collection bypasses automatic non-meme filtering and perceptual-similarity deduplication; exact-content duplicates and blocklisted images are still skipped.

If analysis fails or reaches the 90-second per-image budget, the downloaded image and explicit labels are stored in an available fallback category with an empty description. Find these entries through the WebUI analysis-failure scan and reanalyse later. The receipt confirms that images have been queued; a failed background download cannot be stored because no image was obtained, and its cause is recorded in the logs.
