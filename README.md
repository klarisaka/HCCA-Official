**English** | [简体中文](README.zh-CN.md)

# HCCA Official

HCCA Official 0.2.1 gives reading, execution, and retries their own contexts. Keep the main agent's input small and reuse stable prompt prefixes.

## How it works

Root plans and integrates the work. Each delegated task starts with a fresh context and shared role prompts, keeping tool output and retry history local to the worker.

```mermaid
flowchart LR
    U["You"] --> R["Root"]
    R -->|delegate| ER["Executor"]
    ER -->|result + evidence| R
    R -->|analysis task| A["Analyst"]
    A -->|findings| R
    A -->|delegate| EA["Executor"]
    EA -->|result + evidence| A
    R -->|read| RR["Reader"]
    RR -->|findings| R
```

Each Executor box represents an independent assignment using the shared role configuration. Results return to the parent that requested the task.

| Mode | Who plans the work | Start here |
| --- | --- | --- |
| DSH plugin | Your DSH chat acts as Root | Install the plugin and select HCCA |
| Standalone | HCCA runs its own Root | Use the command below |

## Frozen v7 benchmark

SWE-bench Verified Mini, 50 tasks. HCCA has 49 official evaluations and one unfinished task counted as unresolved. Both DSH runs evaluated all 50 tasks.

| Frozen run | Resolved | Input tokens | Cached input | Output tokens | Normalized cost |
| --- | --- | --- | --- | --- | --- |
| **HCCA v7 · low** | **36/50 (72%)** | **36.811M** | **35.850M** | **0.654M** | **$0.644** |
| DSH minimal · low | 34/50 (68%) | 29.254M | 28.190M | 0.516M | $0.554 |
| DSH minimal · high | 35/50 (70%) | ≥ 106.265M | ≥ 104.137M | ≥ 4.050M | ≥ $3.062 |

The measured HCCA run recorded 1,301 `low` requests and zero `high` requests. Its role policy configures thinking/planning and analysts at `high`, and execution/reading at `low`.

DSH low used DeepSeek official with four concurrent tasks. The historical HCCA low and DSH high runs used GXWTF with two concurrent tasks. All costs use common off-peak token rates. Six failed GXWTF requests before the official low run have unknown usage and cost.

HCCA resolves two more tasks than DSH low at 16.3% higher normalized cost. These figures describe the frozen v7 benchmark runner. HCCA Official 0.2.1 packages the current Windows and DSH runtime. [Benchmark details](docs/BENCHMARK-v7-2026-11-13.md) · [Paper](paper/hcca-v7-2026-11-13/main.pdf).

## Use with DSH

You need Python 3.11+, the DSH CLI, and pnpm. Install from the full repository:

```sh
git clone https://github.com/klarisaka/HCCA-Official.git
cd HCCA-Official
python integrations/dsh-hcca/install.py --profile web
```

For the desktop app, replace `web` with `desktop`. [Configure the HCCA worker model](docs/USAGE.md#model-connection), restart DSH, and select **HCCA** in a new chat.

Describe your task in chat. Root delegates reading, execution, or analysis as needed. Executors can adjust their approach and retry after failures. The **HCCA tab** shows worker progress, token usage, cache hit rate, and results.

Send follow-up instructions in the same chat while work is running. Root decides what needs to change and passes relevant updates down the task hierarchy.

## Run standalone

After [configuring a model](docs/USAGE.md#model-connection), run from the repository root with your workspace path:

```sh
python -m hcca.runner --goal "Complete your task" --workspace "/path/to/project" --out "./runs/example"
```

Open a dashboard for that run:

```sh
python -m hcca.dsh_worker --runs "./runs" --port 8765
```

Visit <http://127.0.0.1:8765/>.

## Learn more

[Usage and configuration](docs/USAGE.md) · [DSH integration](integrations/dsh-hcca/README.md) · [Architecture](docs/ARCHITECTURE.md) · [Benchmark harness](benchmarks/README.md) · [Past experiments](docs/USAGE.md#past-experiments)

## License

HCCA's original code is licensed under [AGPL-3.0-only](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md) for external materials.
