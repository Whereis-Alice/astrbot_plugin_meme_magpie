# LLM-driven sticker usage

> [← Back to README](../../README_EN.md) · [Docs index](README.md) · [中文](../llm-tools.md)

Three tools the model can call on its own, no command needed:

| Tool | Purpose |
|:---|:---|
| `magpie_search_meme` | Search for candidates and return category, work, character, scenes, scope, use count |
| `magpie_send_meme` | Send one of the candidates; failures come back with an explicit reason so the model can retry |
| `magpie_steal_meme` | Collect an image from the current message; leave `image_ref` empty to take the first image |

## Letting the model fill in the metadata

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

## Known facts go into the analysis prompt

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
