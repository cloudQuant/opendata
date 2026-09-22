/**
 * Application constants configuration.
 * Centralized configuration for pagination, websocket, timeouts, etc.
 */

export const PAGINATION = {
  DEFAULT_PAGE_SIZE: 20,
  MAX_PAGE_SIZE: 100,
  PAGE_SIZE_OPTIONS: [10, 20, 50, 100] as const,
} as const

export const WEBSOCKET = {
  BASE_DELAY: 1000,
  MAX_DELAY: 30000,
  PING_INTERVAL: 25000,
  RECONNECT_ATTEMPTS: 5,
} as const

export const API = {
  TIMEOUT: 30000,
  BASE_URL: '/api/v1',
} as const

export const AUTH = {
  ACCESS_TOKEN_EXPIRE_MINUTES: 1440,
  REFRESH_TOKEN_EXPIRE_DAYS: 30,
} as const

export const CACHE = {
  DEFAULT_TTL: 5 * 60 * 1000,
  MAX_TTL: 30 * 60 * 1000,
} as const

export const DEBOUNCE = {
  SEARCH_DELAY: 300,
  INPUT_DELAY: 200,
} as const
