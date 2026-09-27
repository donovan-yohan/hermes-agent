import { useStore } from '@nanostores/react'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { setPluginEnabled } from '@/contrib/plugins-store'
import { registry } from '@/contrib/registry'
import { stubMenuDomApis, stubResizeObserver } from '@/test/jsdom'

import { allPaneIds, findGroup, group, split } from '../model'
import {
  $collapsedTreeSides,
  $dismissedPanes,
  $hiddenTreePanes,
  $layoutTree,
  setTreeSideCollapsed,
  togglePaneVisible
} from '../store'

import { TreeGroup } from './tree-group'

vi.mock('@/contrib/plugins-store', () => ({ setPluginEnabled: vi.fn() }))
const disposers: (() => void)[] = []
const paneId = 'github:pane'

beforeAll(() => {
  stubResizeObserver()
  stubMenuDomApis()
  vi.stubGlobal('CSS', { ...globalThis.CSS, escape: (value: string) => value })
})
beforeEach(() => {
  localStorage.clear()
  $dismissedPanes.set(new Set())
  $hiddenTreePanes.set(new Set())
  $collapsedTreeSides.set(new Set())
  vi.mocked(setPluginEnabled).mockClear()
})
afterEach(() => {
  cleanup()
  disposers.splice(0).forEach(dispose => dispose())
})

function Zone() {
  const tree = useStore($layoutTree)
  const node = tree && findGroup(tree, 'right')

  return node ? <TreeGroup node={node} parentAxis="row" /> : null
}

function mount(closeBehavior?: 'hide') {
  disposers.push(
    registry.register({
      area: 'panes',
      id: paneId,
      source: 'plugin:github',
      title: 'GitHub',
      data: { placement: 'right', dock: { pane: 'files', pos: 'center' }, closeBehavior },
      render: () => <div>GitHub content</div>
    })
  )
  disposers.push(registry.register({ area: 'sidebar.nav', id: 'github:nav', source: 'plugin:github' }))
  disposers.push(registry.register({ area: 'titlebar.right', id: 'github:toggle', source: 'plugin:github' }))
  disposers.push(
    registry.register({ area: 'panes', id: 'files', title: 'Files', data: { placement: 'right' }, render: () => null })
  )
  $layoutTree.set(
    split('row', [
      group(['workspace'], { id: 'main' }),
      group(['files', paneId], { id: 'right', active: paneId, headerHidden: false })
    ])
  )
  render(<Zone />)
}

function assertHidden() {
  expect(setPluginEnabled).not.toHaveBeenCalled()
  expect($dismissedPanes.get()).toContain(paneId)
  expect(allPaneIds($layoutTree.get()!)).not.toContain(paneId)
  expect(registry.getArea('sidebar.nav').some(c => c.id === 'github:nav')).toBe(true)
  expect(registry.getArea('titlebar.right').some(c => c.id === 'github:toggle')).toBe(true)
  expect(registry.getArea('panes').some(c => c.id === paneId)).toBe(true)
}

describe.each([undefined, 'hide'] as const)('native plugin close controls (metadata=%s)', closeBehavior => {
  it('tab X hides, preserves contributions, and supports repeated titlebar toggle', () => {
    mount(closeBehavior)
    const tab = globalThis.document.querySelector<HTMLElement>(`[data-tree-tab="${paneId}"]`)!
    fireEvent.click(within(tab).getByRole('button', { name: /close/i }))
    assertHidden()
    act(() => togglePaneVisible(paneId))
    expect(screen.getByText('GitHub content')).toBeTruthy()
    expect(allPaneIds($layoutTree.get()!)).toContain('files')
    act(() => togglePaneVisible(paneId))
    assertHidden()
    act(() => setTreeSideCollapsed('right', true))
    act(() => setTreeSideCollapsed('right', false))
    assertHidden()
    act(() => togglePaneVisible(paneId))
    expect(screen.getByText('GitHub content')).toBeTruthy()
  })

  it.each(['middle-click', 'meta-click'] as const)('%s hides without disabling', gesture => {
    mount(closeBehavior)
    const tab = globalThis.document.querySelector<HTMLElement>(`[data-tree-tab="${paneId}"]`)!

    if (gesture === 'middle-click') {
      fireEvent.pointerDown(tab, { button: 1 })
      fireEvent.pointerUp(tab, { button: 1 })
    } else {
      fireEvent.pointerDown(tab, { button: 0, metaKey: true })
    }

    assertHidden()
  })

  it.each(['Close', 'Close Others', 'Close to the Right', 'Close All'])(
    'context %s hides without disabling',
    async label => {
      mount(closeBehavior)
      const target = label === 'Close Others' || label === 'Close to the Right' ? 'files' : paneId
      const tab = globalThis.document.querySelector<HTMLElement>(`[data-tree-tab="${target}"]`)!
      fireEvent.pointerDown(tab, { button: 2, pointerType: 'mouse' })
      fireEvent.contextMenu(tab, { button: 2 })
      fireEvent.click(await screen.findByRole('menuitem', { name: new RegExp(`^${label}$`, 'i') }))
      assertHidden()
    }
  )
})
