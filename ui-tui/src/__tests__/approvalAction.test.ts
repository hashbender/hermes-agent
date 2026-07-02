import { describe, expect, it } from 'vitest'

import { approvalAction, approvalFullReviewMessage, approvalOverflowMessage } from '../components/prompts.js'

describe('approvalAction — pure key dispatch for ApprovalPrompt', () => {
  it('maps Esc to deny — parity with global Ctrl+C cancellation', () => {
    expect(approvalAction('', { escape: true }, 0)).toEqual({ kind: 'choose', choice: 'deny' })
    expect(approvalAction('', { escape: true }, 2)).toEqual({ kind: 'choose', choice: 'deny' })
  })

  it('maps number keys 1..4 to once/session/always/deny in registration order', () => {
    expect(approvalAction('1', {}, 0)).toEqual({ kind: 'choose', choice: 'once' })
    expect(approvalAction('2', {}, 0)).toEqual({ kind: 'choose', choice: 'session' })
    expect(approvalAction('3', {}, 0)).toEqual({ kind: 'choose', choice: 'always' })
    expect(approvalAction('4', {}, 0)).toEqual({ kind: 'choose', choice: 'deny' })
  })

  it('ignores out-of-range numbers', () => {
    expect(approvalAction('0', {}, 1)).toEqual({ kind: 'noop' })
    expect(approvalAction('5', {}, 1)).toEqual({ kind: 'noop' })
    expect(approvalAction('9', {}, 1)).toEqual({ kind: 'noop' })
  })

  it('confirms the current selection on Enter', () => {
    expect(approvalAction('', { return: true }, 0)).toEqual({ kind: 'choose', choice: 'once' })
    expect(approvalAction('', { return: true }, 3)).toEqual({ kind: 'choose', choice: 'deny' })
  })

  it('moves selection up/down within bounds', () => {
    expect(approvalAction('', { upArrow: true }, 2)).toEqual({ kind: 'move', delta: -1 })
    expect(approvalAction('', { downArrow: true }, 1)).toEqual({ kind: 'move', delta: 1 })
  })

  it('clamps selection movement at the edges', () => {
    expect(approvalAction('', { upArrow: true }, 0)).toEqual({ kind: 'noop' })
    expect(approvalAction('', { downArrow: true }, 3)).toEqual({ kind: 'noop' })
  })

  it('offers a full-payload review row only when the command preview overflows', () => {
    expect(approvalAction('v', {}, 0)).toEqual({ kind: 'noop' })
    expect(approvalAction('5', {}, 0)).toEqual({ kind: 'noop' })
    expect(approvalAction('', { downArrow: true }, 3)).toEqual({ kind: 'noop' })

    expect(approvalAction('v', {}, 0, undefined, true)).toEqual({ kind: 'toggleFull' })
    expect(approvalAction('V', {}, 0, undefined, true)).toEqual({ kind: 'toggleFull' })
    expect(approvalAction('5', {}, 0, undefined, true)).toEqual({ kind: 'toggleFull' })
    expect(approvalAction('', { return: true }, 4, undefined, true)).toEqual({ kind: 'toggleFull' })
    expect(approvalAction('', { downArrow: true }, 3, undefined, true)).toEqual({ kind: 'move', delta: 1 })
    expect(approvalAction('', { downArrow: true }, 4, undefined, true)).toEqual({ kind: 'noop' })
  })

  it('uses approval-local overflow copy instead of the old scrollback fallback', () => {
    expect(approvalOverflowMessage(1)).toBe('… +1 more line hidden - select View full payload to review here')
    expect(approvalOverflowMessage(3)).toBe('… +3 more lines hidden - select View full payload to review here')
    expect(approvalOverflowMessage(3)).not.toContain('full text above')
    expect(approvalFullReviewMessage()).toBe('Full payload shown in this prompt')
  })

  it('Esc beats numeric/return — denying is always the first interpretation', () => {
    // If a terminal somehow delivers Esc + a digit in the same event, deny
    // wins.  Documents the precedence so a future refactor doesn't flip it.
    expect(approvalAction('1', { escape: true }, 0, undefined, true)).toEqual({ kind: 'choose', choice: 'deny' })
    expect(approvalAction('v', { escape: true }, 0, undefined, true)).toEqual({ kind: 'choose', choice: 'deny' })
    expect(approvalAction('', { escape: true, return: true }, 1, undefined, true)).toEqual({
      kind: 'choose',
      choice: 'deny'
    })
  })

  it('returns noop for unrelated keystrokes (printable letters etc.)', () => {
    expect(approvalAction('a', {}, 0)).toEqual({ kind: 'noop' })
    expect(approvalAction(' ', {}, 0)).toEqual({ kind: 'noop' })
  })

  it('respects a reduced option set when permanent allow is disabled', () => {
    // tirith content-security warning present → no "always"; the 3-item set is
    // once/session/deny, so 3 maps to deny and 4 is out of range.
    const opts = ['once', 'session', 'deny'] as const

    expect(approvalAction('3', {}, 0, opts)).toEqual({ kind: 'choose', choice: 'deny' })
    expect(approvalAction('4', {}, 0, opts)).toEqual({ kind: 'noop' })
    expect(approvalAction('', { downArrow: true }, 2, opts)).toEqual({ kind: 'noop' })
    expect(approvalAction('', { return: true }, 2, opts)).toEqual({ kind: 'choose', choice: 'deny' })

    expect(approvalAction('4', {}, 0, opts, true)).toEqual({ kind: 'toggleFull' })
    expect(approvalAction('', { return: true }, 3, opts, true)).toEqual({ kind: 'toggleFull' })
  })
})
