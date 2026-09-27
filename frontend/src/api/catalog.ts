import request from '@/utils/request'

/** Freshness verdict of one measurement (data_query.py `_freshness`). */
export type FreshnessStatus = 'fresh' | 'stale' | 'missing'

/**
 * One source leg of a domain: what that source's own ods table delivered.
 *
 * `unmapped` is a measured state, not a fallback: the source registers no
 * field mapping for the domain, so there is no column of its own to read.
 * Such a leg is never measured with another leg's column.
 */
export interface CatalogSource {
  source: string
  verified: boolean
  table: string
  status: FreshnessStatus | 'unmapped'
  reason: string | null
  latest: string | null
  lag_days: number | null
}

/** Coverage and time range of the merged table (AC-18 覆盖 / 时间范围). */
export interface CatalogCoverage {
  rows: number
  symbols: number | null
  start: string | null
  end: string | null
  diff_flagged: number | null
}

/** 质量标记: the merge's own marker column plus the cross-check detail. */
export interface CatalogQuality {
  diff_flagged: number | null
  diff_report_rows: number | null
  flag: 'clean' | 'flagged' | 'unmeasured'
}

/** One catalog entry: a domain with its readings (AC-18), not one capability leg. */
export interface CatalogEntry {
  domain: string
  asset_class: string
  display_name: string
  layer: string
  table: string
  freshness_field: string | null
  latest: string | null
  lag_days: number | null
  status: FreshnessStatus
  coverage: CatalogCoverage | null
  quality: CatalogQuality | null
  sources: CatalogSource[]
}

/** The whole `/data/catalog` payload, including the calendar date lags use. */
export interface CatalogPayload {
  domains: CatalogEntry[]
  expected_data_date: string
  domains_total: number
  source_legs_total: number
}

/**
 * What the catalog looks like when the answer carries nothing at all.
 * A factory, not a constant: callers keep the object around in component state.
 */
export function emptyCatalog(): CatalogPayload {
  return { domains: [], expected_data_date: '', domains_total: 0, source_legs_total: 0 }
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
  /** List the visible domains with their readings (design §10.1 / FR-20). */
  async catalog(): Promise<CatalogPayload> {
    const payload = await request.get<unknown, CatalogPayload | undefined>('/data/catalog')
    return payload ?? emptyCatalog()
  },

  /** Query one domain (dwd by default). */
  async query(
    assetClass: string,
    domain: string,
    params: Record<string, string | number | undefined>
  ): Promise<DataPage> {
    const payload = await request.get<unknown, DataPage | undefined>(
      `/data/${assetClass}/${domain}`,
      { params }
    )
    return payload ?? emptyDataPage()
  },
}
