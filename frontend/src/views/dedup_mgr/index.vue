<template>
  <section class="page" data-module="dedup_mgr">
    <header class="page-head">
      <div>
        <h2>去重管理管理</h2>
        <p class="page-desc">维护流式数据去重的键值配置、窗口管理和状态容量控制。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记去重策略</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出去重管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无去重管理数据，可先登记去重策略</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记去重策略</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条去重管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 换键迁移：旧键 -> 新组合键的核对、差额移交与只读回查 -->
    <section class="migration-panel" data-testid="migration-panel">
      <header class="panel-head">
        <div>
          <h3>去重换键迁移</h3>
          <p class="page-desc">
            核对同时带旧订单键和新组合键的事件，向分组分区移交双键分区迁移差额；
            已交付批次只读，可按旧键回查本次迁移范围。
          </p>
        </div>
        <div class="page-actions">
          <button class="btn" data-testid="migration-refresh" type="button" @click="reloadMigrations">
            刷新批次
          </button>
        </div>
      </header>
      <p v-if="migrationMessage" class="detail-note" data-testid="migration-message">
        {{ migrationMessage }}
      </p>

      <div class="stat-row">
        <article v-for="item in migrationStats" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value" :data-testid="item.testid">{{ item.value }}</strong>
        </article>
      </div>

      <table class="data-table" data-testid="migration-table">
        <thead>
          <tr>
            <th>批次编号</th>
            <th>关联策略</th>
            <th>旧键字段</th>
            <th>新组合键</th>
            <th>旧批次已确认</th>
            <th>状态</th>
            <th>事件数</th>
            <th>差额数</th>
            <th>迁移结论</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="batch in migrations"
            :key="String(batch.id)"
            :data-testid="`migration-row-${batch.id}`"
            :data-status="String(batch.状态)"
          >
            <td>{{ batch.批次编号 }}</td>
            <td>{{ batch.关联策略 }}</td>
            <td>{{ batch.旧键字段 }}</td>
            <td>{{ batch.新组合键 }}</td>
            <td>{{ batch.旧批次已确认 ? '是' : '否' }}</td>
            <td>{{ batch.状态 }}</td>
            <td>{{ batch.事件总数 ?? 0 }}</td>
            <td>{{ batch.差额数 ?? 0 }}</td>
            <td class="conclusion-cell">{{ batch.迁移结论 || '—' }}</td>
            <td class="row-actions">
              <button
                v-for="action in migrationActions"
                :key="action"
                class="link"
                type="button"
                :data-testid="`migration-action-${batch.id}-${action}`"
                @click="runMigrationAction(action, batch)"
              >
                {{ action }}
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`migration-detail-open-${batch.id}`"
                @click="openBatch(batch)"
              >
                查看台账
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`migration-legacy-${batch.id}`"
                @click="openLegacy(batch)"
              >
                按旧键回查
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`migration-handover-${batch.id}`"
                @click="openHandover(batch)"
              >
                分组分区接管
              </button>
            </td>
          </tr>
          <tr v-if="!migrations.length">
            <td colspan="10" class="empty-state">暂无迁移批次，迁移台账会随核对结果出现在这里</td>
          </tr>
        </tbody>
      </table>

      <section v-if="selectedBatch" class="migration-detail" data-testid="migration-detail">
        <header class="panel-head">
          <h4>
            {{ selectedBatch.批次编号 }} · 事件台账
            <span class="badge">{{ selectedBatch.状态 }}</span>
          </h4>
          <button class="btn ghost" type="button" data-testid="migration-detail-close" @click="selectedBatch = null">
            收起
          </button>
        </header>
        <p class="detail-note">{{ selectedBatch.迁移结论 }}</p>
        <div class="detail-chips">
          <span v-for="(value, label) in selectedBatch.摘要" :key="label" class="chip">
            {{ label }}：{{ value }}
          </span>
        </div>
        <table class="data-table compact">
          <thead>
            <tr>
              <th>事件标识</th>
              <th>键口径</th>
              <th>旧键</th>
              <th>新组合键</th>
              <th>旧分区</th>
              <th>目标分区</th>
              <th>归属状态</th>
              <th>有效归属</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(event, index) in selectedBatch.events"
              :key="`${event.事件标识}-${index}`"
              :data-testid="`migration-event-${index}`"
              :data-status="String(event.归属状态)"
            >
              <td>{{ event.事件标识 }}</td>
              <td>{{ event.键口径 }}</td>
              <td>{{ event.旧键 }}</td>
              <td>{{ event.新组合键 }}</td>
              <td>{{ event.旧分区 ?? '—' }}</td>
              <td>{{ event.目标分区 ?? '—' }}</td>
              <td>{{ event.归属状态 }}</td>
              <td>{{ event.有效归属 ? '是' : '否' }}</td>
              <td>{{ event.冲突原因 || '—' }}</td>
            </tr>
          </tbody>
        </table>

        <h4>双键分区迁移差额（{{ selectedBatch.迁移差额?.length ?? 0 }}）</h4>
        <table class="data-table compact">
          <thead>
            <tr>
              <th>事件标识</th>
              <th>来源编号</th>
              <th>旧键</th>
              <th>新组合键</th>
              <th>旧分区</th>
              <th>目标分区</th>
              <th>归属状态</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(diff, index) in selectedBatch.迁移差额"
              :key="`${diff.事件标识}-${index}`"
              :data-testid="`migration-diff-${index}`"
            >
              <td>{{ diff.事件标识 }}</td>
              <td>{{ diff.来源编号 }}</td>
              <td>{{ diff.旧键 }}</td>
              <td>{{ diff.新组合键 }}</td>
              <td>{{ diff.旧分区 }}</td>
              <td>{{ diff.目标分区 }}</td>
              <td>{{ diff.归属状态 }}</td>
            </tr>
            <tr v-if="!selectedBatch.迁移差额?.length">
              <td colspan="7" class="empty-state">没有需要迁移的分区差额</td>
            </tr>
          </tbody>
        </table>

        <template v-if="legacyView">
          <h4>按旧键回查 {{ legacyView.本次回查旧键 || '' }}（{{ legacyView.只读 ? '只读' : '可写' }}）</h4>
          <p class="detail-note" data-testid="legacy-scope">{{ legacyView.范围说明 }}</p>
          <table class="data-table compact" data-testid="legacy-reconciliation">
            <thead>
              <tr>
                <th>旧键</th>
                <th>旧分区</th>
                <th>事件数</th>
                <th>有效归属数</th>
                <th>旧键压缩</th>
                <th>新键归属分区</th>
                <th>拒绝</th>
                <th>重复</th>
                <th>冲突</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="group in legacyView.两代键对账" :key="group.旧键" data-testid="legacy-group">
                <td>{{ group.旧键 }}</td>
                <td>{{ group.旧分区 }}</td>
                <td>{{ group.事件数 }}</td>
                <td>{{ group.有效归属数 }}</td>
                <td>{{ group.旧键压缩 ? '是' : '否' }}</td>
                <td>{{ (group.新键归属分区 || []).join('、') || '—' }}</td>
                <td>{{ (group.拒绝 || []).join('、') || '—' }}</td>
                <td>{{ (group.重复 || []).join('、') || '—' }}</td>
                <td>{{ (group.冲突 || []).join('、') || '—' }}</td>
              </tr>
              <tr v-if="!legacyView.两代键对账?.length">
                <td colspan="9" class="empty-state">该旧键下没有事件台账</td>
              </tr>
            </tbody>
          </table>
        </template>

        <template v-if="handoverResult">
          <h4>分组分区接管结论</h4>
          <p class="detail-note" data-testid="handover-summary">
            差额 {{ handoverResult.差额数 }} 笔 · 接管 {{ handoverResult.接管数 }} 笔 ·
            退回源端 {{ handoverResult.退回源端数 }} 笔 · 归属冲突 {{ handoverResult.归属冲突数 }} 笔 ·
            待确认 {{ handoverResult.待确认数 }} 笔
          </p>
          <table class="data-table compact" data-testid="handover-table">
            <thead>
              <tr>
                <th>事件标识</th>
                <th>旧分区</th>
                <th>目标分区</th>
                <th>来源编号</th>
                <th>接管结论</th>
                <th>目标归属</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(item, index) in handoverResult.切换清单"
                :key="`${item.事件标识}-${index}`"
                :data-testid="`handover-row-${index}`"
                :data-decision="item.接管结论"
              >
                <td>{{ item.事件标识 }}</td>
                <td>{{ item.旧分区 }}</td>
                <td>{{ item.目标分区 }}</td>
                <td>{{ item.来源编号 }}</td>
                <td>{{ item.接管结论 }}</td>
                <td>{{ item.目标归属 ? '是' : '否' }}</td>
                <td>{{ item.结论说明 }}</td>
              </tr>
            </tbody>
          </table>
        </template>
      </section>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type MigrationBatch = Record<string, any> & {
  摘要?: Record<string, number>
  events?: Record<string, any>[]
  迁移差额?: Record<string, any>[]
}
type LegacyView = Record<string, any> & { 两代键对账?: Record<string, any>[] }
type HandoverResult = Record<string, any> & { 切换清单?: Record<string, any>[] }

