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
- **Sending** — "Send as QQ sticker" is on by default. `summary`, `sub_type` and `subType` are all written at once, so every adapter renders it correctly and quietly ignores the keys it does not know; turn the setting off to send an ordinary image.

This assumes OneBot's `messageFormat` is `array` (the default). Set it to `string` and the raw message becomes one CQ-code string, at which point marketplace stickers cannot be read at all.

QQ Official has no OneBot sticker flag. The separate sticker collection setting applies to both QQ Official and Webhook. Telegram and other platforms retain their own image or sticker delivery.

## QQ Official: text above the sticker

In Meme Thief's sending settings, choose **Attach to reply** and enable **QQ Official: text above sticker** (enabled by default). Ordinary QQ media captions place the image first. This mode sends the reply as native Markdown with selectable text followed by the sticker in the same message.

Images are uploaded to QQ native media first, without requiring an image host. Uploads for identical content and bot identity share a cached reference and concurrent uploads are coalesced. Original images and GIF animation are retained; the existing GIF conversion is used when Send as GIF is enabled. If native upload is unavailable, the plugin tries an installed, configured ImgBed Ferry that permits integration uploads, then AstrBot's file callback service. If all fail, the ordinary image path remains available and the reason is logged.

With [OutputProSplit](https://github.com/Whereis-Alice/astrbot_plugin_outputpro_split), the sticker stays an image component until text splitting is complete, then becomes a Markdown image reference. Image syntax is never split. Set the splitter's sticker strategy to **Inline** or **Follow previous** to place it below the final text segment, including replies ending in punctuation. **Standalone** still sends it separately. The text segment soft limit preserves standalone image boundaries.

The mode also works without a splitter. Already streamed text and replies rendered as a long image retain the existing separate sticker delivery. Turning off the setting restores ordinary QQ media; OneBot and Telegram are unaffected.

## Working with message-splitting plugins

With a plugin like [astrbot_plugin_outputpro_split](https://github.com/Whereis-Alice/astrbot_plugin_outputpro_split) installed — the kind that chops one long reply into several messages — this plugin's sticker is by default **a separate message**, invisible to the splitter and therefore outside its image and sticker layout rules. Switching "auto sticker delivery mode" to `attach` fixes that:

| Value | Behaviour | Use when |
|:---|:---|:---|
| `separate` (default) | The sticker is its own message, exactly as before | No splitter installed, or you want the safest compatibility |
| `attach` | The sticker is appended to the end of the reply's message chain | You want the splitter to lay out text and sticker together |

The cost of `attach` is that selection has to finish before the reply goes out, so replies get slightly slower. A timeout guards this (10 s by default): on timeout, on no match, on any failure at all, it falls back to `separate` asynchronous delivery — **the reply itself is never blocked, and the worst case is a sticker that arrives a moment late**. `attach` also stands down when the reply is about to be rendered as one long image (AstrBot's text-to-image), since a sticker sitting at the end of the chain would be drawn into the picture.

### What is that "compat path" switch for

Current `outputpro_split` recognises Meme Thief's own image paths, so the compat path is generally unnecessary. Older splitters that only recognise `plugin_stealer` can still use it.

Turn on "emit compat path for splitters" and the sticker about to be attached is first hard-linked (copied if the link fails, e.g. across volumes) into a compat directory whose name contains that string, and attached from there, which the splitter recognises correctly. The original file and the path recorded in the library are untouched, and the compat directory keeps only the 64 most recent files, so disk use is negligible.

The compat path only helps older splitters recognise stickers; it does not control QQ's native media layout.
