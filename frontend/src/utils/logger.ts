import { ElMessage } from 'element-plus'

type LogLevel = 'debug' | 'info' | 'warn' | 'error'

interface LogEntry {
  level: LogLevel
  message: string
  timestamp: string
  context?: Record<string, unknown>
}

const LOG_LEVELS: Record<LogLevel, number> = {
  debug: 0,
  info: 1,
  warn: 2,
  error: 3,
}

function getLogLevel(): LogLevel {
  const level = import.meta.env.VITE_LOG_LEVEL as LogLevel | undefined
  return level && LOG_LEVELS[level] !== undefined ? level : 'info'
}

function shouldLog(level: LogLevel): boolean {
  return LOG_LEVELS[level] >= LOG_LEVELS[getLogLevel()]
}

function formatTimestamp(): string {
  return new Date().toISOString()
}

function formatMessage(level: LogLevel, message: string, context?: Record<string, unknown>): string {
  const timestamp = formatTimestamp()
  const contextStr = context ? ` ${JSON.stringify(context)}` : ''
  return `[${timestamp}] [${level.toUpperCase()}] ${message}${contextStr}`
}

function logToConsole(entry: LogEntry): void {
  const formatted = formatMessage(entry.level, entry.message, entry.context)

  switch (entry.level) {
    case 'debug':
      console.debug(formatted)
      break
    case 'info':
      console.info(formatted)
      break
    case 'warn':
      console.warn(formatted)
      break
    case 'error':
      console.error(formatted)
      break
  }
}

function createLogEntry(level: LogLevel, message: string, context?: Record<string, unknown>): LogEntry {
  return {
    level,
    message,
    timestamp: formatTimestamp(),
    context,
  }
}

export const logger = {
  debug(message: string, context?: Record<string, unknown>): void {
    if (!shouldLog('debug')) return
    const entry = createLogEntry('debug', message, context)
    logToConsole(entry)
  },

  info(message: string, context?: Record<string, unknown>): void {
    if (!shouldLog('info')) return
    const entry = createLogEntry('info', message, context)
    logToConsole(entry)
  },

  warn(message: string, context?: Record<string, unknown>): void {
    if (!shouldLog('warn')) return
    const entry = createLogEntry('warn', message, context)
    logToConsole(entry)
  },

  error(message: string, context?: Record<string, unknown>): void {
    if (!shouldLog('error')) return
    const entry = createLogEntry('error', message, context)
    logToConsole(entry)
    if (import.meta.env.PROD) {
      ElMessage.error(message)
    }
  },

  apiError(endpoint: string, error: unknown, context?: Record<string, unknown>): void {
    const errorMessage = error instanceof Error ? error.message : String(error)
    this.error(`API Error [${endpoint}]: ${errorMessage}`, {
      endpoint,
      error: errorMessage,
      ...context,
    })
  },

  userAction(action: string, context?: Record<string, unknown>): void {
    this.info(`User Action: ${action}`, context)
  },

  performance(metric: string, value: number, context?: Record<string, unknown>): void {
    this.debug(`Performance [${metric}]: ${value}ms`, context)
  },
}

export function logError(error: unknown, context?: string): void {
  const message = error instanceof Error ? error.message : String(error)
  logger.error(context ? `${context}: ${message}` : message, {
    error: error instanceof Error ? error.stack : undefined,
  })
}

export function logApiError(endpoint: string, error: unknown): void {
  logger.apiError(endpoint, error)
}