const ENDPOINT = '/api/dedup_mgr'
const MIGRATION_GROUP_ID = 2
const columns = ["策略编号", "关联流", "去重键", "去重窗口", "保留策略", "状态上限", "命中次数", "误杀率"]
const actions = ["启用去重", "停用去重", "清理状态"]
const statuses = ["草稿", "运行中", "容量告警", "已停用"]
const metricNames = ["策略总数", "运行中策略", "容量告警策略"]
const migrationActions = ["开始核对", "确认旧批次", "移交差额", "中断迁移", "继续迁移", "交付批次"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const migrations = ref<MigrationBatch[]>([])
const migrationTotal = ref(0)
const selectedBatch = ref<MigrationBatch | null>(null)
const legacyView = ref<LegacyView | null>(null)
const handoverResult = ref<HandoverResult | null>(null)
const migrationMessage = ref('')
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])
const migrationStats = computed(() => {
  const count = (status: string) => migrations.value.filter((batch) => batch.状态 === status).length
  return [
    { label: '迁移批次', value: migrationTotal.value, testid: 'migration-stat-total' },
    { label: '归属冲突', value: count('归属冲突'), testid: 'migration-stat-conflict' },
    { label: '待移交', value: count('待移交'), testid: 'migration-stat-ready' },
    { label: '已交付', value: count('已交付'), testid: 'migration-stat-delivered' },
  ]
})

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'dedup_mgr.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('去重管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '去重管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('去重策略列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '去重管理列表读取失败'
  }
}

