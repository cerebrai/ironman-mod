import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Mode } from '../types'

const mode = atom({ plugin: 'ironman', key: 'mode' } as const, null)

const MODE_COLOR: Record<Mode, string> = {
  discuss: '#4fc3f7',
  teach: '#81c784',
  code: '#ffb300',
}

// /ironman on|off|mode writes {"mode": "..."} here; no file means ironman is off.
async function readMode($: EngineInterface): Promise<Mode | null> {
  const home = await $.env.get('HOME')
  const id = await $.session.id()
  try {
    const text = await $.fs.read(`${home}/.claude/ironman/sessions/${id}`)
    return JSON.parse(text).mode ?? 'discuss'
  } catch {
    return null
  }
}

async function refreshMode($: EngineInterface): Promise<void> {
  const value = await readMode($)
  await update($, mode, () => value)
}

export const registerBand: Register = on => {
  on('session.start', { isInteractive: true }, async ($, e, next) => {
    await refreshMode($)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    await refreshMode($)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const current = await read($, mode)
    if (current === null || e.props.hasSurvey) {
      return next(e)
    }
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box borderStyle="round" borderColor="#c62828" paddingX={1} alignSelf="flex-start">
        <Text bold color="#ffc107">ironman</Text>
        <Text dimColor> · </Text>
        <Text bold color={MODE_COLOR[current]}>{current}</Text>
      </Box>
    )
  })
}
