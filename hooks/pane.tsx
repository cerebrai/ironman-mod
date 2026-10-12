import { atom, read, update } from 'claude-code'
import type { ElementTable, EngineInterface, Register, RenderElement } from 'claude-code'

import type { Diagram, Plan, PlanItem } from '../types'

const PANE = 'ironman'
const PANE_COLUMNS = 40
const DIAGRAM_MAX_COLUMNS = 70
const DIAGRAM_MIN_COLUMNS = 24

// Used when PUPPETEER_EXECUTABLE_PATH is unset; the converter's own browser fails here.
const DEFAULT_CHROME = '/usr/bin/google-chrome'

// Pictures are made at 3x; this many diagram pixels fill one terminal column.
const PIXELS_PER_COLUMN = 36

const plan = atom({ plugin: 'ironman', key: 'plan' } as const, null)
const focus = atom({ plugin: 'ironman', key: 'focus' } as const, null)
const openNotes = atom({ plugin: 'ironman', key: 'openNotes' } as const, {})
const diagram = atom({ plugin: 'ironman', key: 'diagram' } as const, null)

type Lit = 'done' | 'now' | 'todo'

const MARK: Record<Lit, string> = { done: '✓', now: '▶', todo: '○' }
const COLOR: Record<Lit, string | undefined> = { done: '#81c784', now: '#ffc107', todo: undefined }
const BORDER: Record<Lit, string> = { done: '#81c784', now: '#ffc107', todo: '#5c6773' }

const PLAN_TOOL = {
  name: 'pane_plan',
  description:
    'Show the overall plan in the side pane: the blocks of a piece of code, the ' +
    'headings of a document, or the steps of a lesson. Call it once, after the ' +
    'user agrees to the overview. Replaces any earlier plan or diagram. Style ' +
    '"blocks" draws boxes joined by arrows (use it for code); style "list" draws ' +
    'an outline (lessons, documents). The default is list.',
  isDeferred: false,
  inputSchema: {
    type: 'object',
    properties: {
      title: { type: 'string' },
      style: { enum: ['list', 'blocks'] },
      items: {
        type: 'array',
        items: {
          type: 'object',
          properties: {
            id: { type: 'string', description: 'short unique name, like b3' },
            label: { type: 'string' },
            depth: { type: 'number', description: '0 for a heading, 1 for a step under it' },
            note: {
              type: 'string',
              description: 'one to three plain sentences explaining the item; the user opens it with an arrow',
            },
            feeds: {
              type: 'array',
              items: { type: 'string' },
              description: 'blocks style: ids of the blocks this one passes its result to',
            },
          },
          required: ['id', 'label', 'depth'],
        },
      },
    },
    required: ['title', 'items'],
  },
}

const FOCUS_TOOL = {
  name: 'pane_focus',
  description:
    'Light up one item of the plan to show which piece we are on. Call it as ' +
    'each piece starts. An empty id clears the highlight. If a diagram is showing, ' +
    'the pane goes back to the plan.',
  isDeferred: false,
  inputSchema: {
    type: 'object',
    properties: { id: { type: 'string' } },
    required: ['id'],
  },
}

const DIAGRAM_TOOL = {
  name: 'pane_diagram',
  description:
    'Draw a mermaid diagram in the side pane, such as the architecture of the code ' +
    'we are about to build. Use it for the overview, before the plan. Write a ' +
    'flowchart that runs top to bottom (flowchart TD), with at most about 8 boxes, ' +
    'labels of one to three words, and short arrow labels. Use plain rectangles ' +
    'like A[Label] only: no diamonds {}, no subgraphs, no styling lines. The next pane_plan ' +
    'replaces the diagram.',
  isDeferred: false,
  inputSchema: {
    type: 'object',
    properties: {
      title: { type: 'string' },
      mermaid: { type: 'string', description: 'mermaid source, for example "flowchart TD\\n  A[Read] --> B[Draw]"' },
      alt: { type: 'string', description: 'one line saying what the diagram shows' },
    },
    required: ['title', 'mermaid'],
  },
}

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

// Decodes the first 24 bytes of a PNG; its width and height sit at bytes 16 and 20.
function pngSize(base64: string): { width: number; height: number } {
  const bytes: number[] = []
  for (let i = 0; i < 32; i += 4) {
    const n = [0, 1, 2, 3].reduce((acc, j) => acc * 64 + B64.indexOf(base64[i + j] ?? 'A'), 0)
    bytes.push((n >> 16) & 255, (n >> 8) & 255, n & 255)
  }
  const at = (o: number) =>
    ((bytes[o]! << 24) | (bytes[o + 1]! << 16) | (bytes[o + 2]! << 8) | bytes[o + 3]!) >>> 0
  return { width: at(16), height: at(20) }
}

