import type { PaneData } from '@hermes/plugin-sdk'
import { describe, expect, it, vi } from 'vitest'

import { group, split } from '@/components/pane-shell/tree/model'
import {
  $collapsedTreeSides,
  $dismissedPanes,
  $layoutTree,
  $paneVisible,
  isPaneVisible,
  setTreeSideCollapsed
} from '@/components/pane-shell/tree/store'
import { setPluginEnabled } from '@/contrib/plugins-store'
import { registry } from '@/contrib/registry'
import { host } from '@/sdk'

vi.mock('@/contrib/plugins-store', () => ({ setPluginEnabled: vi.fn() }))

// Main's side-collapse model groups root children by their declared placement.
describe.each(['left', 'right'] as const)('host.togglePane behind the collapsed %s side', side => {
  it.each([undefined, 'hide'] as const)('reveals without closing (closeBehavior=%s)', closeBehavior => {
    const oldTree = $layoutTree.get()
    const oldDismissed = $dismissedPanes.get()
    const oldSides = $collapsedTreeSides.get()
    const paneId = `collapsed:${side}:${closeBehavior ?? 'default'}`

    const disposers = [
      registry.register({ area: 'panes', id: 'collapsed:main', data: { placement: 'main' } }),
      registry.register({
        area: 'panes',
        id: paneId,
        source: 'plugin:collapsed',
        data: { placement: side, closeBehavior } satisfies PaneData
      })
    ]

    const visible = $paneVisible(paneId)
    const onVisible = vi.fn()
    const unsubscribe = visible.listen(onVisible)

    try {
      vi.mocked(setPluginEnabled).mockClear()
      $dismissedPanes.set(new Set())
      $collapsedTreeSides.set(new Set())
      const main = group(['collapsed:main'])
      const plugin = group([paneId])
      $layoutTree.set(split('row', side === 'left' ? [plugin, main] : [main, plugin]))
      expect(visible.get()).toBe(true)
      onVisible.mockClear()

      setTreeSideCollapsed(side, true)
      expect.soft(onVisible).toHaveBeenLastCalledWith(false, true, undefined)
      expect.soft(isPaneVisible(paneId)).toBe(false)
      const collapsedTree = $layoutTree.get()

      host.togglePane(paneId)

      expect($collapsedTreeSides.get().has(side)).toBe(false)
      expect(isPaneVisible(paneId)).toBe(true)
      expect(visible.get()).toBe(true)
      expect($dismissedPanes.get().has(paneId)).toBe(false)
      expect(setPluginEnabled).not.toHaveBeenCalled()
      expect($layoutTree.get()).toEqual(collapsedTree)

      // Once visible, existing close policy still applies: hide takes precedence
      // over disabling a single-pane plugin; the default remains disabling it.
      host.togglePane(paneId)

      if (closeBehavior === 'hide') {
        expect($dismissedPanes.get().has(paneId)).toBe(true)
        expect(isPaneVisible(paneId)).toBe(false)
        expect(setPluginEnabled).not.toHaveBeenCalled()
      } else {
        expect(setPluginEnabled).toHaveBeenCalledWith('collapsed', false)
      }
    } finally {
      unsubscribe()
      disposers.forEach(dispose => dispose())
      $layoutTree.set(oldTree)
      $dismissedPanes.set(oldDismissed)
      $collapsedTreeSides.set(oldSides)
    }
  })
})
