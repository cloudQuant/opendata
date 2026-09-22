/**
 * WebSocket client with automatic reconnection and exponential backoff.
 *
 * Usage:
 *   import { useExecutionWs } from '@/utils/websocket'
 *   const ws = useExecutionWs((data) => { console.log(data) })
 *   // ws.close() when done
 */

export interface WsOptions {
  /** Max reconnect attempts before giving up (0 = infinite) */
  maxRetries?: number
  /** Base delay in ms for exponential backoff */
  baseDelay?: number
  /** Maximum delay in ms */
  maxDelay?: number
  /** Ping interval in ms to keep connection alive */
  pingInterval?: number
}

const DEFAULT_OPTIONS: Required<WsOptions> = {
  maxRetries: 0,
  baseDelay: 1000,
  maxDelay: 30000,
  pingInterval: 25000,
}

export class ReconnectingWebSocket {
  private ws: WebSocket | null = null
  private retryCount = 0
  private closed = false
  private pingTimer: ReturnType<typeof setInterval> | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private opts: Required<WsOptions>

  constructor(
    private url: string,
    private onMessage: (data: unknown) => void,
    options?: WsOptions,
  ) {
    this.opts = { ...DEFAULT_OPTIONS, ...options }
    this.connect()
  }

  private connect() {
    if (this.closed) return

    try {
      this.ws = new WebSocket(this.url)
    } catch {
      this.scheduleReconnect()
      return
    }

    this.ws.onopen = () => {
      this.retryCount = 0
      this.startPing()
    }

    this.ws.onmessage = (event: MessageEvent) => {
      try {
        const parsed = JSON.parse(event.data)
        if (parsed.type === 'pong') return
        this.onMessage(parsed)
      } catch {
        // ignore non-JSON messages
      }
    }

    this.ws.onclose = () => {
      this.stopPing()
      if (!this.closed) {
        this.scheduleReconnect()
      }
    }

    this.ws.onerror = () => {
      // onclose will fire after onerror, reconnect handled there
    }
  }

  private scheduleReconnect() {
    if (this.closed) return
    if (this.opts.maxRetries > 0 && this.retryCount >= this.opts.maxRetries) return

    const delay = Math.min(
      this.opts.baseDelay * Math.pow(2, this.retryCount),
      this.opts.maxDelay,
    )
    this.retryCount++

    this.reconnectTimer = setTimeout(() => {
      this.connect()
    }, delay)
  }

  private startPing() {
    this.stopPing()
    this.pingTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send('ping')
      }
    }, this.opts.pingInterval)
  }

  private stopPing() {
    if (this.pingTimer) {
      clearInterval(this.pingTimer)
      this.pingTimer = null
    }
  }

  /** Permanently close the connection (no reconnect). */
  close() {
    this.closed = true
    this.stopPing()
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN
  }
}

/**
 * Create a WebSocket connection to the execution updates endpoint.
 *
 * Automatically determines ws:// or wss:// based on page protocol.
 */
export function useExecutionWs(
  onMessage: (data: { type: string; data: Record<string, unknown> }) => void,
  options?: WsOptions,
): ReconnectingWebSocket {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  const url = `${protocol}//${host}/ws/executions`
  return new ReconnectingWebSocket(
    url,
    onMessage as (data: unknown) => void,
    options,
  )
}
