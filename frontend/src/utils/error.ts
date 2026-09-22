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
  detail?: string
  message?: string
}

/**
 * Extract error message from API/Axios errors.
 * Handles error.response.data.detail and error.response.data.message.
 */
export function getApiErrorMessage(error: unknown, fallback = '请求失败'): string {
  if (error && typeof error === 'object' && 'response' in error) {
    const data = (error as { response?: { data?: ApiErrorPayload } }).response?.data
    return data?.detail ?? data?.message ?? fallback
  }
  return getErrorMessage(error) || fallback
}
