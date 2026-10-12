export type Mode = 'discuss' | 'teach' | 'code'

export type PlanItem = {
  id: string
  label: string
  depth: number
  note?: string
  feeds?: string[]
}
export type Plan = { title: string; style?: 'list' | 'blocks'; items: PlanItem[] }

export type Diagram = { title: string; path: string; width: number; height: number; alt: string }

declare module 'claude-code' {
  interface PluginState {
    ironman: {
      mode: Mode | null
      plan: Plan | null
      focus: string | null
      openNotes: Record<string, boolean>
      diagram: Diagram | null
    }
  }
}
