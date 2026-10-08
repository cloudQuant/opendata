import request from '@/utils/request'

export interface DataInterfaceSummary {
  id: number
  name: string
  display_name: string
  description: string | null
  category_name: string | null
  is_active: boolean
}

interface InterfacePage {
  items: DataInterfaceSummary[]
  total: number
  page: number
  page_size: number
  total_pages?: number
}

const PAGE_SIZE = 100
const MAX_CATALOG_PAGES = 100

function validatePage(payload: unknown, requestedPage: number): InterfacePage {
  if (
    !payload ||
    typeof payload !== 'object' ||
    Array.isArray(payload) ||
    !('items' in payload) ||
    !('total' in payload) ||
    !('page' in payload) ||
    !('page_size' in payload) ||
    !Array.isArray(payload.items) ||
    typeof payload.total !== 'number' ||
    !Number.isSafeInteger(payload.total) ||
    payload.total < 0 ||
    payload.page !== requestedPage ||
    payload.page_size !== PAGE_SIZE
  ) {
    throw new Error('数据接口目录返回了无效分页信息')
  }

  const page = payload as InterfacePage
  const expectedPages = Math.ceil(page.total / PAGE_SIZE)
  if (page.total_pages !== undefined && page.total_pages !== expectedPages) {
    throw new Error('数据接口目录分页结果不完整')
  }

  const expectedItems = Math.min(PAGE_SIZE, Math.max(page.total - (requestedPage - 1) * PAGE_SIZE, 0))
  if (page.items.length !== expectedItems) {
    throw new Error('数据接口目录分页结果不完整')
  }

  if (
    page.items.some((item) =>
      !item ||
      typeof item !== 'object' ||
      !Number.isSafeInteger(item.id) ||
      item.id <= 0 ||
      item.is_active !== true ||
      typeof item.name !== 'string' ||
      typeof item.display_name !== 'string' ||
      (item.description !== null && typeof item.description !== 'string') ||
      (item.category_name !== null && typeof item.category_name !== 'string')
    )
  ) {
    throw new Error('数据接口目录包含无效或未启用的项目')
  }

  return {
    ...page,
    total_pages: page.total_pages ?? expectedPages,
  }
}

export const interfacesApi = {
  /** Load every enabled interface; never treat a partial page as an empty catalog. */
  async listEnabled(): Promise<DataInterfaceSummary[]> {
    const readPage = async (page: number) => {
      const payload = await request.get<unknown, InterfacePage | undefined>('/data/interfaces/', {
        params: { page, page_size: PAGE_SIZE, is_active: true },
      })
      return validatePage(payload, page)
    }

    const firstPage = await readPage(1)
    const totalPages = firstPage.total_pages ?? 0
    if (totalPages > MAX_CATALOG_PAGES) {
      throw new Error(
        `启用的数据接口超过当前页面目录加载上限（${MAX_CATALOG_PAGES * PAGE_SIZE} 项），请使用分页目录筛选后重试。`
      )
    }

    const items = [...firstPage.items]

    for (let page = 2; page <= totalPages; page += 1) {
      const nextPage = await readPage(page)
      if (nextPage.total !== firstPage.total || nextPage.total_pages !== totalPages) {
        throw new Error('数据接口目录在加载期间发生变化，请重试')
      }
      items.push(...nextPage.items)
    }

    if (items.length !== firstPage.total || new Set(items.map((item) => item.id)).size !== items.length) {
      throw new Error('数据接口目录分页结果不完整')
    }

    return items
  },
}
