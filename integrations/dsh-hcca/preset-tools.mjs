/**
 * HCCA tools for the `hcca` agent preset (plain ESM, no build step).
 *
 * Tools are registered into the preset's agent-plane composition, so only
 * sessions created with the HCCA preset see them. The sidecar is started by
 * the host plugin (`@hcca/dsh-hcca`); this module only talks HTTP to it.
 */
export const name = 'hcca-tools'

export const inject = ['tools']

/** Best-effort session id from the execution's agent context. */
function sessionOf(exec) {
  const agent = exec?.agent
  const candidates = [
    agent?.session?.id,
    agent?.session?.sessionId,
    agent?.sessionId,
    agent?.session?.meta?.id,
    agent?.id,
  ]
  return String(candidates.find((value) => typeof value === 'string' && value) || '')
}

async function call(base, method, path, body, timeoutMs = 15000, signal) {
  const response = await fetch(base + path, {
    method,
    headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs),
  })
  const text = await response.text()
  let data
  try {
    data = JSON.parse(text)
  } catch {
    data = { raw: text }
  }
  if (!response.ok) {
    const error = new Error(data.error || `sidecar HTTP ${response.status}`)
    error.status = response.status
    throw error
  }
  return data
}

function summarize(status, { withReport = true } = {}) {
  if (!status || !status.agents) return 'HCCA sidecar 尚无运行记录。'
  const active = status.agents.filter((agent) => ['RUNNING', 'READY', 'WAITING'].includes(String(agent.status || '').toUpperCase()))
  const lines = [
    `run: ${status.run_id} · status: ${status.status} · model: ${status.model || '?'}`,
    `agents: ${status.agents.length} (${active.length} active) · messages: ${status.messages?.sent ?? 0}/${status.messages?.processed ?? 0} · wakes: ${status.messages?.wakes ?? 0}`,
    `tokens: in ${status.tokens?.input ?? 0} (cache ${Math.round((status.tokens?.cache_hit_rate ?? 0) * 100)}%) · out ${status.tokens?.output ?? 0} · cost $${status.tokens?.cost_estimate ?? 0}`,
  ]
  if (status.plan?.agents?.length) {
    lines.push('Root 拆解:')
    for (const agent of status.plan.agents) {
      const prompt = String(agent.prompt || '').replace(/\s+/g, ' ')
      lines.push(`  - ${agent.name}: ${prompt.length > 160 ? prompt.slice(0, 160) + '…' : prompt}`)
    }
  }
  if (status.agents.length) {
    lines.push('agents:')
    for (const agent of status.agents) {
      const dim = agent.dimension ? `:${agent.dimension}` : ''
      lines.push(`  - ${agent.agent_id} (${agent.role}${dim}) ${agent.status}${agent.waiting_on ? ` wait:${agent.waiting_on.kind}` : ''}`)
    }
  }
  if (status.pending_calls?.length) {
    lines.push('进行中调用:')
    for (const pending of status.pending_calls) lines.push(`  - ${pending.tag} (${pending.elapsed_s ?? 0}s, 第 ${pending.attempt ?? 1} 次)`)
  }
  if (status.reviews?.length) lines.push(`复盘: ${status.reviews.map((item) => item.decision).join(' → ')}`)
  if (status.checks && status.checks.score != null) lines.push(`checks: ${status.checks.score}/${status.checks.total} · judge: ${status.judge?.overall ?? 'n/a'}`)
  if (status.report_chars) lines.push(`report: ${status.report_chars} chars`)
  if (status.error) lines.push(`error: ${status.error}`)
  if (withReport && status.report) {
    lines.push('', '--- 最终报告 ---', String(status.report))
  }
  return lines.join('\n')
}

