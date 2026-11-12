[English](README.md) | 简体中文

# SWE-bench 适配器

此目录提供在外部准备的 SWE-bench Verified Mini 任务上运行 HCCA 的代码。
需要 Python 3.11+、Git、模型连接，以及已准备好的任务仓库。
需要 Linux 测试依赖的仓库请在 Linux 或 WSL 中运行，官方评分还需要 SWE-bench 和 Docker。

准备运行基准时，将任务输入下载到自行指定的数据目录。

```sh
python benchmarks/fetch_swe_mini.py --out /path/to/data/mini.json
```

将选定任务的仓库检出到 `base_commit`，安装测试依赖，并保持工作树干净。
如果任务在容器中运行，将 HCCA 源码和模型环境配置放入该环境，然后运行单个任务。

```sh
python benchmarks/swe_mini.py --tasks /path/to/data/mini.json --instance-id INSTANCE_ID --workspace /path/to/task-repo --out /path/to/predictions/INSTANCE_ID
```

适配器核对基准提交后运行 HCCA，将 `prediction-hcca-v0.2.1.jsonl`、
`hcca-v0.2.1-adapter-status.json` 和运行日志写入输出目录。
补丁通过临时 Git 索引捕获，包含新增源码文件。传给模型的任务输入包含 issue 和仓库名称。

在安装好官方评分工具的环境中评估预测补丁，选定任务需要存在于评分工具的数据集中。

```sh
python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Verified --predictions_path /path/to/predictions/INSTANCE_ID/prediction-hcca-v0.2.1.jsonl --run_id hcca-v0.2.1-local
```

下载器只保存推理所需字段。评测数据集、仓库、镜像、日志和结果由用户单独获取。
