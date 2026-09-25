import type { PaneData } from '@hermes/plugin-sdk'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setPluginEnabled } from '@/contrib/plugins-store'
import { registry } from '@/contrib/registry'

import { allPaneIds, group, split } from './model'
import {
  $dismissedPanes,
  $layoutTree,
  closeTreePane,
  isPaneVisible,
  revealTreePane,
  watchContributedPanes
} from './store'

vi.mock('@/contrib/plugins-store', () => ({ setPluginEnabled: vi.fn() }))
vi.mock('@/store/notifications', () => ({ notify: vi.fn() }))
vi.mock('@/i18n', () => ({ translateNow: (key: string) => key }))

const disposers: (() => void)[] = []

function registerPluginPane(pluginId: string, paneId: string) {
  disposers.push(
    registry.register({
      area: 'panes',
      data: { placement: 'main' },
      id: paneId,
      render: () => null,
      source: `plugin:${pluginId}`,
      title: paneId
    })
  )
}

beforeEach(() => {
  window.localStorage.clear()
  $dismissedPanes.set(new Set())
  vi.mocked(setPluginEnabled).mockReset()
})

afterEach(() => {
  disposers.splice(0).forEach(dispose => dispose())
})

describe('closing plugin panes', () => {
  it('hides an opted-in sole pane until explicit reveal without unloading its other contributions', () => {
    const pane = {
      area: 'panes',
      data: { placement: 'right', closeBehavior: 'hide' } satisfies PaneData,
      id: 'single:pane',
      render: () => null,
      source: 'plugin:single',
      title: 'Single'
    }

    disposers.push(registry.register(pane))
    disposers.push(registry.register({ area: 'sidebar.nav', id: 'single:nav', source: 'plugin:single' }))
    disposers.push(registry.register({ area: 'panes', id: 'files', data: { placement: 'right' } }))
    $layoutTree.set(
      split('row', [
        group(['workspace'], { id: 'g-main' }),
        group(['files', pane.id], { active: pane.id, id: 'g-right' })
      ])
    )

    closeTreePane(pane.id)

    expect(setPluginEnabled).not.toHaveBeenCalled()
    expect($dismissedPanes.get()).toContain(pane.id)
    expect(allPaneIds($layoutTree.get()!)).not.toContain(pane.id)
    expect(registry.getArea('sidebar.nav').some(c => c.id === 'single:nav')).toBe(true)
    expect(registry.getArea('panes').some(c => c.id === pane.id)).toBe(true)

    // Re-registration and adoption must respect the persisted dismissal.
    disposers.push(registry.register(pane))
    watchContributedPanes()
    expect(isPaneVisible(pane.id)).toBe(false)
    expect(allPaneIds($layoutTree.get()!)).not.toContain(pane.id)

    revealTreePane(pane.id)

    expect($dismissedPanes.get()).not.toContain(pane.id)
    expect(isPaneVisible(pane.id)).toBe(true)
    expect(allPaneIds($layoutTree.get()!)).toContain('files')
    expect(setPluginEnabled).not.toHaveBeenCalled()
  })

  it('dismisses one pane without disabling a plugin that contributes multiple panes', () => {
    registerPluginPane('bots', 'bots:pane')
    registerPluginPane('bots', 'bots:routines')
    $layoutTree.set(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'g-main' }),
        group(['bots:pane'], { active: 'bots:pane', id: 'g-bots' }),
        group(['bots:routines'], { active: 'bots:routines', id: 'g-routines' })
      ])
    )

    closeTreePane('bots:routines')

    expect(allPaneIds($layoutTree.get()!)).not.toContain('bots:routines')
    expect(allPaneIds($layoutTree.get()!)).toContain('bots:pane')
    expect($dismissedPanes.get()).toContain('bots:routines')
    expect(setPluginEnabled).not.toHaveBeenCalled()

    // Any later registry mutation runs pane adoption. The dismissal must
    // survive that cycle instead of immediately resurrecting Cronjobs.
    registerPluginPane('other', 'other:pane')
    expect(allPaneIds($layoutTree.get()!)).not.toContain('bots:routines')
  })

  it('keeps an opted-in pane dismissed across a store reload until reveal', async () => {
    const pane = {
      area: 'panes',
      id: 'persistent:pane',
      source: 'plugin:persistent',
      data: { placement: 'right', closeBehavior: 'hide' } satisfies PaneData
    }

    disposers.push(registry.register(pane))
    $layoutTree.set(group(['workspace', pane.id], { active: pane.id }))
    closeTreePane(pane.id)

    vi.resetModules()
    const reloaded = await import('./store')
    const { registry: reloadedRegistry } = await import('@/contrib/registry')
    disposers.push(reloadedRegistry.register(pane))
    disposers.push(reloadedRegistry.register({ area: 'panes', id: 'workspace', data: { placement: 'main' } }))
    reloaded.watchContributedPanes()

    expect(reloaded.$dismissedPanes.get()).toContain(pane.id)
    expect(allPaneIds(reloaded.$layoutTree.get()!)).not.toContain(pane.id)

    reloaded.revealTreePane(pane.id)

    expect(reloaded.$dismissedPanes.get()).not.toContain(pane.id)
    expect(reloaded.isPaneVisible(pane.id)).toBe(true)
    expect(setPluginEnabled).not.toHaveBeenCalled()
  })

  it('dismisses a sole plugin pane even without close metadata', () => {
    registerPluginPane('single', 'single:pane')
    $layoutTree.set(group(['workspace', 'single:pane'], { active: 'single:pane', id: 'g-single' }))

    closeTreePane('single:pane')

    expect(setPluginEnabled).not.toHaveBeenCalled()
    expect(allPaneIds($layoutTree.get()!)).not.toContain('single:pane')
    expect($dismissedPanes.get()).toContain('single:pane')
  })
})
