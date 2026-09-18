# GridWise live API verification

Completed: 2026-09-18 14:10 UTC (20:10 Asia/Dhaka).
Includes both local and deployed Render verification with real provider calls.
No keys or raw provider error bodies are included in this report.

## Final configuration

- Primary: Groq, `openai/gpt-oss-20b`, low reasoning effort, 2048-token output budget.
- Fallback: Gemini, `gemini-3.1-flash-lite`, minimal reasoning effort.
- Primary attempt limit: 8 seconds. Final provider uses the remaining shared 17-second LLM budget.
- Solver limit: 10 seconds. Overall API deadline: 28 seconds.
- Credentials are stored only in ignored `.env`, with owner-only permissions (0600).

## Deployed API verification (Render)

| Check | Result | Observed p95 | Maximum |
|---|---|---:|---:|
| All 10 public cases, 2 rounds, deployed Render | 20/20 passed | 5.402 s | 9.165 s |
| Health endpoint | HTTP 200, `{"status":"ok"}` | — | 0.3 s |
| Malformed/missing input | HTTP 400 | — | — |

Base URL: `https://bup-hackathon-preli-du-algoarchitects.onrender.com`

Every deployed case returned HTTP 200, matched organizer directive semantics, passed
independent replay against organizer ground truth, matched reference cost, and
returned consistent totals and response schema. Equivalent optimal schedules were
accepted.

## Local verification results

| Check | Result | Observed p95 | Maximum |
|---|---|---:|---:|
| Groq-primary configuration, all 10 cases repeated twice | 20/20 passed | 3.745 s | 4.927 s |
| Gemini independently, all 10 cases | 10/10 passed | 10.109 s | 10.109 s |
| Forced Groq failure followed by real Gemini, SAMPLE-05 | Passed, HTTP 200 | Single request | 8.036 s |
| Offline unit/integration tests | 45 passed | N/A | N/A |

During the two-round configured run, Groq returned HTTP 429 on five requests.
All five requests completed successfully through Gemini fallback. The 20/20 result
therefore describes the combined provider configuration, not Groq alone.

Health returned HTTP 200 with the expected JSON. Malformed JSON and missing fields
returned HTTP 400.

## Issues found and corrected

The original Groq Llama and Gemini 2.5 Flash-Lite defaults returned HTTP 404 on
actual generation calls. Model listing alone was insufficient to prove generation
availability. Replaced both with models verified through generation calls.

Gemini initially exceeded the 8-second per-attempt deadline in three of ten cases.
Explicit minimal reasoning and allowing the final provider to use the remaining
shared budget produced the successful final 10/10 run. Earlier exploratory runs
also encountered a transient Gemini 503. External provider availability is not
guaranteed by the final passing run.

## Scope and remaining work

These are small local and deployed samples, not load-test benchmarks. Gemini is
slower than the primary provider. Provider quota/rate limits and latency may vary.
The default provider order remains `groq,gemini`.

Never commit `.env`. Configure credentials through hosting secrets (Render
environment variables). GitHub Actions CI builds the Docker image and publishes
to GHCR on main-branch pushes. Render deploys from the same main branch.
