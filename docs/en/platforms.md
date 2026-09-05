# Platform and protocol adapters

> [← Back to README](../../README_EN.md) · [Docs index](README.md) · [中文](../platforms.md)

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