// Returns the picture's size, or the converter's error text when it fails.
async function renderMermaid($: EngineInterface, source: string,
                             base: string): Promise<{ width: number; height: number } | string> {
  await $.fs.write(`${base}.mmd`, source)
  const chrome = (await $.env.get('PUPPETEER_EXECUTABLE_PATH')) ?? DEFAULT_CHROME
  const run = await $.process.run(
    ['npx', '-y', '-p', '@mermaid-js/mermaid-cli', 'mmdc',
     '-i', `${base}.mmd`, '-o', `${base}.png`, '-b', 'transparent',
     '-c', `${$.plugin.root}/assets/mermaid.json`, '-s', '3'],
    { env: { PUPPETEER_EXECUTABLE_PATH: chrome }, timeoutMs: 120_000 },
  )
  if (run.exitCode !== 0) return run.stderr.slice(0, 500)
  const { base64 } = await $.fs.read(`${base}.png`, { as: 'bytes' })
  return pngSize(base64)
}

// The pane is as wide as the picture, plus room for its border.
function paneColumnsFor(d: { width: number }): number {
  const fit = Math.ceil(d.width / PIXELS_PER_COLUMN) + 4
  return Math.min(DIAGRAM_MAX_COLUMNS, Math.max(DIAGRAM_MIN_COLUMNS, fit))
}

function pictureColumns(d: Diagram, columns: number): number {
  return Math.min(255, columns, Math.max(10, Math.ceil(d.width / PIXELS_PER_COLUMN)))
}

// A terminal cell is about twice as tall as it is wide.
function pictureRows(d: Diagram, columns: number): number {
  return Math.min(255, Math.max(1, Math.round((columns * (d.height / d.width)) / 2)))
}

function stateOf(i: number, at: number): Lit {
  return i < at ? 'done' : i === at ? 'now' : 'todo'
}

// Bytes, not a file path: a terminal inside a multiplexer (herdr) cannot read the path.
function drawDiagram(els: ElementTable, d: Diagram, png: string, columns: number): RenderElement {
  const { Text } = els
  if (!('Image' in els)) return <Text dimColor>{d.alt}</Text>
  const { Image } = els
  const cols = pictureColumns(d, columns)
  return (
    <Image key={d.path} source={{ png }}
           columns={cols} rows={pictureRows(d, cols)} alt={d.alt} />
  )
}

// The arrow that opens or closes an item's note; items without a note get none.
function noteButton($: EngineInterface, els: ElementTable, item: PlanItem,
                    isOpen: boolean): RenderElement | null {
  if (!item.note) return null
  const { Button } = els
  return (
    <Button key={`${item.id}-note`} label={isOpen ? '▾' : '▸'}
            onPress={() => update($, openNotes, m => ({ ...m, [item.id]: !m[item.id] }))} />
  )
}

function drawList($: EngineInterface, els: ElementTable, items: PlanItem[], at: number,
                  opened: Record<string, boolean>): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      {items.map((item, i) => {
        const s = stateOf(i, at)
        const isOpen = opened[item.id] === true
        return (
          <Box key={item.id} flexDirection="column" paddingLeft={item.depth * 2}>
            <Box gap={1}>
              <Text bold={s === 'now'} color={COLOR[s]} dimColor={at >= 0 && s === 'todo'}>
                {MARK[s]} {item.label}
              </Text>
              {noteButton($, els, item, isOpen)}
            </Box>
            {item.note && isOpen ? (
              <Box paddingLeft={2}>
                <Text dimColor>{item.note}</Text>
              </Box>
            ) : null}
          </Box>
        )
      })}
    </Box>
  )
}

function drawBlocks($: EngineInterface, els: ElementTable, items: PlanItem[], at: number,
                    opened: Record<string, boolean>): RenderElement {
  const { Box, Text } = els
  return (
    <Box flexDirection="column">
      {items.map((item, i) => {
        const s = stateOf(i, at)
        const isOpen = opened[item.id] === true
        return (
          <Box key={item.id} flexDirection="column" paddingLeft={item.depth * 2}>
            <Box borderStyle="round" borderColor={BORDER[s]} paddingX={1} flexDirection="column">
              <Box gap={1}>
                <Text bold={s === 'now'} color={COLOR[s]} dimColor={at >= 0 && s === 'todo'}>
                  {MARK[s]} {item.id} {item.label}
                </Text>
                {noteButton($, els, item, isOpen)}
              </Box>
              {item.note && isOpen ? <Text dimColor>{item.note}</Text> : null}
            </Box>
            {item.feeds && item.feeds.length > 0 ? (
              <Text dimColor>  ▼ feeds {item.feeds.join(', ')}</Text>
            ) : null}
          </Box>
        )
      })}
    </Box>
  )
}

