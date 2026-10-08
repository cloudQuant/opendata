import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import type { AuthUser, LoginRequest, RegisterRequest } from '@/types'
import { authApi } from '@/api/auth'
import { useStoreAction } from '@/composables/useStoreAction'

export const useAuthStore = defineStore(
  'auth',
  () => {
    const user = ref<AuthUser | null>(null)
    const accessToken = ref<string | null>(null)
    const refreshToken = ref<string | null>(null)
    const actionHelper = useStoreAction()

    const isAuthenticated = computed(() => !!user.value && !!accessToken.value)
    const isAdmin = computed(() => user.value?.role?.toLowerCase() === 'admin')
    const loading = computed(() => actionHelper.loading.value)
    const error = computed(() => actionHelper.error.value)

    async function login(credentials: LoginRequest): Promise<void> {
      await actionHelper.execute(
        async () => {
          const response = await authApi.login(credentials)
          accessToken.value = response.access_token
          refreshToken.value = response.refresh_token
          user.value = response.user
          return response
        },
        {
          errorMessage: '登录失败',
          onSuccess: () => {
            ElMessage.success('登录成功')
          },
        }
      )
    }

    async function register(data: RegisterRequest): Promise<void> {
      await actionHelper.execute(
        async () => {
          const response = await authApi.register(data)
          user.value = null
          accessToken.value = response.access_token
          refreshToken.value = response.refresh_token
          try {
            user.value = await authApi.getCurrentUser()
          } catch (error) {
            user.value = null
            accessToken.value = null
            refreshToken.value = null
            throw error
          }
          return response
        },
        {
          errorMessage: '注册失败',
          onSuccess: () => {
            ElMessage.success('注册成功')
          },
        }
      )
    }

    async function refreshAccessToken(): Promise<void> {
      if (!refreshToken.value) {
        throw new Error('No refresh token available')
      }
      await actionHelper.execute(
        async () => {
          const response = await authApi.refreshToken(refreshToken.value!)
          accessToken.value = response.access_token
          if (response.refresh_token) {
            refreshToken.value = response.refresh_token
          }
          return response
        },
        {
          errorMessage: 'Token刷新失败',
          onError: () => {
            logout()
          },
        }
      )
    }

    function logout(): void {
      user.value = null
      accessToken.value = null
      refreshToken.value = null
      actionHelper.reset()
    }

    function setUser(userData: AuthUser): void {
      user.value = userData
    }

    return {
      user,
      accessToken,
      refreshToken,
      loading,
      error,
      isAuthenticated,
      isAdmin,
      login,
      register,
      refreshAccessToken,
      logout,
      setUser,
    }
  },
  {
    persist: {
      paths: ['user', 'accessToken', 'refreshToken'],
    },
  }
)
