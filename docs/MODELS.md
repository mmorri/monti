# Subscription model survey (2026-10-06)

Exact model IDs per subscription, so tier pools never guess. Sources are
ranked: **live catalog** (`monti models` against your own logins) beats
**vendor docs**, which beats third-party references. Anything marked
*verify* must be confirmed via `monti models` after logging in — an
unverified pool entry fails its tier fast instead of rotating, so unverified
IDs stay commented out in `config.yaml` until confirmed.

Benchmarks: SWE-bench Verified is frozen since Feb 2026 (quoted for older
models only); current signal is Terminal-Bench 4.0 / DeepSWE v1.1 /
Artificial-Analysis Coding Agent Index. See the 2026-10-05 research notes in
chat history for the full table.

## kimi (kimi.ai) — live catalog ✅

`k3`, `k3-256k`, `kimi-for-coding`, `kimi-for-coding-highspeed`.
K3: 93.4% Verified, 69% DeepSWE. Coding-tuned `kimi-for-coding` (~80% class)
is the moderate workhorse; `-highspeed` the everyday lane.

## muse (Meta Muse Code) — live catalog ✅

`spark` line `muse-spark-1.1` → `1.3` (+`-contributor` cheap tiers),
`muse-image-1.0`, `muse-voice-transcribe-1.0`, `sam-3.1`.
Spark 1.3: 75.4% DeepSWE (vendor; field-leading), ~72 TB2.1 (Vals),
~20% fewer tool calls than 1.2, 1M context. Legit high/very_high wallet.

## anthropic (Claude Pro/Max) — vendor docs ✅

Pro/Max share one catalog (allowance differs). Exact API IDs from
platform.claude.com/docs (all pinned snapshots, none sooner than Sep 2027):

| ID | Use | Bench |
|---|---|---|
| `claude-fable-5-1` | very_high: long-horizon reasoning | TB4 57.9 (as Fable 5.1) |
| `claude-opus-5-5` | very_high: daily-driver frontier | CAI 66 (#1), TB4 53.9 (as Opus 5) |
| `claude-sonnet-5-5` | high: speed+intelligence balance | — |
| `claude-haiku-4-5` | moderate: fastest, near-frontier | 73.3 Verified, live 200 on this account 2026-10-06 |

Legacy IDs (`opus-4.x`, `sonnet-4.x`, `sonnet-5` without `-5`) still resolve
but are superseded — `claude-sonnet-4-5` (our old default) is stale.

## z.ai (GLM Coding Plan) — vendor docs ✅

The plan serves **exactly two models**, nothing else:
`glm-5.3` (flagship, 95.4% Verified, 69% DeepSWE) and `glm-5.3-flash`
(92.0% Verified, 3× quota). Requests naming older IDs are silently
re-routed server-side (5.2/5.1→5.3, 4.7→flash) — always pin the real IDs.
Off-peak (incl. all weekend) costs 50% points.

## openai (ChatGPT/Codex subscription) — vendor docs ✅

API names from OpenAI docs; availability varies by plan (Plus has limited
Astra) and Codex CLI version (Astra needs ≥0.153.0):

| ID | Use | Bench |
|---|---|---|
| `gpt-6-astra` | very_high: hardest end-to-end work | TB4 **58.2** 🥇, DeepSWE 74% |
| `gpt-6-sol` | high: everyday coding at 1/5 Astra | TB4 43%, DeepSWE 68.8% |
| `gpt-6.1-sol` | high alt (15–160 msgs/5h on Plus) | — |
| `gpt-6-luna` | ⚠️ single-pass tasks only | TB4 **13%** — collapses on agent loops, keep out of pools |
| `gpt-5.6-sol` / `-terra` / `-luna` | legacy, still served | — |

`gpt-5.5` retires from Codex sign-in on **2026-10-14** — replace with
`gpt-6-sol`/`gpt-6-luna` per OpenAI's migration note.

## windsurf (Cognition) — reference snapshot, VERIFY live ⚠️

`opencode-windsurf-auth` documents 94 IDs, incl. `claude-opus-4.7`,
`gpt-5.5`, `gemini-3.5-flash`, `kimi-k2.6`, `deepseek-v4`, `swe-1.6`
(+`:variant` suffixes like `:high`, `:thinking`, `-fast`).
No Fable 5.x / Opus 5.x / GPT-6 in that snapshot, and the per-account
catalog is authoritative — **run `monti models` after login** and promote
confirmed UIDs into the pools. Guessed form `claude-fable-5-1` is
unconfirmed (real UIDs look like `claude-opus-4-7-medium`).

## copilot (GitHub Copilot) — vendor docs, not in default pools

30+ models incl. Fable 5.1, Opus 5, Sonnet 5, GPT-5.5/5.6 family, Gemini
flashes, Grok 4.5/4.6, Kimi K3. Usable today via
`monti --model copilot/<id from catalog>` / `/model`; not pooled by default
(pending your call on which wallet should burn first).

## cursor — chat not ported

Auth works; chat fails closed (decision record in docs/TRANSPORTS.md).
No pool entries until a transport exists.

## Suggested pool map (mirrors config.yaml)

| Tier | Wallets in burn order |
|---|---|
| everyday | zai/flash → kimi/highspeed |
| moderate | kimi/for-coding → windsurf/gpt-5.5 (+ anthropic/haiku-4-5 once verified) |
| high | zai/5.3 → anthropic/sonnet-5-5 → kimi/k3 → muse/spark-1.3 → openai/sol |
| very_high | anthropic/fable-5-1 → anthropic/opus-5-5 → openai/astra → muse/spark-1.3 (+ windsurf/fable once verified) |
