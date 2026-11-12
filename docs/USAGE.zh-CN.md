[English](USAGE.md) | 简体中文 · [项目首页](../README.zh-CN.md)

# 使用与配置

本文适用于 HCCA Official 0.2.1。

## 选择入口

| 入口 | 谁担任 Root | 适合的用法 |
| --- | --- | --- |
| DSH 插件 | DSH 当前聊天 | 在聊天中派发阅读、执行和分析任务，随时补充要求 |
| HCCA 独立运行 | HCCA 自身 | 从命令行提交任务，由 HCCA 拆解并整合结果 |

两种入口共用 reader、executor 与引导消息能力。reader 负责阅读，executor 负责修改、命令和测试，analyst 负责分析与进一步拆解。共享角色配置默认位于 `~/.hcca/agents`，可通过 `HCCA_AGENT_HOME` 指定位置。每个任务使用独立上下文。Root 与 Analyst 都可以派发 Executor，收到结果后继续规划。

## 模型连接

在启动程序的环境中设置模型地址、名称和密钥。DSH Root 的模型由 DSH 配置，以下变量配置 HCCA workers 的模型。

Windows PowerShell 示例

```powershell
$env:HCCA_LLM_BASE_URL = "https://ai.gxwtf.cn/v1"
$env:HCCA_LLM_MODEL = "deepseek-v4.1-flash"
$env:HCCA_LLM_API_KEY = "你的 API key"
$env:HCCA_LLM_VERIFY_TLS = "1"
```

Linux 示例

```sh
export HCCA_LLM_BASE_URL="https://ai.gxwtf.cn/v1"
export HCCA_LLM_MODEL="deepseek-v4.1-flash"
export HCCA_LLM_API_KEY="你的 API key"
export HCCA_LLM_VERIFY_TLS="1"
```

更换服务时，替换地址、模型和密钥。独立入口在地址与密钥均已配置时连接真实模型，否则使用 ScriptedLLM。通用客户端默认模型名为 `deepseek-chat`，DSH sidecar 的兼容默认值为 GXWTF 与 `deepseek-v4.1-flash`。显式环境配置优先。

| 可选变量 | 用途 |
| --- | --- |
| `HCCA_LLM_TIMEOUT` | 单次网络读取的超时 |
| `HCCA_LLM_IDLE_TIMEOUT` | 等待实际模型输出的空闲超时 |
| `HCCA_LLM_CALL_TIMEOUT` | 单次调用的绝对期限，`0` 表示由任务期限控制 |
| `HCCA_LLM_CONCURRENCY` | 模型并发数，`0` 表示不额外限流 |

请求省略输出 token 上限，推理强度使用 `low` 或 `high`。上面的示例显式启用 TLS 证书校验。

### 连接排查

2026-11-12，GXWTF 的过期证书使启用校验的连接在模型请求前失败。服务商更新证书后可恢复正常校验。如果选择现有兼容设置，可在启动 DSH 或 HCCA 的进程环境中设置 `HCCA_LLM_VERIFY_TLS=0`，这会关闭该进程的证书校验。

## 安装 DSH 插件

准备 Python 3.11+、DSH CLI 和 pnpm，并保留完整 HCCA 仓库。在仓库根目录运行以下命令。

```sh
python integrations/dsh-hcca/install.py --profile web
```

桌面版将 `web` 换成 `desktop`。安装后重启对应 DSH 服务或应用，新建会话并选择 HCCA 预设。安装器会备份并更新目标 profile 的 HCCA 配置。插件使用完整仓库中的 Python runtime。

端口、解释器和配置位置等选项见 [DSH 插件说明](../integrations/dsh-hcca/README.zh-CN.md)。

## 独立运行

在完整仓库根目录提交任务，替换工作区和输出路径。Linux 可将 `python` 换成 `python3`。

```sh
python -m hcca.runner --goal "完成你的任务" --workspace "/path/to/project" --out "./runs/example"
```

查看这次运行的面板

```sh
python -m hcca.dsh_worker --runs "./runs" --port 8765
```

打开 [本机面板](http://127.0.0.1:8765/)。运行代码的 Python 依赖仅标准库，开发检查另需 pytest。

### 两种入口的默认值

| 配置 | DSH workers | 独立通用入口 |
| --- | --- | --- |
| 思考与分析强度 | analyst 为 `high`，Root 由 DSH 设置 | 默认继承客户端 `high` |
| 执行与阅读强度 | executor、reader 为 `low` | 默认继承客户端 `high` |
| 执行轮数 | 无固定轮数墙 | 每阶段 16 轮，可用 `--max-tool-rounds` 调整 |
| 阅读轮数 | 8 轮 | 8 轮 |
| 执行软上下文预算 | 524,288 token | 无限执行模式默认 262,144 token，可显式调整 |
| 任务期限 | 每个 worker 默认 10,800 秒 | `--max-wall-seconds` 默认 `0` |
| 共享请求池 | 无固定次数墙 | Root 默认 40 次，analyst 默认 16 次 |

独立入口的 `--unlimited-executor-rounds` 用于代码直执行，需要可验证的 Git 工作区和正数 `--max-wall-seconds`。DSH 在同一工作区已有写入任务时会向 Root 返回冲突，由 Root 调整派发顺序。

## 执行中补充要求

在 DSH 聊天中直接发送补充要求。Root 会收到消息、判断影响范围，并向相关 worker 发送调整。worker 根据自己的任务继续向下级规划。

引导消息进入接收者下一轮输入，system 前缀保持稳定。已经执行中的命令会继续到当前执行边界。面板中的「排队」和「已送入输入」分别表示等待接收和已经加入模型输入。

独立页面的引导发给 HCCA Root。Root 或 analyst 同步等待子任务时，会在下一次决策边界处理留言。已结束的子任务需要重新派发。

DSH 工具的参数与等待方式见 [DSH 聊天 Root](../integrations/dsh-hcca/README.zh-CN.md#dsh-聊天-root)。

## 查看进度与用量

DSH 的 HCCA 选项卡聚合同会话的 workers，展示任务、进度、工具调用、用量和引导回执。用量统计覆盖 HCCA workers，DSH Root 的消耗由 DSH 记录。服务商返回的用量缺失时，费用记录保留缺失状态。

## 架构

Root、worker、上下文与引导消息的设计见[架构说明](ARCHITECTURE.zh-CN.md)。

## 基准工具

[基准工具](../benchmarks/README.md)使用外部准备的题目数据与仓库，其说明包含任务准备和官方评测步骤。

## 历史实验

最新冻结 v7 对照中，HCCA 通过 36/50，复用的 DSH 基线通过 35/50。基准概览包含 token 用量、缓存输入、归一费用与配置。[冻结 v7 概览](BENCHMARK-v7-2026-11-12.zh-CN.md) · [论文](../paper/hcca-v7-2026-11-12/main.pdf)。

冻结 v6 与 DSH 极简 high 的历史对照包含完整 50 题范围、接口配置、归一价格与用量完整性。[历史对照记录](../reports/verified50-hcca-v6-vs-dsh-summary.json)。
