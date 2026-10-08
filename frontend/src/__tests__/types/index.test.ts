import { describe, it, expect } from 'vitest'
import type {
  ApiResponse,
  PaginatedResponse,
  User,
  AuthUser,
  CurrentAuthUser,
  LoginRequest,
  RegisterRequest,
  AuthResponse,
  RegisterResponse,
  RefreshTokenResponse,
  DataScript,
  Task,
  DataTable,
  ExecutionStats,
} from '@/types'

describe('TypeScript Types', () => {
  it('ApiResponse type is valid', () => {
    const response: ApiResponse = {
      success: true,
      message: 'ok',
      data: { key: 'value' },
    }
    expect(response.success).toBe(true)
    expect(response.message).toBe('ok')
  })

  it('User type is valid', () => {
    const user: User = {
      id: 1,
      email: 'test@example.com',
      role: 'admin',
      is_active: true,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }
    expect(user.id).toBe(1)
    expect(user.role).toBe('admin')
  })

  it('LoginRequest type is valid', () => {
    const req: LoginRequest = { email: 'test@example.com', password: 'pass123' }
    expect(req.email).toBe('test@example.com')
  })

  it('RegisterRequest type is valid', () => {
    const req: RegisterRequest = {
      email: 'test@example.com',
      password: 'pass123',
      password_confirm: 'pass123',
    }
    expect(req.password).toBe(req.password_confirm)
  })

  it('AuthResponse type is valid', () => {
    const user: AuthUser = {
      user_id: 9,
      email: 'test@example.com',
      role: 'user',
      created_at: '2026-10-08T00:00:00Z',
      updated_at: null,
    }
    const res: AuthResponse = {
      access_token: 'token',
      refresh_token: 'refresh',
      require_password_change: false,
      user,
    }
    expect(res.access_token).toBe('token')
    expect(res.require_password_change).toBe(false)
    expect(res.user.user_id).toBe(9)
    expect(res.user.updated_at).toBeNull()
  })

  it('registration, current-profile, and refresh types match their endpoint payloads', () => {
    const registration: RegisterResponse = {
      user_id: 10,
      email: 'new@example.com',
      access_token: 'registered-access',
      refresh_token: 'registered-refresh',
    }
    const profile: CurrentAuthUser = {
      user_id: registration.user_id,
      email: registration.email,
      role: 'user',
      is_active: true,
      created_at: '2026-10-08T00:00:00Z',
      updated_at: null,
    }
    const refreshed: RefreshTokenResponse = {
      access_token: 'new-access',
      refresh_token: 'new-refresh',
    }

    expect(registration.user_id).toBe(10)
    expect(profile.is_active).toBe(true)
    expect(refreshed.refresh_token).toBe('new-refresh')
  })

  it('DataScript type is valid', () => {
    const script: DataScript = {
      id: 1,
      script_id: 'test_script',
      script_name: 'Test Script',
      category: 'stock',
      sub_category: null,
      frequency: 'daily',
      description: null,
      source: 'akshare',
      target_table: null,
      module_path: null,
      function_name: null,
      estimated_duration: 60,
      timeout: 300,
      is_active: true,
      is_custom: false,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }
    expect(script.script_id).toBe('test_script')
  })

  it('Task type is valid', () => {
    const task: Task = {
      id: 1,
      name: 'Test Task',
      description: null,
      user_id: 1,
      task_kind: 'script',
      script_id: 'test',
      script_name: 'Test',
      schedule_type: 'cron',
      schedule_expression: '0 8 * * *',
      parameters: {},
      is_active: true,
      retry_on_failure: true,
      max_retries: 3,
      timeout: 0,
      last_execution_at: null,
      next_execution_at: null,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }
    expect(task.name).toBe('Test Task')
  })

  it('pipeline Task supports a null script identity', () => {
    const task: Task = {
      id: 2,
      name: 'Pipeline Task',
      description: null,
      user_id: 1,
      task_kind: 'pipeline',
      script_id: null,
      script_name: null,
      schedule_type: 'cron',
      schedule_expression: '0 2 * * *',
      parameters: { domain: 'stock_daily' },
      is_active: true,
      retry_on_failure: true,
      max_retries: 3,
      timeout: 0,
      last_execution_at: null,
      next_execution_at: null,
      created_at: '2026-09-30',
      updated_at: '2026-09-30',
    }
    expect(task.script_id).toBeNull()
    expect(task.task_kind).toBe('pipeline')
  })

  it('DataTable type is valid', () => {
    const table: DataTable = {
      id: 1,
      table_name: 'ak_test',
      table_comment: 'Test table',
      category: 'stock',
      script_id: 'test',
      row_count: 100,
      last_update_time: null,
      last_update_status: null,
      created_at: '2024-01-01',
      updated_at: '2024-01-01',
    }
    expect(table.table_name).toBe('ak_test')
  })

  it('PaginatedResponse type is valid', () => {
    const paginated: PaginatedResponse<User> = {
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
    }
    expect(paginated.total).toBe(0)
  })

  it('ExecutionStats type is valid', () => {
    const stats: ExecutionStats = {
      total_count: 100,
      success_count: 90,
      failed_count: 10,
      success_rate: 0.9,
      avg_duration: 5.5,
      today_executions: 5,
    }
    expect(stats.success_rate).toBe(0.9)
  })
})
