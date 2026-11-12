**English** | [简体中文](BENCHMARK-v7-2026-11-12.zh-CN.md) · [Project home](../README.md)

# Frozen v7 benchmark

Published on November 12, 2026. The community dataset `MariusHobbhahn/swe-bench-verified-mini` supplies all 50 tasks and the fixed denominator. HCCA v7 resolved 36/50 (72%). It has 49 official evaluation records and one unfinished task counted as unresolved. The reused frozen DSH baseline resolved 35/50 (70%), with all 50 tasks evaluated.

## Results and usage

| Metric | HCCA frozen v7 | DSH minimal high, frozen baseline |
| --- | --- | --- |
| Resolved | 36/50 (72%) | 35/50 (70%) |
| Officially evaluated | 49 | 50 |
| Unfinished, counted unresolved | 1 | 0 |
| Actual request attempts | 1,301 | 2,014 |
| Complete usage records | 1,301 | 2,013 |
| Incomplete usage records | 0 | 1 |
| Recorded input tokens | 36,810,516 | 106,265,446 known-complete |
| Recorded cached input tokens | 35,849,984 | 104,137,856 known-complete |
| Recorded uncached input tokens | 960,532 | 2,127,590 known-complete |
| Recorded output tokens | 654,304 | 4,050,837 known-complete |
| Reasoning tokens within output | 408,903 | 3,690,753 known-complete |
| Other output tokens | 245,401 | 360,084 known-complete |
| Cached share of recorded input | 97.3906% | 97.9979% |
| Normalized cost, USD | $0.644212152 | ≥ $3.062054268 |

DSH's token counts cover its complete usage records. One partial record makes its total tokens and normalized cost lower bounds. Output includes reasoning tokens, counted once. Usage totals cover all actual model requests in the respective frozen runs. [Machine-readable summary](https://github.com/klarisaka/HCCA/blob/5899faf6fefa2babae641d46ada98e8f60aa2670/reports/verified50-hcca-v7-vs-dsh-2026-11-12-summary.json) · [Per-task outcomes](https://github.com/klarisaka/HCCA/blob/5899faf6fefa2babae641d46ada98e8f60aa2670/reports/verified50-hcca-v7-vs-dsh-2026-11-12-tasks.json).

Paired outcomes are 33 tasks resolved by both, 3 by HCCA only, 2 by DSH only, and 12 by neither. The recorded reasoning field accounts for 96.6% of the measured output-token gap. Reasoning effort differs between the two configurations.

The HCCA phase took approximately 60 minutes 9.6 seconds with two concurrent tasks, including generation and official evaluation.

## Configuration

| Setting | HCCA frozen v7 | DSH frozen baseline |
| --- | --- | --- |
| Provider | GXWTF | GXWTF |
| Configured model alias | `deepseek-v4.1-flash` | `deepseek-v4.1-flash` |
| Concurrent benchmark tasks | 2 | 2 |
| Execution reasoning effort | `low` | `high` |
| Execution path | Direct executor, initial planning bypassed | `sdk-minimal` |
| Run | Frozen v7 | Reused frozen `sdk-minimal` baseline |

Earlier runtime versions were developed and evaluated on this same manifest. This single trial describes these frozen configurations as a development-benchmark measurement. Reasoning effort, prompts, tool behavior, and execution policy vary together, limiting causal attribution to architecture. Reusing the DSH run preserves the same comparator across versioned HCCA experiments.

HCCA Official 0.2.1 packages the current Windows and DSH runtime. The figures above describe the frozen v7 benchmark runner. Current installation and configuration instructions are maintained in [HCCA Official](https://github.com/klarisaka/HCCA-Official).

## Cost accounting

All totals use common per-million-token rates of $0.003 for cached input, $0.15 for uncached input, and $0.60 for output.

```text
USD = (cached input × 0.003 + uncached input × 0.15 + output × 0.60) / 1,000,000
```

These rates normalize the comparison. Actual provider charges follow the provider's billing. The cached-token totals are charged at the cached-input rate. [Pricing reference](https://api-docs.deepseek.com/quick_start/pricing/).

## Paper and historical results

- [Paper PDF](https://github.com/klarisaka/HCCA/blob/5899faf6fefa2babae641d46ada98e8f60aa2670/paper/hcca-v7-2026-11-12/main.pdf), [LaTeX source](https://github.com/klarisaka/HCCA/blob/5899faf6fefa2babae641d46ada98e8f60aa2670/paper/hcca-v7-2026-11-12/main.tex), and [Chinese abstract](https://github.com/klarisaka/HCCA/blob/5899faf6fefa2babae641d46ada98e8f60aa2670/paper/hcca-v7-2026-11-12/ABSTRACT.zh-CN.md).
- Historical frozen v6 resolved 38/50 (76%) at $0.738707166 normalized cost. It used DeepSeek official with eight concurrent tasks. Its configuration and usage remain in the versioned record. [Historical v6 summary](https://github.com/klarisaka/HCCA/blob/d337069fa8b81851d2086482992edd560244f4b6/reports/verified50-hcca-v6-vs-dsh-summary.json).
- [Usage and configuration](USAGE.md) · [Past experiments](USAGE.md#past-experiments).
