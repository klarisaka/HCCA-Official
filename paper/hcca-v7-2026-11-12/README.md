# HCCA for Token-Efficient Repository Repair

Technical report, November 12, 2026.

[Read the paper](main.pdf) · [中文摘要](ABSTRACT.zh-CN.md) · [Audited results](../../reports/verified50-hcca-v7-vs-dsh-2026-11-12-summary.json)

The paper reports the frozen HCCA v7 configuration with direct execution and low reasoning effort on all 50 tasks of the community-selected Verified Mini dataset, with 1,301 low-effort requests and zero high-effort requests. The reported comparator is the completed DSH minimal-high baseline. One unfinished HCCA task receives zero credit and all its usage remains included.

The editable source is [main.tex](main.tex), with audited figures in [metrics.tex](metrics.tex) and the vector chart in `figures/`.

Build with XeLaTeX from this directory. TeX Live supplies the TeX Gyre and STIX Two fonts and the required packages.

```sh
xelatex -interaction=nonstopmode -halt-on-error main.tex
xelatex -interaction=nonstopmode -halt-on-error main.tex
```
