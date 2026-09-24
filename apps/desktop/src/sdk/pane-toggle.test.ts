import { describe, expect, it } from 'vitest'

import { findGroupOfPane, group, split } from '@/components/pane-shell/tree/model'
import { $dismissedPanes, $layoutTree, isPaneVisible, watchContributedPanes } from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import { host } from '@/sdk'

describe('host.togglePane', () => {
  it('fronts an inactive tab, hides the visible pane, and keeps explicit reveal idempotent', () => {
    const oldTree = $layoutTree.get()
    const oldDismissed = $dismissedPanes.get()

    const disposers = [
      registry.register({ area: 'panes', id: 'toggle:main', data: { placement: 'main' } }),
      registry.register({ area: 'panes', id: 'toggle:files', data: { placement: 'right' } }),
      registry.register({
        area: 'panes',
        id: 'toggle:pane',
        source: 'plugin:toggle',
        data: { placement: 'right', closeBehavior: 'hide', dock: { pane: 'toggle:files', pos: 'center' } }
      })
    ]

    try {
      $dismissedPanes.set(new Set())
      $layoutTree.set(
        split('row', [group(['toggle:main']), group(['toggle:files', 'toggle:pane'], { active: 'toggle:files' })])
      )
      host.togglePane('toggle:pane')
      expect(isPaneVisible('toggle:pane')).toBe(true)
      host.togglePane('toggle:pane')
      expect(isPaneVisible('toggle:pane')).toBe(false)
      expect($dismissedPanes.get()).toContain('toggle:pane')
      watchContributedPanes()
      expect(isPaneVisible('toggle:pane')).toBe(false)
      host.togglePane('toggle:pane')
      expect(isPaneVisible('toggle:pane')).toBe(true)
      host.revealPane('toggle:pane')
      host.revealPane('toggle:pane')
      expect(isPaneVisible('toggle:pane')).toBe(true)
      expect(findGroupOfPane($layoutTree.get()!, 'toggle:pane')?.id).toBe(
        findGroupOfPane($layoutTree.get()!, 'toggle:files')?.id
      )
      const before = $layoutTree.get()
      host.togglePane('  ')
      expect($layoutTree.get()).toBe(before)
    } finally {
      disposers.forEach(dispose => dispose())
      $layoutTree.set(oldTree)
      $dismissedPanes.set(oldDismissed)
    }
  })
})
