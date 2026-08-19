import { afterEach, describe, expect, it } from 'vitest'

import { registry } from '@/contrib/registry'

import { COMPOSER_AREAS, type ComposerMiddleware, isComposerDraftHandled, runComposerMiddleware } from './contrib'

const disposers: Array<() => void> = []

function addMiddleware(id: string, handler: ComposerMiddleware['handler'], order?: number) {
  disposers.push(
    registry.register({ id, area: COMPOSER_AREAS.middleware, order, data: { handler } satisfies ComposerMiddleware })
  )
}

afterEach(() => {
  disposers.splice(0).forEach(d => d())
})

describe('runComposerMiddleware', () => {
  it('passes the draft through untouched when nothing is registered', async () => {
    const draft = { text: 'hello' }

    expect(await runComposerMiddleware(draft)).toBe(draft)
  })

  it('chains rewrites in registry order', async () => {
    addMiddleware('b', d => ({ ...d, text: `${d.text}b` }), 20)
    addMiddleware('a', d => ({ ...d, text: `${d.text}a` }), 10)

    expect(await runComposerMiddleware({ text: 'x' })).toEqual({ text: 'xab' })
  })

  it('cancels the send when a handler returns null', async () => {
    addMiddleware('gate', () => null)
    addMiddleware('later', d => ({ ...d, text: 'never' }), 99)

    expect(await runComposerMiddleware({ text: 'x' })).toBeNull()
  })

  it('treats a throwing handler as pass-through', async () => {
    addMiddleware('boom', () => {
      throw new Error('broken plugin')
    })
    addMiddleware('after', d => ({ ...d, text: `${d.text}!` }), 99)

    expect(await runComposerMiddleware({ text: 'x' })).toEqual({ text: 'x!' })
  })

  it('supports async handlers', async () => {
    addMiddleware('async', async d => ({ ...d, text: d.text.toUpperCase() }))

    expect(await runComposerMiddleware({ text: 'quiet' })).toEqual({ text: 'QUIET' })
  })

  // `{ handled: true }` is the "I took this draft" outcome: the composer
  // clears and no turn starts. It is NOT `null` (cancel, draft restored).
  it('stops the chain when a handler consumes the draft', async () => {
    addMiddleware('consumer', () => ({ handled: true }))
    addMiddleware('later', d => ({ ...d, text: 'never' }), 99)

    const result = await runComposerMiddleware({ text: 'x' })

    expect(result).toEqual({ handled: true })
    expect(isComposerDraftHandled(result)).toBe(true)
  })

  it('lets earlier middleware rewrite the draft the consumer then takes', async () => {
    const seen: string[] = []

    addMiddleware('rewrite', d => ({ ...d, text: `${d.text}!` }), 10)
    addMiddleware(
      'consumer',
      d => {
        seen.push(d.text)

        return { handled: true }
      },
      20
    )

    expect(await runComposerMiddleware({ text: 'x' })).toEqual({ handled: true })
    expect(seen).toEqual(['x!'])
  })

  it('separates a consumed draft from a cancelled one', async () => {
    expect(isComposerDraftHandled(null)).toBe(false)
    expect(isComposerDraftHandled({ text: 'x' })).toBe(false)
    expect(isComposerDraftHandled({ handled: true })).toBe(true)
  })
})
