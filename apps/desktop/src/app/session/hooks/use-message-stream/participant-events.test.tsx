// The four `participant.*` gateway events (participant-seam-v1 §4/§5). An
// external agent replies ALONGSIDE Hermes: its row must carry attribution,
// stream on its own turn id, and never touch Hermes's own turn bookkeeping
// (busy / awaitingResponse / streamId) — a participant talking is not Hermes
// working, and Stop aimed at Hermes must not seal somebody else's reply.
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { type ChatMessage, chatMessageText, toChatMessages } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import type { RpcEvent } from '@/types/hermes'

import { MAX_STREAM_FLUSH_GAP_MS } from './utils'

import { useMessageStream } from './index'

const SID = 'session-1'
const TURN = 'pturn-1'

// Wire shape: the gateway ships the full §1.1 object. Only the three fields
// the transcript renders survive the parser.
const PARTICIPANT = {
  id: 'claude:default',
  handle: 'claude',
  display_name: 'Claude Code',
  plugin_id: 'hermes-plugin-relay',
  adapter_id: 'claude-code-stream-json'
}

let handleEvent: ((event: RpcEvent) => void) | null = null
let latestState: ClientSessionState | null = null

function Harness({ initialState }: { initialState?: ClientSessionState }) {
  const activeSessionIdRef = useRef<string | null>(SID)

  const sessionStateByRuntimeIdRef = useRef(
    new Map<string, ClientSessionState>(initialState ? [[SID, initialState]] : [])
  )

  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const current = sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState()
      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)
      latestState = next

      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

function mountStream(initialMessages: ChatMessage[] = []) {
  const initialState = { ...createClientSessionState(), messages: initialMessages }

  latestState = initialState
  render(<Harness initialState={initialState} />)
  expect(handleEvent).not.toBeNull()
}

function emit(type: string, payload: RpcEvent['payload'] = {}) {
  act(() => handleEvent!({ payload, session_id: SID, type }))
}

// Participant text rides the shared delta coalescer, so it lands on the flush
// timer rather than per token — the same cadence Hermes's own stream uses.
async function flushDeltas() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(MAX_STREAM_FLUSH_GAP_MS)
  })
}

function messages(): ChatMessage[] {
  return latestState?.messages ?? []
}

function participantRow(): ChatMessage | undefined {
  return messages().find(message => message.participant)
}

function startTurn() {
  emit('participant.message.start', { participant: PARTICIPANT, participant_turn_id: TURN, row_id: 42, timestamp: 10 })
}

