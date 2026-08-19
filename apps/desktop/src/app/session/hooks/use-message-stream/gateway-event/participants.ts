import { toMessageParticipant } from '@/lib/chat-messages'
import { coerceGatewayText } from '@/lib/chat-runtime'

import type { GatewayEventContext } from './types'

/** External participant rows stream beside Hermes without changing Hermes turn state. */
export function handleParticipantEvent(ctx: GatewayEventContext): boolean {
  const { deps, event, payload, sessionId, occurredAt } = ctx

  if (event.type === 'participant.user_message') {
    if (sessionId) {
      deps.appendParticipantUserMessage(sessionId, coerceGatewayText(payload?.text), payload?.row_id, occurredAt)
    }

    return true
  }

  if (event.type === 'participant.message.start') {
    const participant = toMessageParticipant(payload?.participant)

    if (sessionId && participant && payload?.participant_turn_id) {
      deps.flushQueuedDeltas(sessionId)
      deps.beginParticipantMessage(sessionId, payload.participant_turn_id, participant, payload.row_id, occurredAt)
    }

    return true
  }

  if (event.type === 'participant.message.delta') {
    if (sessionId && payload?.participant_turn_id) {
      deps.appendParticipantDelta(
        sessionId,
        payload.participant_turn_id,
        coerceGatewayText(payload?.text),
        occurredAt
      )
    }

    return true
  }

  if (event.type === 'participant.message.complete') {
    if (sessionId && payload?.participant_turn_id) {
      deps.completeParticipantMessage(
        sessionId,
        payload.participant_turn_id,
        {
          status: payload.status,
          text: coerceGatewayText(payload.text),
          error: typeof payload.error === 'string' ? payload.error : undefined
        },
        occurredAt
      )
    }

    return true
  }

  return false
}