async function reloadMigrations() {
  migrationMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/migrations`)
    if (!response.ok) throw new Error('迁移批次读取失败')
    const payload = await response.json()
    migrations.value = payload.items ?? []
    migrationTotal.value = payload.total ?? migrations.value.length
    if (selectedBatch.value) {
      const current = migrations.value.find((item) => item.id === selectedBatch.value?.id)
      if (current) await loadDetail(current)
    }
  } catch (error) {
    migrationMessage.value = error instanceof Error ? error.message : '迁移批次读取失败'
  }
}

async function loadDetail(batch: MigrationBatch) {
  const response = await request(`${ENDPOINT}/migrations/${batch.id}`)
  if (!response.ok) throw new Error('迁移批次明细读取失败')
  selectedBatch.value = await response.json()
}

async function openBatch(batch: MigrationBatch) {
  legacyView.value = null
  handoverResult.value = null
  try {
    await loadDetail(batch)
  } catch (error) {
    migrationMessage.value = error instanceof Error ? error.message : '迁移批次明细读取失败'
  }
}

async function runMigrationAction(action: string, batch: MigrationBatch) {
  migrationMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/migrations/${batch.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '迁移动作未生效')
    migrationMessage.value = payload.message
    await reloadMigrations()
    if (payload.entry && payload.entry.events) {
      legacyView.value = null
      handoverResult.value = null
      selectedBatch.value = payload.entry
    } else {
      await loadDetail(batch)
    }
  } catch (error) {
    migrationMessage.value = error instanceof Error ? error.message : '迁移动作未生效'
  }
}

async function openLegacy(batch: MigrationBatch) {
  migrationMessage.value = ''
  try {
    await loadDetail(batch)
    const response = await request(`${ENDPOINT}/migrations/${batch.id}/legacy`)
    if (!response.ok) throw new Error('只读回查失败')
    legacyView.value = await response.json()
    handoverResult.value = null
  } catch (error) {
    migrationMessage.value = error instanceof Error ? error.message : '只读回查失败'
  }
}

async function openHandover(batch: MigrationBatch) {
  handoverResult.value = null
  legacyView.value = null
  migrationMessage.value = ''
  try {
    const response = await request(`/api/grp_partition/${MIGRATION_GROUP_ID}/migrations/consume`, {
      method: 'POST',
      body: JSON.stringify({ batch_code: batch.批次编号 }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '分组分区接管失败')
    handoverResult.value = payload.entry
    migrationMessage.value = payload.message
    await loadDetail(batch)
    await reloadMigrations()
  } catch (error) {
    migrationMessage.value = error instanceof Error ? error.message : '分组分区接管失败'
  }
}

onMounted(() => {
  void reload()
  void reloadMigrations()
})
</script>