export function apply(ctx, config = {}) {
  const port = Number(config.port || process.env.HCCA_SIDECAR_PORT || 8765)
  const base = `http://127.0.0.1:${port}`
  const waiting = new Map()
  const inputVersion = new Map()
  const request = (method, path, body, timeoutMs, signal) => call(base, method, path, body, timeoutMs, signal)
  ctx.on('agent/inbox/inserted', ({ agent, message }) => {
    if (message?.source?.kind !== 'user') return
    const session = sessionOf({ agent })
    if (!session) return
    inputVersion.set(session, (inputVersion.get(session) || 0) + 1)
    for (const controller of waiting.get(session) || []) controller.abort()
  })
  const tool = (definition) => {
    ctx.tools.register(definition)
  }
  function pendingUserInput(exec) {
    const inbox = exec?.agent?.inbox
    return [...(inbox?.nextTurn || []), ...(inbox?.nextStep || [])].some((message) => message.source?.kind === 'user')
  }
  async function waitForRun(runId, session, seconds, exec, version = inputVersion.get(session) || 0) {
    const newInput = `HCCA 子任务 ${runId} 仍在运行，DSH Root 有新的用户消息待处理。`
    if (pendingUserInput(exec) || (inputVersion.get(session) || 0) !== version) {
      exec?.concludeTurn?.()
      return newInput
    }
    const controller = new AbortController()
    if (!waiting.has(session)) waiting.set(session, new Set())
    waiting.get(session).add(controller)
    try {
      const final = await request('GET', `/api/wait?run=${encodeURIComponent(runId)}&timeout=${Math.round(seconds)}`, undefined, (Math.round(seconds) + 30) * 1000, controller.signal)
      return final?.timed_out ? `HCCA 子任务 ${runId} 仍在运行。` : summarize(final)
    } catch (error) {
      if (controller.signal.aborted) {
        exec?.concludeTurn?.()
        return newInput
      }
      return `HCCA 子任务 ${runId} 等待中断：${error?.message || error}。用 hcca_status 查看进度或继续等待。`
    } finally {
      waiting.get(session)?.delete(controller)
      if (!waiting.get(session)?.size) waiting.delete(session)
    }
  }

  tool({
    name: 'hcca_delegate',
    description:
      '委派一个独立子任务给 HCCA reader、executor 或 analyst。你作为 Root 拆解并整合结果。默认等待子任务完成，timeout_seconds=0 可立即取得 run_id。',
    parameters: {
      type: 'object',
      properties: {
        objective: { type: 'string', description: '子任务目标、必要背景与验收条件' },
        role: { type: 'string', enum: ['reader', 'executor', 'analyst'], description: 'reader 负责阅读，executor 负责命令、修改与测试，analyst 负责分析；默认 analyst。' },
        workspace: {
          type: 'string',
          description: '工作区绝对路径；省略时从任务文本识别',
        },
        timeout_seconds: {
          type: 'number',
          description: '可选：最多等待多少秒（默认 2400，即 40 分钟）。0 表示不等待、立即返回 run_id。',
        },
      },
      required: ['objective'],
      additionalProperties: false,
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const session = sessionOf(exec)
      const version = inputVersion.get(session) || 0
      const result = await request('POST', '/api/delegate', {
        objective: args.objective,
        external_root: true,
        role: args.role || 'analyst',
        workspace: args.workspace,
        evidence: args.evidence,
        session,
      })
      const runId = result.run_id
      const waitSeconds = Number(args.timeout_seconds ?? 2400)
      if (runId && (pendingUserInput(exec) || (inputVersion.get(session) || 0) !== version)) {
        return waitForRun(runId, session, 0, exec, version)
      }
      if (!runId || waitSeconds <= 0) {
        return `已创建 HCCA run ${runId}（status=${result.status}）。用 hcca_status 查看进度。`
      }
      return waitForRun(runId, session, waitSeconds, exec, version)
    },
  })

  tool({
    name: 'hcca_status',
    description: '查看 HCCA 子任务进度和结果。指定 run_id 与 timeout_seconds 可继续等待，期间人类新消息会唤醒 Root。',
    parameters: {
      type: 'object',
      properties: {
        run_id: { type: 'string', description: '可选：run id' },
        timeout_seconds: { type: 'number', description: '可选：等待已有子任务的秒数，默认 0。' },
      },
      additionalProperties: false,
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const session = sessionOf(exec)
      const version = inputVersion.get(session) || 0
      if (!args.run_id && !session) {
        return '无法确定当前会话；请传入 run_id（先用 hcca_runs 查看本会话的运行）。'
      }
      const query = args.run_id
        ? `?run=${encodeURIComponent(args.run_id)}`
        : `?session=${encodeURIComponent(session)}`
      const status = await request('GET', `/api/status${query}`)
      if (args.timeout_seconds > 0 && args.run_id) return waitForRun(args.run_id, session, Number(args.timeout_seconds), exec, version)
      return summarize(status)
    },
  })

  tool({
    name: 'hcca_runs',
    description: '列出 HCCA sidecar 中最近的运行记录。',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    async execute(_args, exec) {
      const session = sessionOf(exec)
      if (!session) {
        return '无法确定当前会话；HCCA 运行记录按会话隔离，未找到 session。'
      }
      const runs = await request('GET', `/api/runs?session=${encodeURIComponent(session)}`)
      return runs.slice(0, 10).map((run) => `${run.run_id}  ${run.status}  ${run.model || ''}`).join('\n')
        || '本会话暂无 HCCA 运行记录；请先用 hcca_delegate 派发任务。'
    },
  })

  tool({
    name: 'hcca_cancel',
    description: '取消一个正在运行的 HCCA run。',
    parameters: {
      type: 'object',
      properties: { run_id: { type: 'string' } },
      required: ['run_id'],
      additionalProperties: false,
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    async execute(args) {
      const result = await request('POST', '/api/cancel', { run_id: args.run_id })
      return `run ${result.run_id} ${result.status}`
    },
  })

  tool({
    name: 'hcca_guidance',
    description: '上层向指定 HCCA worker 调整任务，由该 worker 决定如何指导下级。',
    parameters: {
      type: 'object',
      properties: {
        text: { type: 'string', description: '补充要求原文' },
        run_id: { type: 'string', description: '要调整的子任务 run id' },
        message_id: { type: 'string', description: '可选，重试时复用同一消息编号' },
      },
      required: ['run_id', 'text'],
      additionalProperties: false,
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    async execute(args, exec) {
      const session = sessionOf(exec)
      const receipt = await request('POST', '/api/guidance', {
        run_id: args.run_id, session, text: args.text, message_id: args.message_id,
      })
      return `run ${receipt.run_id} · guidance ${receipt.guidance_id} · ${receipt.status}`
    },
  })

  // Slash command available in HCCA mode (no model message is created).
  ctx.inject(['commands'], (ctx) => {
    ctx.commands.register({
      name: 'hcca-status',
      description: '查看 HCCA 最近一次运行的状态',
      input: { hint: '[run_id]' },
      async handler({ rawInput }) {
        try {
          const runId = String(rawInput || '').trim()
          const status = await request('GET', `/api/status${runId ? `?run=${encodeURIComponent(runId)}` : ''}`)
          return { kind: 'success', text: summarize(status) }
        } catch (error) {
          return { kind: 'error', text: `HCCA sidecar 不可用: ${error?.message || error}` }
        }
      },
    })
  })
}
