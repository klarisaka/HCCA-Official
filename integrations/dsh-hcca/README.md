English | [简体中文](README.zh-CN.md)

# HCCA for DSH

Plugin version 0.2.1 makes the DSH chat the Root agent. It delegates reading, execution, and analysis to HCCA workers, then combines their results. The HCCA tab shows workers in the current session, progress, usage, and guidance receipts.

## Install

Requires Python 3.11+, the DSH CLI, and pnpm. Keep the complete HCCA repository available so the Python sidecar can load its runtime and dashboard.

Run from the repository root. Linux users can use `python3`.

```sh
python integrations/dsh-hcca/install.py --profile web
```

Use `--profile desktop` for the desktop app. Optional arguments include `--port 8766`, `--python /absolute/path/to/python`, `--dsh-home /absolute/path/to/.dsh`, and `--dry-run`.

The installer backs up the profile configuration and HCCA tools, installs the local plugin through DSH, and updates the profile's HCCA configuration block. Running it again updates that block. Backups are stored in the profile's `hcca-backups/` directory. Restart DSH, create a session, and select the HCCA preset.

Configure `HCCA_LLM_BASE_URL`, `HCCA_LLM_MODEL`, and `HCCA_LLM_API_KEY` in the environment used to start DSH. The sidecar inherits these variables. See the [usage guide](../../docs/USAGE.md) for model configuration.

## Delegate and guide tasks

- `hcca_delegate` starts a subtask. Choose `reader`, `executor`, or `analyst` with `role`. The default is `analyst`. Independent tasks can be delegated in parallel within one tool batch.
- Delegation waits for completion by default. Set `timeout_seconds=0` to receive a run ID immediately. Use `hcca_status` to check the result, or pass the run ID and a positive `timeout_seconds` to wait again.
- Send additional requirements in the DSH chat while work is running. This wakes the waiting Root, which replans and sends targeted instructions through `hcca_guidance`.
- The HCCA tab explains how to return to chat to guide the Root. Guidance receipts show whether a message is queued or has been added to a worker's model input.

The executor handles edits, commands, and tests. The reader handles reading, and the analyst handles analysis. The sidecar reports conflicting workspace writes to the Root so it can adjust task order.

## Run HCCA independently

HCCA also provides its own Root through the standalone entry point. Replace the paths with your workspace and output directory.

```sh
python -m hcca.runner --goal "Your task" --workspace /path/to/project --out /path/to/run
```

Start the standalone dashboard with the sidecar.

```sh
python -m hcca.dsh_worker --port 8765
```

Guidance submitted on the standalone page goes to that run's HCCA Root, which decides how to adjust its subtasks.

## Runtime files

`lib/index.js` starts or reuses the sidecar and proxies its page and GET/POST APIs. `lib/client.js` registers the session tab. `preset-tools.mjs` registers delegation tools and handles user-message wakeups.
