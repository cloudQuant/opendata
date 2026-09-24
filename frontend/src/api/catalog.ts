import request from '@/utils/request'

/** One catalog entry: capability + freshness (AC-18). */
export interface CatalogEntry {
  domain: string
  asset_class: string
  source: string
  verified: boolean
  display_name: string
  layer: string
  latest: string | null
  lag_days: number | null
  status: string
}

/** One data row of a domain query. */
export type DataRow = Record<string, unknown>

/** Page of a domain query. */
export interface DataPage {
  domain: string
  asset_class: string
  layer: string
  source: string
  adjust: string
  columns: string[]
  rows: DataRow[]
  page: number
  page_size: number
  count: number
}

export const catalogApi = {
  /** List the visible domains with freshness (design §10.1 / FR-20). */
  async catalog(): Promise<CatalogEntry[]> {
    const response = await request.get<{ success: boolean; data: { domains: CatalogEntry[] } }>(
      '/data/catalog',
    )
    return response.data?.data?.domains ?? []
  },

  /** Query one domain (dwd by default). */
  async query(
    assetClass: string,
    domain: string,
    params: Record<string, string | number | undefined>,
  ): Promise<DataPage> {
    const response = await request.get<{ success: boolean; data: DataPage }>(
      `/data/${assetClass}/${domain}`,
      { params },
    )
    return response.data?.data ?? { rows: [], columns: [] }
  },
}
