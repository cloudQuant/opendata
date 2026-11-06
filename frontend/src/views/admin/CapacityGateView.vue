<script setup lang="ts">
/**
 * 配额门禁（capacity gate）状态面（iteration-204 / 204-D04-02 第三面）。
 *
 * 结论先说：现有 API 不把闸门状态暴露给浏览器，本页因此不发任何请求，也不
 * 猜测运行时状态。已检查的读取面：
 * - `opendata/api/stock_daily_capacity.py` 是依赖函数，不是路由：它只被
 *   `opendata/api/stock_daily_publication.py:170` 作为 `Depends` 使用；
 * - `get_stock_daily_capacity_gate()` 把"未启用""pin 不符""文件超限""已过期"
 *   统一折叠成受保护路由的同一个 404（`raise _not_found()`），按设计就让调用方
 *   无法区分关闭与配置无效——所以一次 404 探测也不能被当作状态读数；
 * - `opendata/api/settings.py` 只暴露数据库配置，没有任何容量项；
 * - 受保护的路由 `GET /api/v2/stock-daily/records` 是 `include_in_schema=False`，
 *   且不在浏览器声明的 `/api/v1` 契约面上。
 *
 * 能诚实说出的只有源码事实：三处闸门在代码里就是默认关闭的。下面每一项都带
 * 出处，运行时是否已被某个部署改写属于「不可观察」，本页不代替部署方声明。
 */
import { computed, ref } from 'vue'

const CAPACITY_NOT_OBSERVABLE_TITLE = '配额门禁状态不可从浏览器观察'
const CAPACITY_NOT_OBSERVABLE_MESSAGE =
  '没有任何 HTTP 操作返回容量闸门状态：stock_daily_capacity.py 是路由依赖而不是路由，' +
  '它把所有配置失败折叠成受保护路由的同一个 404，因此"关闭"和"pin 无效"在设计上不可区分。' +
  '本页不发请求，也不把 404 当作读数；下面列出的是能从源码确认的状态，运行时状态请由部署方证据填写。'

interface SourceGate {
  name: string
  state: string
  /** Only a source read can be stated here; runtime state stays 不可观察. */
  evidence: string
  detail: string
}

/** Reload is a local re-read of these code facts, never a server probe. */
const inspectedAt = ref(new Date().toISOString())

const sourceGates = computed<SourceGate[]>(() => [
  {
    name: 'stock-daily 容量闸门',
    state: '默认关闭（default-off）',
    evidence: 'opendata/api/stock_daily_capacity.py:171-197',
    detail:
      '要求 STOCK_DAILY_PUBLICATION_API_ENABLED 恰为字面 true（不是真值判断），并要求 ' +
      'STOCK_DAILY_PUBLICATION_CAPACITY_FILE 指向一个 64 KiB 以内、SHA-256 与 ' +
      'STOCK_DAILY_PUBLICATION_CAPACITY_SHA256 完全相符的 JSON attestation；文件每请求重读、' +
      '不缓存，且必须落在 not_before ≤ now < expires_at 的半开区间内。',
  },
  {
    name: 'market-symbol 读取预算闸门',
    state: '源码硬关闭',
    evidence: 'opendata/api/published_data.py:136,150-156',
    detail:
      '_MARKET_SYMBOL_API_ENABLED = False，注释写明在生产查询计划证据证明快照扫描预算可接受之前保持关闭；' +
      '该常量不是运行时配置项。',
  },
  {
    name: 'capture discovery 扫描闸门',
    state: '源码硬关闭',
    evidence: 'opendata/api/published_data.py:138-147',
    detail:
      '_SINA_CAPTURE_DISCOVERY_ENABLED = False，注释说明这是刻意不可被部署配置打开的硬闸门，' +
      '避免在未评审的物理扫描上放行查询。',
  },
])

