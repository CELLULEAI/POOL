# Connect your tools to Cellule.ai

Cellule.ai exposes an **OpenAI-compatible API**. Any tool that can talk to
OpenAI can talk to the Cellule pool — point it at the base URL below and pick a
model. This is the short onboarding card; for the full endpoint reference see
[`API.md`](../API.md).

## TL;DR

| | |
|---|---|
| **Base URL** | `https://cellule.ai/v1` |
| **Auth** | `Authorization: Bearer acc_xxxxxxxx` (your personal key) |
| **Model for tools / integrations** | `iamine/raw` — *stateless, recommended* |
| **Model for personal chat with memory** | `iamine` — *stateful* |

## 1. Get a key

Create an account at **https://cellule.ai** — your personal key looks like
`acc_xxxxxxxx`. Send it as `Authorization: Bearer acc_xxxxxxxx`.

No account yet? Guests get **20 free requests per IP**, after which a key is
required.

## 2. Pick the right model (the key point)

`/v1/models` advertises two public models:

| Model | Behavior | Use it for |
|---|---|---|
| `iamine` | **stateful** — long-term memory is injected, answers are rich but can be noisy for automated callers | personal chat where you *want* memory |
| `iamine/raw` | **stateless** — no memory, no assist pipeline, a clean/deterministic OpenAI reply | **integrations & tools (recommended)** |

You can also force stateless mode on any model with the header
`X-Iamine-Stateless: 1` (accepted values: `1`, `true`, `yes`, `on`). Both
`iamine/raw` and the header bypass long-term memory injection and the assist
pipeline (sub-agents / auto-review / sticky conversation id), returning a plain
OpenAI response built only from the messages you send.

> Why this matters: stateless clients (Nextcloud `integration_openai`, Open
> WebUI, ...) otherwise receive memory-augmented output (extra `follow_ups`,
> inflated prompt tokens, occasional "roles must alternate"). `iamine/raw` fixes
> that.

## 3. Quick test

```bash
curl -s https://cellule.ai/v1/chat/completions \
  -H "Authorization: Bearer acc_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"iamine/raw","messages":[{"role":"user","content":"Say hello in one sentence."}]}'
```

## 4. Connect common clients

### Nextcloud — `integration_openai` app
- Service URL: `https://cellule.ai/v1`
- API key: `acc_xxxxxxxx`
- Default model: `iamine/raw`
- **Known pitfall:** after changing the model, Nextcloud's Redis cache may pin
  the old model list and you get *"Une erreur s'est produite au moment de
  planifier la tâche"*. Fix: flush the Redis keys matching `*integration_openai*`
  (`redis-cli --scan --pattern '*integration_openai*' | xargs redis-cli del`),
  then reload.

### Open WebUI
- Settings → Connections → OpenAI API
- Base URL: `https://cellule.ai/v1`, API key: `acc_xxxxxxxx`
- Pick `iamine/raw` from the model list.

### Python (`openai` SDK)
```python
from openai import OpenAI

client = OpenAI(base_url="https://cellule.ai/v1", api_key="acc_xxxxxxxx")
r = client.chat.completions.create(
    model="iamine/raw",
    messages=[{"role": "user", "content": "Summarize: ..."}],
)
print(r.choices[0].message.content)
```

### Anthropic-compatible clients (Claude Code, Anthropic SDK)
The pool also speaks the Anthropic message format at `/v1/messages`:
```bash
export ANTHROPIC_BASE_URL="https://cellule.ai"
export ANTHROPIC_API_KEY="acc_xxxxxxxx"
claude --model iamine
```

## 5. Limits & good citizenship

- **Guests:** 20 requests per IP, then create a (free) account.
- **Rate limiting:** the pool throttles abusive sources — keep automated callers
  reasonable; back off on HTTP `429`.
- **Discoverability:** call `GET /v1/models` to see what the pool currently
  serves. Most OpenAI clients read their model dropdown from this list.

---

*Cellule.ai is a community-run, decentralized AI inference network (AGPLv3). The
compute comes from volunteer machines — be kind to the pool. Full API:
[`API.md`](../API.md).*
