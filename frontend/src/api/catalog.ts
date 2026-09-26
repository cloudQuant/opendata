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

/**
 * What a domain query looks like when the answer carries no page at all.
 * A factory, not a constant: callers keep the object around in component state.
 */
export function emptyDataPage(): DataPage {
  return {
    domain: '',
    asset_class: '',
    layer: '',
    source: '',
    adjust: '',
    columns: [],
    rows: [],
    page: 1,
    page_size: 0,
    count: 0,
  }
}

export const catalogApi = {
  /** List the visible domains with freshness (design §10.1 / FR-20). */
  async catalog(): Promise<CatalogEntry[]> {
    const payload = await request.get<unknown, { domains?: CatalogEntry[] } | undefined>(
      '/data/catalog',
    )
    return payload?.domains ?? []
  },

  /** Query one domain (dwd by default). */
  async query(
    assetClass: string,
    domain: string,
    params: Record<string, string | number | undefined>,
  ): Promise<DataPage> {
    const payload = await request.get<unknown, DataPage | undefined>(
      `/data/${assetClass}/${domain}`,
      { params },
    )
    return payload ?? emptyDataPage()
  },
}
