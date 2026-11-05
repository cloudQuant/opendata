/**
 * Coverage readings on the plane this browser is allowed to speak on.
 *
 * `frontend/src/api/*.ts` is the contract with `opendata.main:app` under the
 * axios `baseURL` of `/api/v1` (that is what
 * `tests/test_frontend_endpoint_contract.py` compares against the mounted route
 * table), so every path here is one that router actually answers. It is also why
 * nothing here names `/api/v2/datasets/{dataset_id}/coverage`: that operation is
 * real (`opendata/api/published_data.py:1092`), but it is carried by the
 * `/api/v2` routers, which `api_router` never mounts under the browser prefix.
 * Declaring it on this plane would be a button that always 404s — the exact
 * failure PR #182 closed for the reviewed runtime — so instrument-level
 * evidence coverage is reported as UNKNOWN here instead of being inferred.
 *
 * The verdicts below are the only conclusions this module is allowed to draw,
 * and each one is derived from a token the server actually sent. Nothing
 * defaults to a complete-looking state: an unrecognised token, a missing field
 * and a failed read all land on `UNKNOWN`.
 */
import request from '@/utils/request'
import type { CatalogEntry, CatalogPayload } from '@/api/catalog'
import { catalogApi } from '@/api/catalog'

/**
 * What the console may state about one dataset/instrument's coverage.
 *
 * `PROVEN` — the reading returned an actual measured window.
 * `PARTIAL` — the reading names missing ranges inside the requested scope.
 * `SOURCE_LIMITED` — the reading itself reports a source/retention limit.
 * `NOT_SUPPORTED` — this subject has no coverage reading of this kind at all.
 * `UNKNOWN` — nothing above; the fail-closed state, and never rendered as complete.
 */
export type CoverageVerdict = 'PROVEN' | 'PARTIAL' | 'SOURCE_LIMITED' | 'NOT_SUPPORTED' | 'UNKNOWN'

/** Server-side status tokens that carry a coverage conclusion of their own. */
export type CoverageStatusToken =
  | 'complete'
  | 'partial'
  | 'source_limited'
  | 'not_applicable'
  | 'unknown'

export interface VerdictExplanation {
  verdict: CoverageVerdict
  /** Always non-empty: the server reason when there is one, else the gap wording. */
  reason: string
}

export interface ObservedWindow {
  start: string | null
  end: string | null
  rows: number | null
  symbols: number | null
}

export interface CoverageRow extends VerdictExplanation {
  key: string
  /** Domain (the unit `/data/catalog` measures) or the operator's instrument label. */
  subject: string
  markets: string[]
  table: string | null
  observed: ObservedWindow
  freshnessStatus: string | null
  /** Which operation this row's verdict came from, so a reader can check it. */
  measuredThrough: string
  /**
   * Formatted on the row so a table slot never has to hand an untyped row object
   * back into a typed formatter: an unknown window prints as an unknown string.
   */
  windowText: string
  factsText: string
}

/** Freshness of one domain (`opendata/api/data_query.py:614`). */
export interface DomainFreshness {
  domain: string
  source: string | null
  field: string | null
  latest: string | null
  lag_days: number | null
  /** Raw server token; classified, never assumed to be one of three values. */
  status: string
  expectedDataDate: string | null
  /** Null when the read failed outright: no verdict is invented from nothing. */
  error: string | null
}

/** A domain may hold no warehouse table at all; the server says which case applies. */
const SERVICE_STATE_NOT_SERVED: Record<string, string> = {
  metadata_only: '该域只有登记元数据，没有已申报的仓内服务，因此不存在可测的覆盖窗口。',
  provider_query: '该域由 provider 查询直供，合仓表未服务，因此没有仓内覆盖窗口可测。',
}

/** The verdict wording and colour one row gets; `UNKNOWN` is deliberately grey. */
export const VERDICT_PRESENTATION: Record<
  CoverageVerdict,
  { label: string; tag: 'success' | 'warning' | 'info' | 'danger' }
