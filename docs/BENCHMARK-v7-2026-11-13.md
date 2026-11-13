**English** | [简体中文](BENCHMARK-v7-2026-11-13.zh-CN.md) · [Project home](../README.md)

# Frozen v7 and DSH low

Updated November 13, 2026. All three configurations use the same 50-task community SWE-bench Verified Mini manifest. HCCA's unfinished `sphinx-doc__sphinx-8721` receives zero credit. The completed HCCA and DSH high runs are reused.

## Results and usage

| Metric | HCCA v7 low | DSH minimal low | DSH minimal high |
| --- | --- | --- | --- |
| Resolved | 36/50 (72%) | 34/50 (68%) | 35/50 (70%) |
| Officially evaluated | 49 | 50 | 50 |
| Unfinished | 1 | 0 | 0 |
| Physical model requests | 1,301 | 1,061 | 2,014 |
| Financially complete records | 1,301 | 1,061 | 2,013 |
| Input tokens | 36,810,516 | 29,253,597 | ≥106,265,446 |
| Cached input | 35,849,984 | 28,189,696 | ≥104,137,856 |
| Uncached input | 960,532 | 1,063,901 | ≥2,127,590 |
| Output, including reasoning | 654,304 | 516,149 | ≥4,050,837 |
| Reasoning within output | 408,903 | 365,085 | ≥3,690,753 |
| Normalized cost, USD | $0.644212152 | $0.553843638 | ≥$3.062054268 |

HCCA resolves two more tasks than DSH low, with 16.3% higher normalized cost, 25.8% more input and 26.8% more output. The paired outcomes are 33 resolved by both, 3 by HCCA only, 1 by DSH low only, and 13 by neither. DSH low resolves `sphinx-doc__sphinx-8721`, the unfinished HCCA case. [Summary](../reports/verified50-dsh-minimal-low-2026-11-13-summary.json) · [Per-task results](../reports/verified50-dsh-minimal-low-2026-11-13-tasks.json).

Compared with the historical DSH high run, DSH low resolves one fewer task and has at least 81.9% lower normalized cost. Its output is 87.3% below the high run's known-complete output. HCCA's output exceeds DSH low by 138,155 tokens, of which 43,818 are reasoning tokens. The recorded reasoning difference contributes 31.7% of this output gap. [Historical high comparison](BENCHMARK-v7-2026-11-12.md).

## Recorded configuration

| Setting | HCCA v7 low | DSH minimal low | DSH minimal high |
| --- | --- | --- | --- |
| Provider | GXWTF | DeepSeek official | GXWTF |
| Model alias | `deepseek-v4.1-flash` | `deepseek-flash` | `deepseek-v4.1-flash` |
| Concurrent tasks | 2 | 4 | 2 |
| Run date, UTC | November 12 | November 13 | November 11 |
| Observed effort | 1,301 low, 0 high | 1,061 low, 0 high | high |
| Execution path | Direct executor | `sdk-minimal` | `sdk-minimal` |
| Generation budget per case | 4,800 s | 4,800 s | 4,800 s |
| Requested output cap | omitted | omitted | omitted |
| Temperature and seed | omitted | omitted | omitted |

The official models endpoint identifies `deepseek-flash` as DeepSeek-V4.1-Flash. Both DSH runs use SDK 0.1.5-rc.1 with the same fixed system text, bash tool schema, effective 1,000,000-token window and zero automatic compaction events. Their shared SDK adapter patch removes only output-cap serialization. Generation uses an isolated base-commit repository and the supplied issue.

HCCA's role policy configures thinking/planning and analysts at high effort and execution/reading at low. Its measured direct-executor path produced only low requests. The provider, alias, concurrency and dates differ across these runs, so the comparison is descriptive. The same manifest was used during earlier development.

## Cost and fallback accounting

Common per-million-token rates are $0.003 for cached input, $0.15 for uncached input and $0.60 for output. Reasoning is included within output once. All 1,061 official low requests returned HTTP 200 and complete input, cache and output counters. The official full50 normalized cost is exact under these rates. The historical high run has one incomplete record, so its totals remain lower bounds. Actual provider billing is unknown. [Pricing reference](https://api-docs.deepseek.com/quick_start/pricing/).

Before switching to the official provider, the first GXWTF task made six requests, all HTTP 500 with no usage. Those requests belong to this test's expenditure. Across the official run and fallback attempt, there are 1,067 requests with six financially incomplete records. The total normalized expenditure is unknown, with a known-complete lower bound of $0.553843638. Existing HCCA answers were preserved.

## Paper and previous report

[Updated paper](../paper/hcca-v7-2026-11-13/main.pdf) · [LaTeX source](../paper/hcca-v7-2026-11-13/main.tex) · [Chinese abstract](../paper/hcca-v7-2026-11-13/ABSTRACT.zh-CN.md) · [November 12 report](BENCHMARK-v7-2026-11-12.md).
