English | [简体中文](USAGE.zh-CN.md) · [Project home](../README.md)

# Usage and configuration

This guide covers HCCA Official 0.2.2.

## Choose an entry point

| Entry point | Root | Typical use |
| --- | --- | --- |
| DSH plugin | Your DSH chat | Delegate reading, execution, and analysis from chat and add guidance while tasks run |
| Standalone HCCA | HCCA itself | Submit a task from the command line for HCCA to plan and complete |

Both entry points share reader, executor, and guidance capabilities. Readers inspect files, executors edit files and run commands or tests, and analysts reason about tasks and delegate further work. Shared role configurations live in `~/.hcca/agents` by default. Set `HCCA_AGENT_HOME` to use another location. Each task has its own context. Root and analysts can delegate to executors, receive the results, and plan the next step.

## Model connection

Set the endpoint, model, and API key in the environment that launches the application. DSH configures its Root model separately. These variables configure the HCCA workers.

Windows PowerShell

```powershell
$env:HCCA_LLM_BASE_URL = "https://ai.gxwtf.cn/v1"
$env:HCCA_LLM_MODEL = "deepseek-v4.1-flash"
$env:HCCA_LLM_API_KEY = "your API key"
$env:HCCA_LLM_VERIFY_TLS = "1"
```

Linux

```sh
export HCCA_LLM_BASE_URL="https://ai.gxwtf.cn/v1"
export HCCA_LLM_MODEL="deepseek-v4.1-flash"
export HCCA_LLM_API_KEY="your API key"
export HCCA_LLM_VERIFY_TLS="1"
```

Replace the endpoint, model, and key to use another provider. The standalone entry point connects to a real model when both the endpoint and key are configured, and uses ScriptedLLM otherwise. The generic client defaults to `deepseek-chat`. The DSH sidecar has compatibility defaults for GXWTF and `deepseek-v4.1-flash`. Explicit environment settings take precedence.

| Optional variable | Purpose |
| --- | --- |
| `HCCA_LLM_TIMEOUT` | Timeout for an individual network read |
| `HCCA_LLM_IDLE_TIMEOUT` | Maximum idle time while waiting for actual model output |
| `HCCA_LLM_CALL_TIMEOUT` | Absolute deadline for one call, with `0` leaving control to the task deadline |
| `HCCA_LLM_CONCURRENCY` | Model concurrency, with `0` applying no additional limit |

Requests omit output token caps. Reasoning effort uses `low` or `high`. The examples above explicitly enable TLS certificate verification.

### Connection troubleshooting

On November 12, 2026, GXWTF's expired certificate caused verified connections to fail before model requests. Provider certificate renewal restores the normal setting. If you choose the existing compatibility setting, set `HCCA_LLM_VERIFY_TLS=0` in the process environment that launches DSH or HCCA. This disables certificate validation for that process.

## Install the DSH plugin

Prepare Python 3.11+, the DSH CLI, and pnpm. Keep the complete HCCA repository and run this command from its root.

```sh
python integrations/dsh-hcca/install.py --profile web
```

Use `desktop` in place of `web` for the desktop app. Restart the corresponding DSH service or app, create a chat, and select the HCCA preset. The installer backs up and updates the target profile's HCCA configuration. The plugin loads its Python runtime from the complete repository.

Port, interpreter, and configuration path options are covered in the [DSH plugin guide](../integrations/dsh-hcca/README.md).

## Run standalone

Submit a task from the repository root, replacing the workspace and output paths. On Linux, use `python3` if needed.

```sh
python -m hcca.runner --goal "Complete your task" --workspace "/path/to/project" --out "./runs/example"
```

Open the dashboard for this run

```sh
python -m hcca.dsh_worker --runs "./runs" --port 8765
```

Visit the [local dashboard](http://127.0.0.1:8765/). The Python runtime uses the standard library. Development checks additionally require pytest.

### Defaults by entry point

| Setting | DSH workers | Standalone generic runner |
| --- | --- | --- |
| Planning and analysis effort | Analyst uses `high`, Root is configured in DSH | Inherits the client's default `high` |
| Execution and reading effort | Executor and reader use `low` | Inherits the client's default `high` |
| Execution rounds | No fixed round limit | 16 per stage, adjustable with `--max-tool-rounds` |
| Reading rounds | 8 | 8 |
| Executor soft context budget | 524,288 tokens | Unlimited execution mode defaults to 262,144 tokens and supports an explicit override |
| Task deadline | 10,800 seconds per worker | `--max-wall-seconds` defaults to `0` |
| Shared request budget | No fixed call limit | Root defaults to 40 calls, analyst to 16 |

The standalone `--unlimited-executor-rounds` option supports direct code execution and requires a verifiable Git workspace and a positive `--max-wall-seconds` value. When a DSH worker is already writing to the same workspace, the sidecar returns a conflict so Root can adjust the order of tasks.

Executor workers use `run_command`, `edit_file` and `submit_report`. Command and test jobs expose command execution and reporting. Each worker owns a persistent Bash or PowerShell session, including its working directory and environment. `edit_file` replaces one unique text match while preserving encoding and line endings. Long command output is saved under `executions/<job_id>.commands/` and can be read with `read_execution_log` using `output_file`. A timeout or continuation starts a fresh shell and reports the reset.

## Guide a running task

Send follow-up requirements in the DSH chat. Root receives the message, decides which tasks it affects, and sends guidance to the relevant workers. Each worker can plan further changes for its own children.

Guidance enters the recipient's next model input while the system prefix stays stable. Commands already running continue to their current execution boundary. The dashboard distinguishes guidance waiting in the queue from guidance already included in model input.

Guidance from the standalone dashboard goes to HCCA Root. Root or an analyst synchronously waiting for a child processes guidance at its next decision boundary. Completed child tasks need a new delegation.

Tool arguments and waiting behavior are covered in [Delegate and guide tasks](../integrations/dsh-hcca/README.md#delegate-and-guide-tasks).

## Track progress and usage

The HCCA tab in DSH groups workers from the same chat and shows tasks, progress, tool calls, usage, and guidance receipts. Usage covers HCCA workers. DSH records its Root's consumption. Missing provider usage stays marked as missing in cost records.

## Architecture

See [Architecture](ARCHITECTURE.md) for the Root, worker, context, and guidance design.

## Benchmark harness

Use the [benchmark harness](../benchmarks/README.md) with externally prepared task data and repositories. Its guide covers task setup and official evaluation.

## Past experiments

The latest comparison reports HCCA v7 low at 36/50, DSH minimal low at 34/50 and the historical DSH high baseline at 35/50. The overview includes usage, normalized costs and provider settings. [Benchmark overview](BENCHMARK-v7-2026-11-13.md) · [Paper](../paper/hcca-v7-2026-11-13/main.pdf).

The historical frozen-v6 comparison with DSH minimal high records the complete 50-task scope, provider configurations, normalized prices, and usage completeness. [Historical comparison](../reports/verified50-hcca-v6-vs-dsh-summary.json).
