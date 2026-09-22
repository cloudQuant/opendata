import axios, {
  type AxiosInstance,
  type AxiosError,
  type InternalAxiosRequestConfig,
} from 'axios'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { logger } from '@/utils/logger'

/** API error response shape */
interface ApiErrorData {
  message?: string
  detail?: string
}

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
    // Unwrap data field for convenience
    return res.data ?? res
  },
  async (error: AxiosError) => {
    const authStore = useAuthStore()

    if (error.response) {
      switch (error.response.status) {
        case 401: {
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
              authStore.logout()
              window.location.href = '/login'
            } finally {
              isRefreshing = false
            }
          } else {
            authStore.logout()
            window.location.href = '/login'
          }
          break
        }
        case 403:
          ElMessage.error('权限不足')
          break
        case 404:
          ElMessage.error('请求的资源不存在')
          break
        case 429:
          ElMessage.error('请求过于频繁，请稍后再试')
          break
        case 500:
          ElMessage.error('服务器错误，请稍后再试')
          break
        default: {
          const data = error.response?.data as ApiErrorData | undefined
          ElMessage.error(data?.message ?? data?.detail ?? '请求失败')
        }
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
