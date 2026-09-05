# FAQ

> [← Back to README](../../README_EN.md) · [Docs index](README.md) · [中文](../faq.md)

**Do I have to uninstall astrbot_plugin_stealer?**
Not technically. The two plugins use different data directories, command prefixes and WebUI routes, so they coexist fine. But **both will collect and send**, so you would get duplicates. After migrating, turn off the old plugin's collect/send switches or uninstall it.

**Can migration damage the old plugin's data?**
`apply` only reads from the old directory and writes to the new one — not a byte of the original changes, so you can always fall back. Only `move` relocates files, and that one is irreversible.

**Images are being collected but have no tags. Why?**
Almost always a vision-model problem. Check the AstrBot log for errors and confirm the vision model setting or global captioning model works. Untagged images already in the library can be fixed with batch re-analysis.

**Batch analysis keeps returning 429.**
Drop concurrency to 1 and RPM to 6–10. If it still throttles, your quota is genuinely tight: use the item limit to process a few dozen at a time.

**My server only has 1GB (or 512MB) of RAM. Will analysing hundreds of images blow it up?**
No, but keep concurrency low. Peak memory per image is now decoupled from image size: a 512-square 60-frame animation takes about 17MB, a 1920×480 120-frame animation about 22MB, a 4000×4000 still about 45MB. A batch peaks at roughly *per-image peak × concurrency*, so on a small box leave batch concurrency at the default 2 or drop it to 1 — hundreds of images will just take longer, not run you out of memory. Also note that “send as GIF” adds one more encode at send time, so leave it off when memory is tight.

**Stickers come too often / too rarely.**
Adjust the send probability. If they arrive at odd moments, make sure the intent gate is on.

**Is an embedding model required?**
No. Without one it uses BM25 keyword search; only cases where wording differs but meaning matches will be weaker.

**I edited a description but search still returns the old text?**
It should not happen — editing a description updates the vector immediately, and startup tops up anything stale by text fingerprint. If you do hit it (misaligned vectors left behind in older data), run `/mp rebuild_vectors` once.

**What is the sticker limit? Will it delete my images?**
`2000` by default, `0` means unlimited. By default going over the cap **only warns and deletes nothing** — you have to run `/mp capacity` yourself, or turn on "auto-clean over capacity".

**Dozens of stickers disappeared without a word?**
Grep the log for `容量控制` first, then open the "Capacity and safety" group at the very top of the plugin config page and look at "max stickers". Early versions defaulted that cap to only 100; the hourly background job pruned everything above it — image files included — and logged a single INFO line that is easy to miss, which is why people who migrated several hundred images from the old plugin hit this most often. The default is now 2000 and overflow only warns, **but upgrading never rewrites a value you already saved**, so you still have to check that number yourself. See [Library capacity cap](configuration.md#library-capacity-cap).

**Will it collect someone's private screenshot?**
Yes — it has no idea what privacy is. That is why human review is on by default, why there is a `local` scope (sendable only in the chat it came from), and why there is a blocklist. Keep review enabled and use the collect blocklist to exclude sensitive chats.

**Marketplace stickers are not being collected on LLBot / NapCat / SnowLuma?**
First check that OneBot's `messageFormat` is `array` and not `string` — the plugin needs raw message segments to see marketplace stickers at all. The differences between the three adapters themselves are already handled; details in [Platform and protocol adapters](platforms.md#platform-and-protocol-adapters).

**Stickers look out of place now that I run a message-splitting plugin?**
Set "auto sticker delivery mode" to `attach`, and if needed also turn on "emit compat path for splitters". See [Working with message-splitting plugins](platforms.md#working-with-message-splitting-plugins).
