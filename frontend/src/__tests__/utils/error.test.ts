import { describe, expect, it } from 'vitest'
import { getApiErrorMessage } from '@/utils/error'

describe('getApiErrorMessage validation details', () => {
  it('renders FastAPI missing-field details without leaking the raw object', () => {
    const message = getApiErrorMessage({
      response: {
        data: {
          detail: [
            {
              type: 'missing',
              loc: ['body', 'interface_id'],
              msg: 'Field required',
              input: { parameters: {} },
            },
          ],
        },
      },
    })

    expect(message).toBe('缺少必填字段：interface_id')
    expect(message).not.toContain('[object Object]')
    expect(message).not.toContain('parameters')
  })

  it('ignores malformed validation entries and falls back to the API message', () => {
    const message = getApiErrorMessage({
      response: { data: { detail: [{ loc: ['body'], msg: null }], message: '请求无效' } },
    })
    expect(message).toBe('请求无效')
  })
})
