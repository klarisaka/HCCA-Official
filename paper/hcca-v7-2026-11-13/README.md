# HCCA for Token-Efficient Repository Repair

Technical report, November 13, 2026.

[Read the paper](main.pdf) · [中文摘要](ABSTRACT.zh-CN.md) · [Audited results](../../reports/verified50-dsh-minimal-low-2026-11-13-summary.json)

The report compares frozen HCCA v7 low, a new DSH minimal low run, and the historical DSH minimal high run on the same 50 Verified Mini tasks. HCCA resolves 36 tasks at $0.644212152 normalized cost, DSH low resolves 34 at $0.553843638, and DSH high resolves 35 at a known-complete cost of at least $3.062054268. The new DSH low run uses the official API and four concurrent tasks. The historical runs use GXWTF and two concurrent tasks.

The official DSH low run has 1,061 complete requests. The current testing attempt also includes six failed GXWTF requests with unknown usage, making the entire testing expenditure unknown. Common off-peak normalization and actual provider billing are recorded separately.

The November 12 report remains available in [its dated directory](../hcca-v7-2026-11-12/README.md). The editable source is [main.tex](main.tex), with audited values in [metrics.tex](metrics.tex) and the vector chart in `figures/`.

Build with XeLaTeX from this directory.

```sh
xelatex -interaction=nonstopmode -halt-on-error main.tex
xelatex -interaction=nonstopmode -halt-on-error main.tex
```
