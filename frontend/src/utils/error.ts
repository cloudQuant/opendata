/**
 * Extract a user-friendly error message from unknown error.
 * Use this instead of error: any for type-safe error handling.
 */
export function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message
  }
  if (typeof error === 'string') {
    return error
  }
  if (error && typeof error === 'object' && 'message' in error) {
    const msg = (error as { message: unknown }).message
    return typeof msg === 'string' ? msg : String(msg)
  }
  return '未知错误'
}

/** API error response shape (axios error.response.data) */
interface ApiErrorPayload {
  detail?: unknown
  message?: unknown
}

function formatValidationDetails(detail: unknown): string | undefined {
  if (!Array.isArray(detail)) return undefined

  const messages = detail.flatMap((issue: unknown) => {
    if (!issue || typeof issue !== 'object') return []

    const record = issue as { loc?: unknown; msg?: unknown }
    const message = typeof record.msg === 'string' ? record.msg.trim() : ''
    if (!message) return []

    const location = Array.isArray(record.loc)
      ? record.loc
          .filter(
            (part): part is string | number =>
              (typeof part === 'string' || typeof part === 'number') &&
              !['body', 'query', 'path', 'header'].includes(String(part))
          )
          .map(String)
          .join('.')
      : ''

    if (message === 'Field required') {
      return [location ? `缺少必填字段：${location}` : '缺少必填字段']
    }
    return [location ? `${location}：${message}` : message]
  })

  return messages.length ? messages.join('；') : undefined
}

/**
 * Extract error message from API/Axios errors.
 * Handles error.response.data.detail and error.response.data.message.
 */
export function getApiErrorMessage(error: unknown, fallback = '请求失败'): string {
  if (error && typeof error === 'object' && 'response' in error) {
    const data = (error as { response?: { data?: ApiErrorPayload } }).response?.data
    if (typeof data?.detail === 'string') return data.detail
    const validationMessage = formatValidationDetails(data?.detail)
    if (validationMessage) return validationMessage
    return typeof data?.message === 'string' ? data.message : fallback
  }
  return getErrorMessage(error) || fallback
}
