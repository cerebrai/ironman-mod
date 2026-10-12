import type { Register } from 'claude-code'

import { registerBand } from './band'
import { registerPane } from './pane'

export const register: Register = (on, options) => {
  registerBand(on, options)
  registerPane(on, options)
}