describe('participant.* gateway events', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    handleEvent = null
    latestState = null
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('paints a participant-directed human line as a user bubble', async () => {
    mountStream()

    emit('participant.user_message', { row_id: 7, text: '@claude take a look', mentions: ['claude'], timestamp: 5 })

    expect(messages()).toHaveLength(1)
    expect(messages()[0]).toMatchObject({ role: 'user', rowId: 7 })
    expect(chatMessageText(messages()[0])).toBe('@claude take a look')
  })

  it('ignores a replayed user_message for a row it already painted', async () => {
    mountStream()

    emit('participant.user_message', { row_id: 7, text: '@claude take a look', timestamp: 5 })
    emit('participant.user_message', { row_id: 7, text: '@claude take a look', timestamp: 5 })

    expect(messages()).toHaveLength(1)
  })

  it('streams an attributed assistant row keyed by the participant turn id', async () => {
    mountStream()

    startTurn()

    expect(participantRow()).toMatchObject({ role: 'assistant', pending: true, rowId: 42 })
    expect(participantRow()?.participant).toEqual({
      id: 'claude:default',
      handle: 'claude',
      displayName: 'Claude Code'
    })

    emit('participant.message.delta', { participant_turn_id: TURN, text: 'Looked. ' })
    emit('participant.message.delta', { participant_turn_id: TURN, text: 'Ship it.' })

    // Buffered, not written: both chunks land in ONE store write on the flush
    // timer. Per-token writes here are pathological — a participant turn never
    // arms `busy`, so the view sync would commit synchronously on every one.
    expect(chatMessageText(participantRow()!)).toBe('')

    await flushDeltas()

    expect(chatMessageText(participantRow()!)).toBe('Looked. Ship it.')
    expect(participantRow()?.pending).toBe(true)

    emit('participant.message.complete', {
      participant_turn_id: TURN,
      status: 'completed',
      text: 'Looked. Ship it.',
      timestamp: 12
    })

    expect(participantRow()).toMatchObject({ pending: false, completedAt: 12 })
    expect(participantRow()?.error).toBeUndefined()
    expect(chatMessageText(participantRow()!)).toBe('Looked. Ship it.')
  })

  // An adapter whose capabilities say `streaming: false` sends start →
  // complete with no deltas in between. The final text still has to land.
  it('paints the whole reply for a participant that never streamed', async () => {
    mountStream()

    startTurn()
    emit('participant.message.complete', {
      participant_turn_id: TURN,
      status: 'completed',
      text: 'One shot, whole answer.',
      timestamp: 12
    })

    expect(chatMessageText(participantRow()!)).toBe('One shot, whole answer.')
    expect(participantRow()).toMatchObject({ pending: false })
  })

  it('completes a participant row hydrated while its durable content was still empty', async () => {
    const hydrated = toChatMessages([
      {
        role: 'assistant',
        content: '',
        display_kind: 'participant_message',
        display_metadata: {
          participant: PARTICIPANT,
          participant_turn_id: TURN,
          status: 'streaming'
        },
        row_id: 42,
        timestamp: 10
      }
    ])

    mountStream(hydrated)
    emit('participant.message.complete', {
      participant_turn_id: TURN,
      status: 'completed',
      text: 'Arrived after hydration.',
      timestamp: 12
    })

    expect(participantRow()).toMatchObject({ id: 'participant-pturn-1', pending: false, rowId: 42 })
    expect(chatMessageText(participantRow()!)).toBe('Arrived after hydration.')
  })

  it('never arms Hermes turn state for a participant turn', async () => {
    mountStream()

    startTurn()
    emit('participant.message.delta', { participant_turn_id: TURN, text: 'thinking out loud' })
    await flushDeltas()

    expect(chatMessageText(participantRow()!)).toBe('thinking out loud')
    expect(latestState).toMatchObject({ awaitingResponse: false, busy: false, streamId: null })
    expect(latestState?.sawAssistantPayload).toBe(false)
  })

  // A participant-only flush must not touch mutateStream, which SEEDS a
  // Hermes bubble when the session has none — that would paint an empty
  // assistant row (and arm sawAssistantPayload) for a turn Hermes never ran.
  it('never seeds a Hermes bubble from a participant-only flush', async () => {
    mountStream()

    startTurn()
    emit('participant.message.delta', { participant_turn_id: TURN, text: 'only me talking' })
    await flushDeltas()

    expect(messages()).toHaveLength(1)
    expect(messages()[0].participant).toBeDefined()
  })

  it('keeps a participant reply out of the Hermes assistant bubble', async () => {
    mountStream()

    emit('message.start', {})
    emit('message.delta', { text: 'Hermes speaking' })
    startTurn()
    emit('participant.message.delta', { participant_turn_id: TURN, text: 'Claude speaking' })
    await flushDeltas()

    const hermesRow = messages().find(message => !message.participant && message.role === 'assistant')

    expect(chatMessageText(hermesRow!)).toBe('Hermes speaking')
    expect(chatMessageText(participantRow()!)).toBe('Claude speaking')
  })

  it('seals a failed turn with its error and keeps the partial text', async () => {
    mountStream()

    startTurn()
    emit('participant.message.delta', { participant_turn_id: TURN, text: 'I got as far as' })
    emit('participant.message.complete', {
      participant_turn_id: TURN,
      status: 'failed',
      text: 'I got as far as',
      error: 'claude exited with status 1',
      timestamp: 12
    })

    expect(participantRow()).toMatchObject({ pending: false, error: 'claude exited with status 1' })
    expect(chatMessageText(participantRow()!)).toBe('I got as far as')
  })

  it('seals an interrupted turn without inventing an error', async () => {
    mountStream()

    startTurn()
    emit('participant.message.delta', { participant_turn_id: TURN, text: 'half a th' })
    emit('participant.message.complete', { participant_turn_id: TURN, status: 'interrupted', text: 'half a th' })

    expect(participantRow()).toMatchObject({ pending: false })
    expect(participantRow()?.error).toBeUndefined()
  })

  it('names a failure the participant reported without a reason', async () => {
    mountStream()

    startTurn()
    emit('participant.message.complete', { participant_turn_id: TURN, status: 'failed', text: '' })

    expect(participantRow()?.error).toBeTruthy()
  })

  it('drops deltas and completions for a turn that never started', async () => {
    mountStream()

    emit('participant.message.delta', { participant_turn_id: 'pturn-ghost', text: 'unattributable' })
    emit('participant.message.complete', {
      participant_turn_id: 'pturn-ghost',
      status: 'completed',
      text: 'unattributable'
    })
    await flushDeltas()

    expect(messages()).toHaveLength(0)
  })

  it('drops a start whose attribution cannot be read', async () => {
    mountStream()

    emit('participant.message.start', { participant: { handle: 'claude' }, participant_turn_id: TURN })
    emit('participant.message.start', { participant_turn_id: TURN })
    emit('participant.message.start', { participant: PARTICIPANT })

    expect(messages()).toHaveLength(0)
  })

  it('ignores a duplicate start for a turn already on screen', async () => {
    mountStream()

    startTurn()
    emit('participant.message.delta', { participant_turn_id: TURN, text: 'first words' })
    startTurn()
    await flushDeltas()

    expect(messages().filter(message => message.participant)).toHaveLength(1)
    expect(chatMessageText(participantRow()!)).toBe('first words')
  })
})