export const registerPane: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.tool.register(PLAN_TOOL)
    await $.tool.register(FOCUS_TOOL)
    await $.tool.register(DIAGRAM_TOOL)
    await $.command.register({ name: 'pane', description: 'Open the ironman side pane' })
    return next(e)
  })

  on('command.run', { command: 'pane' }, async $ => {
    const current = await read($, plan)
    const shown = await read($, diagram)
    await $.ui.open({
      id: PANE,
      title: shown?.title ?? current?.title ?? 'ironman',
      columns: shown ? paneColumnsFor(shown) : PANE_COLUMNS,
    })
    return { text: 'ironman pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const shown = await read($, diagram)
    if (shown !== null) {
      const { base64 } = await $.fs.read(shown.path, { as: 'bytes' })
      return drawDiagram(els, shown, base64, Math.max(20, e.props.bodyColumns - 2))
    }
    const current = await read($, plan)
    if (current === null) return <els.Text dimColor>Nothing to show yet.</els.Text>
    const lit = await read($, focus)
    const at = current.items.findIndex(item => item.id === lit)
    const opened = await read($, openNotes)
    return current.style === 'blocks'
      ? drawBlocks($, els, current.items, at, opened)
      : drawList($, els, current.items, at, opened)
  })

  on('tool.call', { tool: 'mcp__ironman__pane_diagram' }, async ($, e) => {
    if (typeof e.title !== 'string' || typeof e.mermaid !== 'string') {
      return { deny: 'pane_diagram needs a title and mermaid text' }
    }
    const tmp = (await $.env.get('TMPDIR')) ?? '/tmp'
    const base = `${tmp}/ironman-pane-${await $.session.id()}/${await $.clock.now()}`
    const made = await renderMermaid($, e.mermaid, base)
    if (typeof made === 'string') {
      return { result: `Mermaid failed. Fix the source and try again:\n${made}` }
    }
    const alt = typeof e.alt === 'string' ? e.alt : e.title
    await update($, diagram, () => ({ title: e.title as string, path: `${base}.png`, ...made, alt }))
    const opened = await $.ui.open({ id: PANE, title: e.title, columns: paneColumnsFor(made) })
    const where = opened.isPlaced ? 'Diagram shown in the side pane.' : `Pane not placed: ${opened.reason}.`
    return { result: `${where} Picture: ${base}.png` }
  })

  on('tool.call', { tool: 'mcp__ironman__pane_plan' }, async ($, e) => {
    if (typeof e.title !== 'string' || !Array.isArray(e.items)) {
      return { deny: 'pane_plan needs a title and a list of items' }
    }
    if (e.style !== undefined && e.style !== 'list' && e.style !== 'blocks') {
      return { deny: 'pane_plan style must be "list" or "blocks"' }
    }
    await update($, plan, () => ({
      title: e.title as string,
      style: e.style as Plan['style'],
      items: e.items as PlanItem[],
    }))
    await update($, focus, () => null)
    await update($, openNotes, () => ({}))
    await update($, diagram, () => null)
    const opened = await $.ui.open({ id: PANE, title: e.title, columns: PANE_COLUMNS })
    return { result: opened.isPlaced ? 'Plan shown in the side pane.' : `Pane not placed: ${opened.reason}.` }
  })

  on('tool.call', { tool: 'mcp__ironman__pane_focus' }, async ($, e) => {
    if (typeof e.id !== 'string') {
      return { deny: 'pane_focus needs an id' }
    }
    const current = await read($, plan)
    if (e.id !== '' && !current?.items.some(i => i.id === e.id)) {
      return { deny: `no plan item has the id ${e.id}` }
    }
    await update($, focus, () => (e.id === '' ? null : e.id))
    if (current !== null && (await read($, diagram)) !== null) {
      await update($, diagram, () => null)
      await $.ui.open({ id: PANE, title: current.title, columns: PANE_COLUMNS })
    }
    return { result: 'Highlight moved.' }
  })
}
