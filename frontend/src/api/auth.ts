import request from '@/utils/request'
import type { AuthResponse, LoginRequest, RegisterRequest } from '@/types'

export const authApi = {
  // Login
  login(credentials: LoginRequest): Promise<AuthResponse> {
    return request({
      url: '/auth/login',
      method: 'POST',
      data: credentials,
    })
  },

  // Register
  register(data: RegisterRequest): Promise<AuthResponse> {
    return request({
      url: '/auth/register',
      method: 'POST',
      data,
    })
  },

  // Refresh token
  refreshToken(refreshToken: string): Promise<AuthResponse> {
    return request({
      url: '/auth/refresh',
      method: 'POST',
      data: { refresh_token: refreshToken },
    })
  },

  // Logout
  logout(): Promise<void> {
    return request({
      url: '/auth/logout',
      method: 'POST',
    })
  },

  // Get current user
  getCurrentUser(): Promise<AuthResponse['user']> {
    return request({
      url: '/auth/me',
      method: 'GET',
    })
  },
}
