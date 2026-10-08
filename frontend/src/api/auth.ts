import request from '@/utils/request'
import type {
  AuthResponse,
  CurrentAuthUser,
  LoginRequest,
  RefreshTokenResponse,
  RegisterRequest,
  RegisterResponse,
} from '@/types'

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
  register(data: RegisterRequest): Promise<RegisterResponse> {
    return request({
      url: '/auth/register',
      method: 'POST',
      data,
    })
  },

  // Refresh token
  refreshToken(refreshToken: string): Promise<RefreshTokenResponse> {
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
  getCurrentUser(): Promise<CurrentAuthUser> {
    return request({
      url: '/auth/me',
      method: 'GET',
    })
  },
}
