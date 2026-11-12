/**
 * HCCA preset tool filter (schema-level).
 *
 * At `agent/created` (before the first model request) restrict the global
 * agent-team / subagent / workflow tools for agents of this preset, so they
 * never appear in the tool catalog. No execution-time interception.
 */
import { appendFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const name = 'hcca-filter'

const DENY = [
  'spawn_teammate',
  'send_message',
  'list_agents',
  'wait_agent',
  'interrupt_agent',
  'team_task_create',
  'team_task_list',
  'team_task_get',
  'team_task_update',
  'subagent',
  'subagent_fork',
  'workflow',
  'run_workflow',
]

const LOG = join(homedir(), '.dsh', 'hcca-filter.log')

function log(line) {
  try {
    appendFileSync(LOG, `${new Date().toISOString()} ${line}\n`)
  } catch {}
}

export function apply(ctx) {
  ctx.on('agent/created', ({ agent }) => {
    if (!agent?.ctx) {
      log('agent/created without ctx')
      return
    }
    const applyRestrictions = (agentCtx) => {
      let applied = 0
      for (const name of DENY) {
        try {
          agentCtx.tools.restrict({ deny: [name] })
          applied += 1
        } catch (error) {
          // Unknown tool in this deployment is fine; log unexpected failures.
          const message = String(error?.message || error)
          if (!message.includes('unknown global tool')) log(`restrict ${name} failed: ${message}`)
        }
      }
      log(`agent ${agent.id || '?'} restricted ${applied}/${DENY.length}`)
    }
    try {
      if (agent.ctx.tools?.restrict) applyRestrictions(agent.ctx)
      else agent.ctx.inject(['tools'], (agentCtx) => applyRestrictions(agentCtx))
    } catch (error) {
      log(`filter failed: ${String(error?.message || error)}`)
    }
  })
}
