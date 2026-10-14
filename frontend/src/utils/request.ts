import axios, {
  type AxiosInstance,
  type AxiosError,
  type InternalAxiosRequestConfig,
} from 'axios'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { logger } from '@/utils/logger'
import { getApiErrorMessage } from '@/utils/error'

// Create axios instance
const request: AxiosInstance = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// Token refresh lock – prevents concurrent refresh requests
let isRefreshing = false
let refreshSubscribers: Array<(token: string) => void> = []
const SESSION_EXPIRED_LOGIN_PATH = '/login?reason=session_expired'
const PUBLIC_AUTH_POST_PATHS = new Set(['/auth/login', '/auth/register'])

function isPublicAuthPost(config: InternalAxiosRequestConfig | undefined): boolean {
  if (!config || (config.method ?? 'get').toLowerCase() !== 'post' || !config.url) return false

  try {
    const fallbackOrigin = window.location.origin
    const baseUrl = new URL(config.baseURL || fallbackOrigin, fallbackOrigin)
    const basePath = baseUrl.pathname.replace(/\/+$/, '')
    const requestUrl = /^https?:\/\//i.test(config.url)
      ? new URL(config.url)
      : new URL(`${basePath}/${config.url.replace(/^\/+/, '')}`, baseUrl.origin)
    const expectedPaths = new Set(
      Array.from(PUBLIC_AUTH_POST_PATHS, (path) => `${basePath}${path}` || path)
    )
    return requestUrl.origin === baseUrl.origin && expectedPaths.has(requestUrl.pathname)
  } catch {
    return false
  }
}

function endSessionAndRedirect(authStore: ReturnType<typeof useAuthStore>): void {
  authStore.logout()
  window.location.href = SESSION_EXPIRED_LOGIN_PATH
}

function onTokenRefreshed(token: string) {
  refreshSubscribers.forEach((cb) => cb(token))
  refreshSubscribers = []
}

function addRefreshSubscriber(cb: (token: string) => void) {
  refreshSubscribers.push(cb)
}

// Request interceptor
request.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const authStore = useAuthStore()
    if (authStore.accessToken) {
      config.headers.Authorization = `Bearer ${authStore.accessToken}`
    }
    return config
  },
  (error: AxiosError) => {
    return Promise.reject(error)
  }
)

// Response interceptor
request.interceptors.response.use(
  (response) => {
    const res = response.data
    // API standard response format: { success: true/false, message: "...", data: {...} }
    if (res.success === false) {
      ElMessage.error(res.message || '请求失败')
      return Promise.reject(new Error(res.message || '请求失败'))
    }
    // Unwrap data field for convenience. Callers therefore type the resolved
    // value, not the response: `request.get<unknown, Payload>(...)`.
    return res.data ?? res
  },
  async (error: AxiosError) => {
    if (axios.isCancel(error)) {
      return Promise.reject(error)
    }

    const authStore = useAuthStore()

    if (error.response) {
      switch (error.response.status) {
        case 401: {
          // Invalid credentials are a normal login failure, not an expired
          // authenticated session. Leave the response for the caller to show.
          if (isPublicAuthPost(error.config)) break

          // Prevent infinite loop: check if this is a refresh token request
          const isRefreshRequest = error.config?.url?.includes('/refresh')

          if (!isRefreshRequest && authStore.refreshToken) {
            if (isRefreshing) {
              // Another request is already refreshing – wait for it
              return new Promise((resolve) => {
                addRefreshSubscriber((newToken: string) => {
                  if (error.config) {
                    error.config.headers.Authorization = `Bearer ${newToken}`
                    resolve(request(error.config))
                  }
                })
              })
            }

            isRefreshing = true
            try {
              await authStore.refreshAccessToken()
              const newToken = authStore.accessToken || ''
              onTokenRefreshed(newToken)
              // Retry original request with new token
              if (error.config) {
                error.config.headers.Authorization = `Bearer ${newToken}`
                return request(error.config)
              }
            } catch (refreshError) {
              // Token refresh failed - log and force re-login
              logger.error('Token refresh failed', { error: refreshError })
              refreshSubscribers = []
              endSessionAndRedirect(authStore)
            } finally {
              isRefreshing = false
            }
          } else {
            endSessionAndRedirect(authStore)
          }
          break
        }
        case 403:
          ElMessage.error('权限不足')
          break
        case 404:
          ElMessage.error('请求的资源不存在')
          break
        case 422:
          ElMessage.error(getApiErrorMessage(error))
          break
        case 429:
          ElMessage.error('请求过于频繁，请稍后再试')
          break
        case 500:
          ElMessage.error('服务器错误，请稍后再试')
          break
        default:
          ElMessage.error(getApiErrorMessage(error))
      }
    } else if (error.request) {
      ElMessage.error('网络错误，请检查网络连接')
    } else {
      ElMessage.error('请求配置错误')
    }

    return Promise.reject(error)
  }
)

export default request
