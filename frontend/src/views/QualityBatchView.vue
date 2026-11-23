<template>
  <section class="page" data-testid="quality-batch">
    <header class="page-head">
      <div>
        <h2>批量质量校验</h2>
        <p class="page-desc">
          一次提交一个源表分区，从血缘找出读取该源表的下游表逐条核查实际行，按表汇总质量结论并回填血缘。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-all" type="button" @click="refreshAll">刷新</button>
      </div>
    </header>

    <p v-if="message" class="notice" data-testid="quality-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="quality-error">{{ errorMessage }}</p>

    <section class="panel" data-testid="submit-panel">
      <h3>提交源表分区</h3>
      <div class="inline-form">
        <label>
          <span>源表</span>
          <select v-model="selectedSource" data-testid="source-select">
            <option v-for="table in sourceTables" :key="table" :value="table">{{ table }}</option>
          </select>
        </label>
        <label>
          <span>待检分区</span>
          <select v-model="selectedPartition" data-testid="partition-select">
            <option v-for="option in partitionOptions" :key="option.partition" :value="option.partition">
              {{ option.partition }}（源表 {{ option.source_row_count }} 行）
            </option>
          </select>
        </label>
        <button class="btn primary" data-testid="submit-partition" type="button" @click="submit">
          提交本分区检查
        </button>
      </div>
      <p class="hint">待检分区来自源表实际分区；只有读取该源表且有启用检查项的下游表会进入本批。</p>
    </section>

    <section class="panel" data-testid="claimable-panel">
      <h3>待领取批次</h3>
      <div class="inline-form">
        <button class="btn" data-testid="claim-next" type="button" @click="claimNextBatch">
          领取下一组检查
        </button>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次</th><th>轮次</th><th>源表分区</th><th>来源</th><th>下游表</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="batch in claimable"
            :key="batch.batch_no"
            :data-testid="`claimable-${batch.batch_no}`"
          >
            <td>{{ batch.batch_no }}</td>
            <td>{{ batch.round_no }}</td>
            <td>{{ batch.source_table }} / {{ batch.partition }}</td>
            <td>{{ batch.trigger }}</td>
            <td>{{ batch.items.map((item) => item.table).join('、') }}</td>
            <td>{{ batch.status }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`claim-${batch.batch_no}`" @click="claimOne(batch.batch_no)">
                领取
              </button>
            </td>
          </tr>
          <tr v-if="!claimable.length">
            <td colspan="7" class="empty-state" data-testid="claimable-empty">当前没有待领取的复查批次</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel" data-testid="lineage-panel">
      <h3>血缘影响分析领取</h3>
      <div class="inline-form">
        <label>
          <span>血缘条目</span>
          <select v-model.number="selectedLineage" data-testid="lineage-select">
            <option v-for="entry in lineageEntries" :key="entry.id" :value="entry.id">
              #{{ entry.id }} {{ entry['目标表'] }} ← {{ entry['来源表列表'] }}
            </option>
          </select>
        </label>
        <button class="btn" data-testid="load-impact" type="button" @click="loadImpactGroups">查看未完成检查</button>
      </div>
      <div v-if="impact">
        <p class="hint" data-testid="impact-summary">
          下游表：{{ impact.downstream_tables.join('、') || '无' }}；{{ impact.message }}
        </p>
        <table class="data-table">
          <thead>
            <tr><th>批次</th><th>轮次</th><th>下游表</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="group in impact.review_groups"
              :key="group.batch_no"
              :data-testid="`impact-group-${group.batch_no}`"
            >
              <td>{{ group.batch_no }}</td>
              <td>{{ group.round_no }}</td>
              <td>{{ group.downstream_tables.join('、') }}</td>
              <td>{{ group.status }}</td>
              <td class="row-actions">
                <button class="link" type="button" :data-testid="`claim-impact-${group.batch_no}`" @click="claimFromImpact(group.batch_no)">
                  领取整组
                </button>
              </td>
            </tr>
            <tr v-if="!impact.review_groups.length">
              <td colspan="5" class="empty-state" data-testid="impact-empty">没有尚未完成的下游检查组</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel" data-testid="claimed-panel" v-if="claimedBatch">
      <h3>当前批次 #{{ claimedBatch.batch_no }}（{{ claimedBatch.status }}）</h3>
      <p class="hint">
        源表分区 {{ claimedBatch.source_table }} / {{ claimedBatch.partition }}；实际领到
        {{ claimedBatch.item_count }} 张下游表的检查。
      </p>
      <table class="data-table">
        <thead>
          <tr><th>下游表</th><th>实际检查分区</th><th>规则</th><th>状态</th><th>表结论</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr
            v-for="item in claimedBatch.items"
            :key="item.item_id"
            :data-testid="`item-${claimedBatch.batch_no}-${item.table}`"
          >
            <td>{{ item.table }}</td>
            <td>{{ item.partition }}</td>
            <td>{{ item.rule_ids.join('、') }}</td>
            <td>{{ item.status }}</td>
            <td>{{ item.conclusion || '尚未执行' }}</td>
            <td class="row-actions">
              <button
                v-if="claimedBatch.status === '已领取'"
                class="link"
                type="button"
                data-testid="execute-batch"
                @click="execute(claimedBatch.batch_no)"
              >
                执行本批检查
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="claimedBatch.status === '已完成'" class="result-list" data-testid="batch-results">
        <article v-for="item in claimedBatch.items" :key="item.item_id" class="result-card">
          <h4>{{ item.table }} / {{ item.partition }}</h4>
          <p>{{ item.conclusion }}</p>
          <table class="data-table">
            <thead>
              <tr><th>规则</th><th>字段</th><th>检查行数</th><th>失败行</th><th>得分/阈值</th><th>结论</th></tr>
            </thead>
            <tbody>
              <tr v-for="result in item.results" :key="result.rule_id">
                <td>{{ result.rule_name || result.rule_id }}</td>
                <td>{{ result.field || '—' }}</td>
                <td>{{ result.rows_checked ?? '—' }}</td>
                <td>{{ result.failed_rows?.length ?? 0 }}</td>
                <td>{{ result.score }} / {{ result.threshold }}</td>
                <td>{{ result.skipped ? '已停用，未读取数据' : result.conclusion }}</td>
              </tr>
            </tbody>
          </table>
        </article>
      </div>
    </section>

    <section class="panel" data-testid="rounds-panel">
      <h3>校验轮次与回查</h3>
      <table class="data-table">
        <thead>
          <tr><th>轮次</th><th>源表分区</th><th>状态</th><th>批次</th><th>待领取</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="round in rounds" :key="round.round_no" :data-testid="`round-${round.round_no}`">
            <td>{{ round.round_no }}</td>
            <td>{{ round.source_table }} / {{ round.partition }}</td>
            <td>{{ round.status }}</td>
            <td>{{ round.batch_count }}</td>
            <td>{{ round.claimable_count }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`open-round-${round.round_no}`" @click="openRound(round.round_no)">
                查看结论
              </button>
              <button
                v-if="round.status === '进行中'"
                class="link"
                type="button"
                :data-testid="`close-round-${round.round_no}`"
                @click="closeRoundRow(round.round_no)"
              >
                结案
              </button>
            </td>
          </tr>
          <tr v-if="!rounds.length">
            <td colspan="6" class="empty-state">还没有提交过源表分区</td>
          </tr>
        </tbody>
      </table>

      <div v-if="activeRound" class="result-list" data-testid="round-detail">
        <h4>轮次 {{ activeRound.round_no }}：{{ activeRound.source_table }} / {{ activeRound.partition }}（{{ activeRound.status }}）</h4>
        <table class="data-table">
          <thead>
            <tr><th>下游表</th><th>分区</th><th>检查项</th><th>失败项</th><th>批次</th><th>逐表结论</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in activeRound.table_results_summary" :key="row.table" :data-testid="`summary-${row.table}`">
              <td>{{ row.table }}</td>
              <td>{{ row.partition }}</td>
              <td>{{ row.checks }}</td>
              <td>{{ row.failed_checks }}</td>
              <td>{{ row.batch_nos.join('、') }}</td>
              <td :data-passed="row.passed">{{ row.passed ? '质量通过' : '质量未通过' }}</td>
            </tr>
          </tbody>
        </table>
        <p class="hint" data-testid="duplicate-summary">
          重复校验：{{ activeRound.duplicate_checks?.length ? activeRound.duplicate_checks : '无' }}
        </p>
        <table class="data-table">
          <thead>
            <tr><th>下游表</th><th>规则</th><th>批次</th><th>检查行号</th><th>失败行号</th><th>结论</th></tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in activeRound.checked_rows" :key="`${row.table}-${row.rule_id}-${index}`">
              <td>{{ row.table }}</td>
              <td>{{ row.rule_id }}</td>
              <td>{{ row.batch_no }}</td>
              <td>{{ row.row_ids.join('、') }}</td>
              <td>{{ row.failed_row_ids.join('、') || '无' }}</td>
              <td>{{ (activeRound.results?.find((item) => item.rule_id === row.rule_id && item.batch_no === row.batch_no) || {}).conclusion }}</td>
            </tr>
          </tbody>
        </table>
        <table class="data-table">
          <thead>
            <tr><th>血缘回填</th><th>分区</th><th>轮次/批次</th><th>质量状态</th><th>结论</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in activeRound.lineage_writebacks" :key="`${row.lineage_id}-${row.batch_no}`">
              <td>#{{ row.lineage_id }} {{ row.target_table }}</td>
              <td>{{ row.partition }}</td>
              <td>{{ row.round_no }} / {{ row.batch_no }}</td>
              <td>{{ row.quality_status }}</td>
              <td>{{ row.conclusion }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { fetchJson } from '@/api/client'
import {
  claimBatch,
  claimFromLineage,
  claimNext,
  closeRound,
  executeBatch,
  loadClaimable,
  loadImpact,
  loadPartitions,
  loadRound,
  loadRounds,
  submitPartition,
} from '@/api/quality'
import type { Batch, ImpactResult, PartitionOption, Round } from '@/api/quality'

type LineageEntry = { id: number; 目标表: string; 来源表列表: string; status: string }

const partitions = ref<PartitionOption[]>([])
const selectedSource = ref('')
const selectedPartition = ref('')
const claimable = ref<Batch[]>([])
const rounds = ref<Round[]>([])
const activeRound = ref<Round | null>(null)
const claimedBatch = ref<Batch | null>(null)
const lineageEntries = ref<LineageEntry[]>([])
const selectedLineage = ref<number | null>(null)
const impact = ref<ImpactResult | null>(null)
const message = ref('')
const errorMessage = ref('')

const sourceTables = computed(() =>
  Array.from(new Set(partitions.value.map((item) => item.source_table))),
)
const partitionOptions = computed(() =>
  partitions.value.filter((item) => item.source_table === selectedSource.value),
)

watch(selectedSource, () => {
  selectedPartition.value = partitionOptions.value[0]?.partition ?? ''
})

function setMessage(text: string) {
  message.value = text
  errorMessage.value = ''
}

function setError(error: unknown, fallback: string) {
  errorMessage.value = error instanceof Error ? error.message : fallback
  message.value = ''
}

async function refreshAll() {
  try {
    partitions.value = await loadPartitions()
    if (!selectedSource.value && sourceTables.value.length) {
      selectedSource.value = sourceTables.value[0]
      selectedPartition.value = partitionOptions.value[0]?.partition ?? ''
    }
    claimable.value = await loadClaimable()
    rounds.value = await loadRounds()
    const lineage = await fetchJson<{ items: LineageEntry[] }>('/api/lineage_graph?size=200')
    lineageEntries.value = lineage.items.filter((entry) => entry.status === '已建立')
    if (!selectedLineage.value && lineageEntries.value.length) {
      selectedLineage.value = lineageEntries.value[0].id
    }
  } catch (error) {
    setError(error, '加载校验数据失败')
  }
}

async function submit() {
  if (!selectedSource.value || !selectedPartition.value) return
  try {
    const result = await submitPartition(selectedSource.value, selectedPartition.value)
    if (!result.ok) throw new Error(result.message)
    setMessage(result.message)
    if (result.batch) claimedBatch.value = null
    await refreshAll()
    if (result.round) activeRound.value = await loadRound(result.round.round_no)
  } catch (error) {
    setError(error, '提交分区失败')
  }
}

async function claimNextBatch() {
  try {
    const result = await claimNext()
    if (!result.ok) throw new Error(result.message)
    setMessage(result.message)
    if (result.batch) claimedBatch.value = result.batch
    await refreshAll()
  } catch (error) {
    setError(error, '领取批次失败')
  }
}

async function claimOne(batchNo: number) {
  try {
    const result = await claimBatch(batchNo)
    if (!result.ok) throw new Error(result.message)
    setMessage(result.message)
    claimedBatch.value = result.batch
    await refreshAll()
  } catch (error) {
    setError(error, '领取批次失败')
  }
}

async function execute(batchNo: number) {
  try {
    const result = await executeBatch(batchNo)
    if (!result.ok) throw new Error(result.message)
    setMessage(result.message)
    claimedBatch.value = result.batch
    await refreshAll()
    if (claimedBatch.value) activeRound.value = await loadRound(claimedBatch.value.round_no)
  } catch (error) {
    setError(error, '执行检查失败')
  }
}

async function loadImpactGroups() {
  if (!selectedLineage.value) return
  try {
    impact.value = await loadImpact(selectedLineage.value)
    setMessage(impact.value.message)
  } catch (error) {
    setError(error, '血缘影响分析读取失败')
  }
}

async function claimFromImpact(batchNo: number) {
  if (!selectedLineage.value) return
  try {
    const result = await claimFromLineage(selectedLineage.value, batchNo)
    if (!result.ok) throw new Error(result.message)
    setMessage(result.message)
    if (result.batch) claimedBatch.value = result.batch
    await refreshAll()
    impact.value = await loadImpact(selectedLineage.value)
  } catch (error) {
    setError(error, '从血缘领取检查失败')
  }
}

async function openRound(roundNo: number) {
  try {
    activeRound.value = await loadRound(roundNo)
  } catch (error) {
    setError(error, '轮次读取失败')
  }
}

async function closeRoundRow(roundNo: number) {
  try {
    const result = await closeRound(roundNo)
    if (!result.ok) throw new Error(result.message)
    setMessage(result.message)
    await refreshAll()
    activeRound.value = await loadRound(roundNo)
  } catch (error) {
    setError(error, '结案失败')
  }
}

onMounted(refreshAll)
</script>

<style scoped>
.notice { background: #ecfdf3; border: 1px solid #abefc6; color: #067647; padding: 8px 12px; border-radius: 6px; font-size: 13px; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-bottom: 14px; }
.panel h3 { margin: 0 0 10px; font-size: 15px; }
.inline-form { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; margin-bottom: 10px; }
.inline-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.inline-form select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; min-width: 200px; }
.hint { color: var(--muted); font-size: 12px; margin: 0 0 10px; }
.result-list { margin-top: 12px; }
.result-card { border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.result-card h4 { margin: 0 0 6px; font-size: 14px; }
.result-card p { font-size: 13px; margin: 0 0 8px; }
[data-passed='false'] { color: #b42318; font-weight: 600; }
</style>
