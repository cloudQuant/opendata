/**
 * Performance monitoring composable.
 *
 * Provides utilities for tracking component render times, API call durations,
 * and other performance metrics.
 */

import { onMounted, onUnmounted, type Ref } from 'vue'
import { logger } from '@/utils/logger'

export interface PerformanceMetric {
  name: string
  startTime: number
  endTime?: number
  duration?: number
}

export interface UsePerformanceOptions {
  /** Component name for logging */
  componentName?: string
  /** Whether to log mount/unmount times */
  trackLifecycle?: boolean
  /** Whether to log to console in development */
  logToConsole?: boolean
}

export interface UsePerformanceReturn {
  /** Start timing a metric */
  startMetric: (name: string) => void
  /** End timing a metric and return duration */
  endMetric: (name: string) => number | null
  /** Time an async function and log the result */
  timeAsync: <T>(name: string, fn: () => Promise<T>) => Promise<T>
  /** Get all recorded metrics */
  getMetrics: () => Map<string, PerformanceMetric>
}

const metrics = new Map<string, PerformanceMetric>()

/**
 * Performance monitoring utilities for Vue components.
 *
 * @example
 * ```ts
 * const perf = usePerformance({ componentName: 'ScriptsView' })
 *
 * // Time async operations
 * await perf.timeAsync('loadScripts', async () => {
 *   scripts.value = await api.list()
 * })
 *
 * // Manual timing
 * perf.startMetric('filter')
 * // ... do filtering
 * const duration = perf.endMetric('filter')
 * ```
 */
export function usePerformance(options: UsePerformanceOptions = {}): UsePerformanceReturn {
  const {
    componentName = 'Unknown',
    trackLifecycle = false,
    logToConsole = import.meta.env.DEV
  } = options

  function startMetric(name: string): void {
    const fullName = `${componentName}:${name}`
    metrics.set(fullName, {
      name: fullName,
      startTime: performance.now()
    })
  }

  function endMetric(name: string): number | null {
    const fullName = `${componentName}:${name}`
    const metric = metrics.get(fullName)
    if (!metric) {
      logger.warn(`Metric "${fullName}" not found. Did you call startMetric?`)
      return null
    }

    metric.endTime = performance.now()
    metric.duration = metric.endTime - metric.startTime

    if (logToConsole) {
      logger.performance(name, metric.duration, { component: componentName })
    }

    return metric.duration
  }

  async function timeAsync<T>(name: string, fn: () => Promise<T>): Promise<T> {
    startMetric(name)
    try {
      const result = await fn()
      const duration = endMetric(name)
      if (duration !== null && duration > 1000) {
        logger.warn(`Slow operation: ${name} took ${duration.toFixed(0)}ms`, {
          component: componentName
        })
      }
      return result
    } catch (error) {
      endMetric(name)
      throw error
    }
  }

  function getMetrics(): Map<string, PerformanceMetric> {
    return new Map(metrics)
  }

  if (trackLifecycle) {
    onMounted(() => {
      logger.debug(`${componentName} mounted`, { category: 'lifecycle' })
    })

    onUnmounted(() => {
      logger.debug(`${componentName} unmounted`, { category: 'lifecycle' })
    })
  }

  return {
    startMetric,
    endMetric,
    timeAsync,
    getMetrics
  }
}

/**
 * Track component render time decorator.
 *
 * @example
 * ```ts
 * <script setup>
 * useTrackRender('MyComponent')
 * </script>
 * ```
 */
export function useTrackRender(componentName: string): void {
  const perf = usePerformance({ componentName })

  onMounted(() => {
    perf.startMetric('render')
    void perf.endMetric('render')
  })
}
