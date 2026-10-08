import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import {
  AxiosError,
  type AxiosAdapter,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'
import { ElMessage } from 'element-plus'
import request from '@/utils/request'
import { tablesApi } from '@/api/tables'

interface SeenRequest {
  url: string
  params: unknown
  signal: InternalAxiosRequestConfig['signal']
}

const seen: SeenRequest[] = []
const originalAdapter = request.defaults.adapter

function response(config: InternalAxiosRequestConfig, data: unknown, status = 200): AxiosResponse {
  return {
    data,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: {},
    config,
    request: {},
  }
}

function httpError(config: InternalAxiosRequestConfig, status: number): AxiosError {
  return new AxiosError(
    `HTTP ${status}`,
    AxiosError.ERR_BAD_RESPONSE,
    config,
    {},
    response(config, { message: `HTTP ${status}` }, status)
  )
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function adapterFor(
  handle: (config: InternalAxiosRequestConfig) => Promise<AxiosResponse> | AxiosResponse
): AxiosAdapter {
  return async (config) => {
    seen.push({
      url: `${config.baseURL ?? ''}${config.url ?? ''}`,
      params: config.params,
      signal: config.signal,
    })
    return handle(config)
  }
}

async function startSchemaRequest(signal: AbortSignal) {
  const pending = tablesApi.getSchema(901, signal)
  await Promise.resolve()
  await Promise.resolve()
  expect(seen).toHaveLength(1)
  expect(seen[0].signal).toBe(signal)
  return { pending }
}

beforeEach(() => {
  seen.length = 0
  request.defaults.adapter = adapterFor((config) =>
    response(config, { success: true, message: 'success', data: null })
  )
  setActivePinia(createPinia())
})

afterEach(() => {
  vi.restoreAllMocks()
})

afterAll(() => {
  request.defaults.adapter = originalAdapter
})

describe('Axios request cancellation', () => {
  it('forwards AbortSignals without changing table URLs or preview pagination parameters', async () => {
    const schemaSignal = new AbortController().signal
    const previewSignal = new AbortController().signal
    request.defaults.adapter = adapterFor((config) =>
      response(config, {
        success: true,
        message: 'success',
        data: config.url?.endsWith('/schema')
          ? { table_name: 'warehouse_901', columns: [], row_count: 0, last_update_time: null }
          : { table_name: 'warehouse_901', columns: ['symbol'], rows: [], row_count: 0 },
      })
    )

    await Promise.all([
      tablesApi.getSchema(901, schemaSignal),
      tablesApi.getData(901, 2, 50, previewSignal),
    ])

    expect(seen).toEqual([
      expect.objectContaining({
        url: '/api/v1/tables/901/schema',
        params: undefined,
        signal: schemaSignal,
      }),
      expect.objectContaining({
        url: '/api/v1/tables/901/data',
        params: { page: 2, page_size: 50 },
        signal: previewSignal,
      }),
    ])
  })

  it('does not toast when a canceled request eventually returns a late success', async () => {
    const gate = deferred<AxiosResponse>()
    let adapterConfig: InternalAxiosRequestConfig | undefined
    request.defaults.adapter = adapterFor((config) => {
      adapterConfig = config
      return gate.promise
    })
    const messageError = vi.spyOn(ElMessage, 'error')
    const controller = new AbortController()
    const { pending } = await startSchemaRequest(controller.signal)

    controller.abort()
    gate.resolve(
      response(adapterConfig!, {
        success: true,
        message: 'success',
        data: { table_name: 'old_table', columns: [], row_count: 0, last_update_time: null },
      })
    )

    await expect(pending).rejects.toMatchObject({ code: AxiosError.ERR_CANCELED })
    expect(messageError).not.toHaveBeenCalled()
  })

  it('does not toast an HTTP failure that arrives after cancellation', async () => {
    const gate = deferred<AxiosResponse>()
    let adapterConfig: InternalAxiosRequestConfig | undefined
    request.defaults.adapter = adapterFor((config) => {
      adapterConfig = config
      return gate.promise
    })
    const messageError = vi.spyOn(ElMessage, 'error')
    const controller = new AbortController()
    const { pending } = await startSchemaRequest(controller.signal)

    controller.abort()
    gate.reject(httpError(adapterConfig!, 500))

    await expect(pending).rejects.toMatchObject({ code: AxiosError.ERR_CANCELED })
    expect(messageError).not.toHaveBeenCalled()
  })

  it('does not toast an application failure envelope that arrives after cancellation', async () => {
    const gate = deferred<AxiosResponse>()
    let adapterConfig: InternalAxiosRequestConfig | undefined
    request.defaults.adapter = adapterFor((config) => {
      adapterConfig = config
      return gate.promise
    })
    const messageError = vi.spyOn(ElMessage, 'error')
    const controller = new AbortController()
    const { pending } = await startSchemaRequest(controller.signal)

    controller.abort()
    gate.resolve(response(adapterConfig!, { success: false, message: 'old route failed' }))

    await expect(pending).rejects.toMatchObject({ code: AxiosError.ERR_CANCELED })
    expect(messageError).not.toHaveBeenCalled()
  })

  it('keeps user-visible errors for current uncanceled 403 and network failures', async () => {
    const messageError = vi.spyOn(ElMessage, 'error')
    request.defaults.adapter = adapterFor((config) => Promise.reject(httpError(config, 403)))

    await expect(tablesApi.getSchema(902)).rejects.toThrow('HTTP 403')
    expect(messageError).toHaveBeenCalledWith('权限不足')

    request.defaults.adapter = adapterFor((config) =>
      Promise.reject(new AxiosError('offline', AxiosError.ERR_NETWORK, config, {}))
    )
    await expect(tablesApi.getSchema(903)).rejects.toThrow('offline')
    expect(messageError).toHaveBeenCalledWith('网络错误，请检查网络连接')
  })
})
