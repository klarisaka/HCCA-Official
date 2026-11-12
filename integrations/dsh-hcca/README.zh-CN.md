[English](README.md) | 简体中文

# HCCA for DSH

插件版本为 0.2.1。DSH 聊天担任 Root，按任务选择 HCCA 的 reader、executor 或 analyst，并整合子任务结果。HCCA 选项卡显示同会话的 worker、进度、用量与引导回执。

## 安装

需要 Python 3.11+、已安装的 DSH CLI 与 pnpm。保留完整 HCCA 仓库，Python sidecar 从仓库加载运行代码和页面。

在仓库根目录运行一条命令。Windows 使用 `python`，Linux 可使用 `python3`。

```sh
python integrations/dsh-hcca/install.py --profile web
```

桌面应用使用 `--profile desktop`。可用 `--port 8766` 指定 sidecar 端口，用 `--python /absolute/path/to/python` 指定解释器，用 `--dsh-home /absolute/path/to/.dsh` 指定配置目录。`--dry-run` 显示计划写入的位置。

安装器先备份目标 profile 的配置和 HCCA 工具，再通过 DSH 的插件管理命令安装本地包，更新 HCCA 专属配置块。重复执行会更新同一块。备份保存在目标 profile 的 `hcca-backups/`。安装完成后重启对应 DSH 服务或桌面应用，新建会话并选择 HCCA 预设。

模型配置由 sidecar 继承 `HCCA_LLM_BASE_URL`、`HCCA_LLM_MODEL` 和 `HCCA_LLM_API_KEY` 等环境变量。请在启动 DSH 的环境中配置凭据。

## DSH 聊天 Root

- `hcca_delegate` 派发一个子任务，`role` 可选 reader、executor 或 analyst，默认 analyst。多个独立子任务可在同一工具批次并行派发。
- 默认等待子任务完成。设置 `timeout_seconds=0` 可立即取得 run id。`hcca_status` 可查看结果，传入 run id 和正数 `timeout_seconds` 可继续等待。
- 执行中在人类聊天中发送补充要求，会唤醒正在等待的 DSH Root。Root 重新规划，再通过 `hcca_guidance` 向指定 run 调整任务。
- HCCA 选项卡中的「在 DSH 聊天中引导」说明如何返回聊天提交要求。回执中的排队与已送入输入，分别表示尚待接收和已经追加到该 worker 的模型输入。

`executor` 处理修改、命令和测试，`reader` 处理阅读，`analyst` 处理分析。写操作的工作区冲突由 sidecar 返回给 Root，Root 可以调整任务次序。

## HCCA 独立运行

完整仓库继续提供独立入口，其中 HCCA 自行担任 Root。通用任务可按以下方式启动，路径替换为实际工作区与运行输出目录。

```sh
python -m hcca.runner --goal "任务" --workspace /path/to/project --out /path/to/run
```

独立 sidecar 页面可用以下命令启动。

```sh
python -m hcca.dsh_worker --port 8765
```

独立页面的引导输入发给该运行的 HCCA Root，由它决定如何调整下级任务。

## 运行文件

`lib/index.js` 启动或复用 sidecar，并代理同源页面与 GET/POST API。`lib/client.js` 注册会话选项卡。`preset-tools.mjs` 注册子任务工具和用户输入唤醒逻辑。


模型连接与运行配置见[使用指南](../../docs/USAGE.zh-CN.md)。