/** The attestation contract, as a field list only — no values are asserted. */
const attestationFields = computed<string[]>(() => [
  'schema_version = "stock-daily-capacity-v1"',
  'dataset_id / source_id / semantic_version（固定标识）',
  'contract_sha256（64 位小写十六进制契约摘要）',
  'publication_id（可选，32 位十六进制）',
  'max_window_days（1…3660）',
  'max_page_size（1…1000）',
  'not_before / expires_at（半开有效期）',
  'evidence_ref / attested_by（运维署名与证据引用）',
])

const runtimeUnknowns = computed<string[]>(() => [
  '本部署是否已设置三个容量配置项',
  'attestation 文件是否存在、摘要是否相符',
  '当前是否落在 not_before…expires_at 有效期内',
  '受保护路由 GET /api/v2/stock-daily/records 此刻是否可用',
])
</script>

<template>
  <section class="capacity-view" aria-labelledby="capacity-title">
    <header class="page-header">
      <div>
        <h1 id="capacity-title">配额门禁</h1>
        <p>本页只陈述能从源码确认的闸门状态；浏览器无法观察服务端运行时状态。</p>
        <p class="hint">源码事实核对时间：{{ inspectedAt }}</p>
      </div>
      <el-button @click="inspectedAt = new Date().toISOString()">重新核对源码事实</el-button>
    </header>

    <el-alert
      type="warning"
      :closable="false"
      show-icon
      role="alert"
      :title="CAPACITY_NOT_OBSERVABLE_TITLE"
      :description="CAPACITY_NOT_OBSERVABLE_MESSAGE"
      data-testid="capacity-not-observable"
    />

    <el-card class="gate-card">
      <template #header>
        <h2>代码里可确认的闸门</h2>
      </template>
      <dl class="gate-list">
        <div
          v-for="gate in sourceGates"
          :key="gate.name"
          class="gate-item"
          data-testid="capacity-gate"
        >
          <dt>{{ gate.name }}</dt>
          <dd>
            <el-tag type="warning" size="small">{{ gate.state }}</el-tag>
            <p class="hint">{{ gate.detail }}</p>
            <code class="path">出处：{{ gate.evidence }}</code>
          </dd>
        </div>
      </dl>
    </el-card>

    <el-card class="gate-card">
      <template #header>
        <h2>attestation 契约字段（字段名，不含任何值）</h2>
      </template>
      <ul class="field-list">
        <li v-for="field in attestationFields" :key="field">{{ field }}</li>
      </ul>
      <p class="hint" data-testid="capacity-no-values">
        这里只列字段名：本页没有 attestation 的实际内容，也不代替部署方声明任何摘要、日期或署名。
      </p>
    </el-card>

    <el-card class="gate-card">
      <template #header>
        <h2>运行时状态：不可观察</h2>
      </template>
      <ul class="field-list" data-testid="capacity-runtime-unknowns">
        <li v-for="item in runtimeUnknowns" :key="item">
          {{ item }}：<strong>不可观察（无接口，且 404 折叠使探测无意义）</strong>
        </li>
      </ul>
      <p class="hint">
        要把这一面变成可读的，需要后端先注册一个只读的容量状态操作并保留 404 折叠语义；
        本页不通过伪造读数来假装它存在。
      </p>
    </el-card>
  </section>
</template>

<style scoped>
.capacity-view {
  display: grid;
  gap: 20px;
  max-width: 1000px;
  margin: 0 auto;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.page-header h1,
.gate-card h2 {
  margin: 0;
}

.page-header p,
.hint {
  color: var(--text-color-secondary, #606266);
  margin: 6px 0 0;
}

.gate-list {
  display: grid;
  gap: 14px;
  margin: 0;
}

.gate-item {
  display: grid;
  grid-template-columns: 190px minmax(0, 1fr);
  gap: 12px;
}

.gate-item dt {
  font-weight: 600;
}

.gate-item dd {
  min-width: 0;
  margin: 0;
}

.field-list {
  display: grid;
  gap: 8px;
  margin: 0;
  padding-left: 20px;
}

.path {
  overflow-wrap: anywhere;
}

@media (max-width: 640px) {
  .page-header,
  .gate-item {
    align-items: stretch;
    grid-template-columns: minmax(0, 1fr);
    flex-direction: column;
  }
}
</style>
