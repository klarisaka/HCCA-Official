English | [简体中文](README.zh-CN.md)

# SWE-bench adapter

This directory contains code for running HCCA on externally supplied SWE-bench
Verified Mini tasks. Python 3.11+, Git, a model connection, and an already prepared
task repository are required. Use Linux or WSL for repositories with Linux test
dependencies. Official evaluation additionally requires SWE-bench and Docker.

Download task inputs to your own data directory when you want to run a benchmark.

```sh
python benchmarks/fetch_swe_mini.py --out /path/to/data/mini.json
```

Prepare the selected task repository at its `base_commit`, install its test
dependencies, and keep the working tree clean. For containerized tasks, place this
HCCA source checkout and the configured model environment inside that environment.
Then run one instance.

```sh
python benchmarks/swe_mini.py --tasks /path/to/data/mini.json --instance-id INSTANCE_ID --workspace /path/to/task-repo --out /path/to/predictions/INSTANCE_ID
```

The adapter checks the base commit, runs HCCA with its reader and executor, and
writes `prediction-hcca-v0.2.1.jsonl`, `hcca-v0.2.1-adapter-status.json`, and the runtime logs under the
chosen output directory. It captures new source files through a temporary Git
index. The repository's real index is preserved. Task inputs passed to the model
contain the issue and repository name.

Evaluate the prediction with the official harness installed in the evaluation
environment. The selected instance must be available in that harness's dataset.

```sh
python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Verified --predictions_path /path/to/predictions/INSTANCE_ID/prediction-hcca-v0.2.1.jsonl --run_id hcca-v0.2.1-local
```

The downloader stores only the fields used for inference. Evaluation datasets,
repositories, images, run logs, and results are obtained separately by the user.