> = {
  PROVEN: { label: '已测得（PROVEN）', tag: 'success' },
  PARTIAL: { label: '部分覆盖（PARTIAL）', tag: 'warning' },
  SOURCE_LIMITED: { label: '受源限制（SOURCE_LIMITED）', tag: 'warning' },
  NOT_SUPPORTED: { label: '不支持（NOT_SUPPORTED）', tag: 'info' },
  UNKNOWN: { label: '未知（UNKNOWN）', tag: 'info' },
}

/** A total lookup for the template: an unexpected verdict string still renders grey. */
export function presentationOf(verdict: string): {
  label: string
  tag: 'success' | 'warning' | 'info' | 'danger'
} {
  return verdict in VERDICT_PRESENTATION
    ? VERDICT_PRESENTATION[verdict as CoverageVerdict]
    : VERDICT_PRESENTATION.UNKNOWN
}

/**
 * Domain ids are path segments here, so one is validated before any request is
 * built — the same refusal `@/reviewed/api` applies to run keys.
 */
const DOMAIN_PATTERN = /^[a-z0-9][a-z0-9_.:-]{0,79}$/

function domainSegment(domain: string): string {
  if (typeof domain !== 'string' || !DOMAIN_PATTERN.test(domain)) {
    throw new Error('coverage_domain_invalid')
  }
  return domain
}

