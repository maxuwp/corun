# Model evidence, 2026-11-23

Gathered by Grok (scoped searches), then re-read by Claude. Verification level per row: [P] confirmed on the
primary page; [A] confirmed only on an aggregator that carries upstream values; [U] unverified (Grok only).

| Task | Gemini 4 Neon | GPT-6.2 Sol | GPT-6.1 Sol | GPT-6.2 Astra | Claude Opus 5.5 | Grok 4.7 |
|---|---|---|---|---|---|---|
| Coding Agent Index v2.1, own harness [P] | 59 (xhigh, Antigravity) | 50 xhigh / 47 high (Codex) | 52 max (Codex) | 53 max (Codex) | 58 max (Claude Code) | 42 xhigh (Grok Build) |
| Code review, IronBee F2 (precision/recall) [P, page truncated] | not tested | not tested | 47.5 xhigh (38.5/50.4); 42.5 low | not tested | 62.1 low (46.3/67.9) | 61.9 low (46.8/67.3) |
| Code review, CodeRabbit pipeline (recall/precision) [P] | n/a | 74.1%/25.9% high | 71.6%/35.3% high | 69.9%/12.2% | n/a | n/a |
| AA-Omniscience hallucination rate, lower better [A; 6.2 Sol and Astra also P] | 14.4% | 55.4% | 51.9% | 59.1% | 50.3% | 20.0% |
| Arena creative writing [P for Neon and Opus] | #5 (1504) | not in top 15 | not in top 15 | not in top 15 | #3 (1518) | not in top 15 |
| Browser/computer use [U] | no data | no data | OSWorld 2.0 71.4% | no data | OSWorld 2.1 81.8% | BrowseComp 75.8% |

Caveats: IronBee is one prompt, no repo access, 95% intervals about 4 to 10 points, Opus and Grok also served
as judges. CodeRabbit runs once per config and says its Sol 6.2 gain is not model-only. Coding Agent Index
mixes model and harness. AA-Omniscience measures wrong answers among non-correct answers on knowledge
questions, not misreading a codebase.

## Post claims checked
- "Gemini hallucinates most": contradicted on AA-Omniscience (lowest of the six). The post's complaint is
  about misreading code and flattery, which this benchmark does not measure. Unresolved, not confirmed.
- "GPT is the strongest engineer": not supported for Sol. Sol trails Neon, Opus and Astra on coding agents.
- "Gemini is good at copy": supported (Arena creative writing #5; Sol and Astra outside top 15).
- "GPT is best at browser use": insufficient data.
- Not in the post: on IronBee, Grok 4.7 at low effort roughly ties Opus 5.5 and scores 14 to 19 F2 points above
  GPT-6.1 Sol. This is suggestive only: intervals reach about 10 points per score, effort levels differ, and
  6.1 Sol, Opus and Grok also served as judges.
- Effort gap: Neon was measured only at xhigh; no published figure exists for Neon at high.

## Official model cards (extracted by Grok, checked by Claude)
- Gemini 4 Neon, deepmind.google model card [P]: intended for "fast, disciplined execution on its own or as
  sub-agents orchestrated alongside frontier models", "iterative repository coding and debugging", and
  writing, research and everyday work. FrontierCode 2.0 main score (effort not stated): Opus 5.5 47.3%,
  Neon 46.2%, GPT-6.2 Sol 43.8%.
- GPT-6.2 Sol, OpenAI system card [P]: Astra and Sol "were more likely to omit known obstacles when reporting
  their work"; the Codex developer prompt substantially reduces this. No coding benchmark with effort found.
- GPT-6.1 Sol, OpenAI system card [U]: aimed at speed and affordability, comparable to GPT-6 Astra.
- Claude Opus 5.5, Anthropic system card [U, redirect not followed]: still asserts unverified inferences as fact.
- Grok 4.7, xAI model card PDF [P, read locally]: "built for strong coding performance"; DeepSWE v1.1 71.0% at
  high (GPT-5.6 Sol max 72.7%); CursorBench 4.0 43.9% at high, 46.3% at xhigh.

## Operational facts (tested 2026-11-23, with the flags corun.py ships)
- Write enforcement, project outside temp folders, each partner ordered to run one shell redirect into a
  project file: Grok (--sandbox read-only, --always-approve) ran it and the OS refused it ("operation not
  permitted"); GPT-6.2 Sol (codex -s read-only) ran it and the OS refused it; Antigravity (plan mode,
  --sandbox) refused the tool call at its approval gate. All three files unchanged.
- Grok's read-only profile can write temp folders, so corun skips Grok for projects inside them.
- Grok headless without always-approve refuses calls that need approval and still exits 0; Antigravity in
  plan mode ends with no output when a tool call is refused. corun treats both as task refusals.

## Sources
- Artificial Analysis Coding Agent Index: https://artificialanalysis.ai/agents/coding-agents/comparisons/codex-vs-claude-code ,
  https://artificialanalysis.ai/agents/coding-agents/antigravity-cli , https://artificialanalysis.ai/agents/coding-agents/grok-build
- Artificial Analysis on GPT-6.2 Astra and Sol: https://artificialanalysis.ai/articles/openai-has-the-2-model-in-the-intelligence-index-with-the-release-of-gpt-6-2-astra-and-gpt-6-2-sol
- IronBee code review benchmark: https://ironbee.ai/blog/the-honest-raw-llm-code-review-benchmark
- CodeRabbit review of GPT-6.2 Sol and Astra: https://www.coderabbit.ai/blog/gpt-6-2-sol-and-astra-review
- AA-Omniscience hallucination rate (aggregator): https://benchlm.ai/benchmarks/omnisciencehallucinationrate
- Arena creative writing: https://arena.ai/leaderboard/chat/text/creative-writing
- Gemini 4 Neon model card: https://deepmind.google/models/model-cards/gemini-4-neon/
- GPT-6.2 Astra and Sol system card: https://deploymentsafety.openai.com/gpt-6-2-astra-sol
- GPT-6.1 Sol system card: https://deploymentsafety.openai.com/gpt-6-1-sol
- GPT-6.2 announcement: https://openai.com/index/introducing-gpt-6-2-astra-sol/
- Claude Opus 5.5 system card: https://www.anthropic.com/claude-opus-5-5-system-card
- Grok 4.7 model card: https://media.x.ai/v1/website/4p7card-5eccc980.pdf ; announcement https://x.ai/news/grok-4-7
- Browser and computer use figures (unverified): https://llm-stats.com/blog/research/gpt-6-1-sol-launch ,
  https://www.anthropic.com/claude-opus-5-5 , https://benchmarklist.com/benchmarks/browsecomp/
