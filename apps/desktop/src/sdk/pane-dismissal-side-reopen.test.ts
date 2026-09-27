import type { PaneData } from '@hermes/plugin-sdk'
import { describe, expect, it, vi } from 'vitest'

import { allPaneIds, group, split } from '@/components/pane-shell/tree/model'
import {
  $collapsedTreeSides,
  $dismissedPanes,
  $layoutTree,
  closeTreePane,
  dismissTreePane,
  isPaneVisible,
  setTreeSideCollapsed,
  watchContributedPanes
} from '@/components/pane-shell/tree/store'
import { setPluginEnabled } from '@/contrib/plugins-store'
import { registry } from '@/contrib/registry'
import { host } from '@/sdk'

vi.mock('@/contrib/plugins-store', () => ({ setPluginEnabled: vi.fn() }))

// Placement, not physical order, determines which side main's chrome heals.
describe.each(['left', 'right', 'bottom', undefined] as const)('persistent plugin dismissal (placement=%s)', placement => {
  it('survives side reopening and peer reveal until explicitly revealed', () => {
    const oldTree = $layoutTree.get()
    const oldDismissed = $dismissedPanes.get()
    const oldSides = $collapsedTreeSides.get()
    const side = placement === 'left' ? 'left' : 'right'

    const disposers = [
      registry.register({ area: 'panes', id: 'dismissal:main', data: { placement: 'main' } }),
      ...['closed', 'peer'].map(name => registry.register({
        area: 'panes',
        id: `dismissal:${name}`,
        source: `plugin:${name}`,
        data: { placement, closeBehavior: 'hide' } satisfies PaneData
      })),
      registry.register({ area: 'panes', id: 'dismissal:core', data: { placement } }),
      registry.register({ area: 'panes', id: 'dismissal:legacy', source: 'plugin:legacy', data: { placement } }),
      registry.register({ area: 'panes', id: 'dismissal:main-legacy', data: { placement: 'main' } })
    ]

    try {
      vi.mocked(setPluginEnabled).mockClear()
      $dismissedPanes.set(new Set())
      $collapsedTreeSides.set(new Set())
      // Deliberately opposite physical order to prove semantic placement wins.
      const main = group(['dismissal:main'])
      const plugin = group(['dismissal:closed'])
      $layoutTree.set(split('row', side === 'left' ? [main, plugin] : [plugin, main]))
      watchContributedPanes()
      closeTreePane('dismissal:closed')
      closeTreePane('dismissal:peer')
      // Old persisted dismissals still need main's chrome recovery behavior.
      dismissTreePane('dismissal:core')
      dismissTreePane('dismissal:legacy')
      dismissTreePane('dismissal:main-legacy')

      setTreeSideCollapsed(side, true)
      setTreeSideCollapsed(side, false)

      expect($dismissedPanes.get()).toContain('dismissal:closed')
      expect($dismissedPanes.get()).toContain('dismissal:peer')
      expect($dismissedPanes.get()).not.toContain('dismissal:core')
      expect($dismissedPanes.get()).toContain('dismissal:legacy')
      expect($dismissedPanes.get()).toContain('dismissal:main-legacy')
      expect(allPaneIds($layoutTree.get()!)).not.toContain('dismissal:closed')
      expect(isPaneVisible('dismissal:closed')).toBe(false)

      setTreeSideCollapsed(side, true)
      host.revealPane('dismissal:peer')
      expect(isPaneVisible('dismissal:peer')).toBe(true)
      expect($dismissedPanes.get()).toContain('dismissal:closed')
      expect(allPaneIds($layoutTree.get()!)).not.toContain('dismissal:closed')

      host.revealPane('dismissal:closed')
      expect($dismissedPanes.get()).not.toContain('dismissal:closed')
      expect(isPaneVisible('dismissal:closed')).toBe(true)
      expect(setPluginEnabled).not.toHaveBeenCalled()
    } finally {
      disposers.forEach(dispose => dispose())
      $layoutTree.set(oldTree)
      $dismissedPanes.set(oldDismissed)
      $collapsedTreeSides.set(oldSides)
    }
  })
})
