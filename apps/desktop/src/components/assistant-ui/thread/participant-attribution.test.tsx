// An external agent participant's reply renders as an ORDINARY assistant row
// wearing an attribution header (participant-seam-v1 §5): same bubble, same
// streaming affordances, but it says who spoke. Hermes's own rows are
// untouched — no header, full action footer.
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Thread } from '.'

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', TestResizeObserver)
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
  window.setTimeout(() => callback(performance.now()), 0)
)
vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
vi.stubGlobal('CSS', { escape: (str: string) => str })

Element.prototype.scrollTo = function scrollTo() {}

afterEach(() => {
  cleanup()
})

const createdAt = new Date('2026-05-01T00:00:00.000Z')

const PARTICIPANT = {
  id: 'claude:default',
  handle: 'claude',
  displayName: 'Claude Code'
}

function userMessage(): ThreadMessage {
  return {
    id: 'user-1',
    role: 'user',
    content: [{ type: 'text', text: '@claude take a look' }],
    attachments: [],
    createdAt,
    metadata: { custom: {} }
  } as unknown as ThreadMessage
}

function assistantMessage(custom: Record<string, unknown> = {}): ThreadMessage {
  return {
    id: 'assistant-1',
    role: 'assistant',
    content: [{ type: 'text', text: 'Looked. Ship it.' }],
    status: { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: {
      unstable_state: null,
      unstable_annotations: [],
      unstable_data: [],
      steps: [],
      custom
    }
  } as unknown as ThreadMessage
}

function Harness({ assistant }: { assistant: ThreadMessage }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [userMessage(), assistant],
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread onBranchInNewChat={() => undefined} />
    </AssistantRuntimeProvider>
  )
}

describe('participant attribution', () => {
  it('names the external agent above its reply', async () => {
    const { container } = render(<Harness assistant={assistantMessage({ participant: PARTICIPANT })} />)

    await screen.findByText('Looked. Ship it.')

    const header = container.querySelector('[data-slot="aui_participant-attribution"]')

    expect(header).not.toBeNull()
    expect(header?.textContent).toContain('Claude Code')
    expect(header?.textContent).toContain('@claude')
    expect(header?.getAttribute('aria-label')).toBe('Reply from Claude Code (@claude)')
  })

  it('hides the Hermes turn actions on a participant row', async () => {
    render(<Harness assistant={assistantMessage({ participant: PARTICIPANT })} />)

    await screen.findByText('Looked. Ship it.')

    // Refresh would re-run HERMES's turn, not the participant's.
    expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Branch in new chat' })).toBeNull()
  })

  it('leaves an ordinary Hermes reply unattributed and fully actionable', async () => {
    const { container } = render(<Harness assistant={assistantMessage()} />)

    await screen.findByText('Looked. Ship it.')

    expect(container.querySelector('[data-slot="aui_participant-attribution"]')).toBeNull()
    expect(await screen.findByRole('button', { name: 'Refresh' })).toBeTruthy()
  })
})