function nullableString(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

function nullableNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/**
 * Map one server status token to a verdict. Unrecognised, empty and absent all
 * land on `UNKNOWN`; no token ever lands on `PROVEN` by default.
 */
export function classifyCoverageToken(token: string | null | undefined): CoverageVerdict {
  switch (token) {
    case 'complete':
      return 'PROVEN'
    case 'partial':
      return 'PARTIAL'
    case 'source_limited':
      return 'SOURCE_LIMITED'
    case 'not_applicable':
      return 'NOT_SUPPORTED'
    default:
      return 'UNKNOWN'
  }
}

/**
 * Verdict of one `/data/catalog` row. Only a measured window can be `PROVEN`,
 * and a `missing`/unmeasurable domain is `UNKNOWN`, never an empty "complete".
 */
export function classifyCatalogEntry(entry: CatalogEntry): VerdictExplanation {
  const serviceState = entry.service_state ?? null
  if (serviceState !== null && serviceState in SERVICE_STATE_NOT_SERVED) {
    const detail = entry.reason === null || entry.reason === undefined ? '' : `（${entry.reason}）`
    return {
      verdict: 'NOT_SUPPORTED',
      reason: `${SERVICE_STATE_NOT_SERVED[serviceState]}${detail}`,
    }
  }

  const observed = observedWindowOf(entry)
  if (observed.start === null || observed.end === null) {
    if (entry.status === 'missing') {
      return {
        verdict: 'UNKNOWN',
        reason: '仓内表当前没有可测的时间窗：服务端报告 missing，无法据此判断历史是否完整。',
      }
    }
    return {
      verdict: 'UNKNOWN',
      reason: '读取未返回覆盖时间窗（coverage 为空或边界缺失）；空窗口不等于覆盖完整。',
    }
  }
  if (observed.rows === null || observed.rows <= 0) {
    return { verdict: 'UNKNOWN', reason: '时间窗存在但行数不可测；不能把它当作已证覆盖。' }
  }
  if (entry.status === 'fresh' || entry.status === 'stale') {
    return {
      verdict: 'PROVEN',
      reason:
        entry.status === 'fresh'
          ? '在 expected_data_date 口径内测得非空窗口，且不落后。'
          : '在 expected_data_date 口径内测得非空窗口，但存在滞后。',
    }
  }
  return {
    verdict: 'UNKNOWN',
    reason: `新鲜度状态 "${entry.status}" 不在本读取面的已知判据里，按 UNKNOWN 处理。`,
  }
}

function observedWindowOf(entry: CatalogEntry): ObservedWindow {
  const coverage = entry.coverage
  if (coverage === null || coverage === undefined) {
    return { start: null, end: null, rows: null, symbols: null }
  }
  return {
    start: coverage.start,
    end: coverage.end,
    rows: coverage.rows,
    symbols: coverage.symbols,
  }
}

/** Half-open window as text; an unmeasured bound stays a stated absence. */
export function formatWindow(observed: ObservedWindow): string {
  if (observed.start === null || observed.end === null) return '—（窗口未测得）'
  return `${observed.start} → ${observed.end}`
}

/** Row/symbol counts as text; `null` from the server is not rendered as zero. */
export function formatFacts(observed: ObservedWindow): string {
  const rowCount = observed.rows === null ? '行数未测得' : `${observed.rows} 行`
  const symbols = observed.symbols === null ? '标的数未测得' : `${observed.symbols} 标的`
  return `${rowCount} · ${symbols}`
}

/** One row per catalog domain; the row's verdict is whatever the reading supports. */
export function buildCoverageRows(payload: CatalogPayload): CoverageRow[] {
  return payload.domains.map((entry) => {
    const explanation = classifyCatalogEntry(entry)
    const observed = observedWindowOf(entry)
    return {
      key: `${entry.asset_class}/${entry.domain}`,
      subject: `${entry.display_name || entry.domain}（${entry.domain}）`,
      markets: entry.markets,
      table: entry.table,
      observed,
      freshnessStatus: entry.status,
      measuredThrough: 'GET /api/v1/data/catalog',
      windowText: formatWindow(observed),
      factsText: formatFacts(observed),
      ...explanation,
    }
  })
}

/**
 * The per-instrument row is always UNKNOWN on this plane, and saying so is the
 * deliverable: `/data/catalog` measures the merged domain table and reports only
 * a symbol *count*, so no instrument-level window can be read from it.
 */
export function instrumentCoverageRow(domain: string, instrument: string): CoverageRow {
  const observed: ObservedWindow = { start: null, end: null, rows: null, symbols: null }
  return {
    key: `${domain}#${instrument}`,
    subject: `${domain} × ${instrument}`,
    markets: [],
    table: null,
    observed,
    freshnessStatus: null,
    measuredThrough: '（无接口调用）',
    windowText: formatWindow(observed),
    factsText: formatFacts(observed),
    verdict: 'UNKNOWN',
    reason:
      '标的级证据覆盖在本契约面不可得：/api/v1 只返回域级合并表的行数与标的计数，' +
      '证据绑定的标的级窗口由 /api/v2/datasets/{dataset_id}/coverage 回答，本面未声明该调用。',
  }
}

function unknownFreshness(domain: string, error: string | null): DomainFreshness {
  return {
    domain,
    source: null,
    field: null,
    latest: null,
    lag_days: null,
    status: '',
    expectedDataDate: null,
    error,
  }
}

function parseFreshness(domain: string, payload: unknown): DomainFreshness {
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    return unknownFreshness(domain, '新鲜度读取未返回对象载荷，按 UNKNOWN 处理。')
  }
  const record = payload as Record<string, unknown>
  return {
    domain: nullableString(record.domain) ?? domain,
    source: nullableString(record.source),
    field: nullableString(record.field),
    latest: nullableString(record.latest),
    lag_days: nullableNumber(record.lag_days),
    status: nullableString(record.status) ?? '',
    expectedDataDate: nullableString(record.expected_data_date),
    error: null,
  }
}

export const coverageApi = {
  /** The domain rows the coverage page reads (existing `/data/catalog` client). */
  catalog(): Promise<CatalogPayload> {
    return catalogApi.catalog()
  },

  /**
   * Freshness of one domain. A failed read resolves to an `UNKNOWN` shape with
   * `error` set rather than throwing, so the page can state the gap in place.
   */
  async domainFreshness(
    domain: string,
    params: { source?: string } = {},
    signal?: AbortSignal
  ): Promise<DomainFreshness> {
    const segment = domainSegment(domain)
    try {
      const payload = await request.get<unknown, unknown | undefined>(
        `/data/domains/${segment}/freshness`,
        { params, signal }
      )
      return parseFreshness(segment, payload)
    } catch {
      return unknownFreshness(segment, '新鲜度读取失败；未用任何其它来源替代。')
    }
  },
}
