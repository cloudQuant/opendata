import request from '@/utils/request'
import type {
  User,
  PaginationParams,
  PaginatedResponse,
} from '@/types'

export const usersApi = {
  // Get user list (admin only)
  list(params?: PaginationParams & { search?: string }): Promise<PaginatedResponse<User>> {
    return request({
      url: '/users/',
      method: 'GET',
      params,
    })
  },

  // Get user detail
  getDetail(userId: number): Promise<User> {
    return request({
      url: `/users/${userId}`,
      method: 'GET',
    })
  },

  // `PUT /users/{id}` is the registered route (opendata/api/users.py, admin only);
  // `UserUpdateRequest` takes `role`, so a partial body changes just the role.
  updateRole(userId: number, role: 'admin' | 'user'): Promise<User> {
    return request({
      url: `/users/${userId}`,
      method: 'PUT',
      data: { role },
    })
  },

  // Delete user (admin only)
  delete(userId: number): Promise<void> {
    return request({
      url: `/users/${userId}`,
      method: 'DELETE',
    })
  },
}
