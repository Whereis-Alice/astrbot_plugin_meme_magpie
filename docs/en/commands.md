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
