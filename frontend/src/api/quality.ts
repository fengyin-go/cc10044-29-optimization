/** 批量质量校验接口：分区提交、批次领取/执行、轮次回查和血缘影响分析。 */
import { fetchJson, request } from './client'

export type PartitionOption = {
  source_table: string
  partition: string
  source_row_count: number
}

export type FailedRow = {
  row_id: number
  field: string
  value: string | number | null
  reason: string
}

export type RuleResult = {
  rule_id: number
  rule_name: string | null
  field?: string | null
  passed: boolean | null
  score: number | null
  threshold: number | null
  rows_checked: number | null
  failed_rows: FailedRow[]
  reason?: string | null
  conclusion?: string | null
  skipped?: boolean
  checked_rows?: Record<string, unknown>[]
}

export type BatchItem = {
  item_id: number
  table: string
  partition: string
  lineage_id: number | null
  rule_ids: number[]
  status: string
  executed_at: string | null
  passed: boolean | null
  conclusion: string | null
  results?: RuleResult[]
}

export type Batch = {
  batch_no: number
  round_no: number
  source_table: string
  partition: string
  trigger: string
  status: string
  created_at: string
  claimed_by: string | null
  claimed_at: string | null
  executed_at: string | null
  item_count: number
  items: BatchItem[]
  downstream_partitions: Record<string, { partition: string; has_partition_data: boolean }>
  lineage_writebacks: {
    lineage_id: number
    table: string
    partition: string
    round_no: number
    batch_no: number
    质量状态: string
    质量结论: string
  }[]
  // 影响分析列表里额外带出
  claimable?: boolean
  downstream_tables?: string[]
}

export type TableSummary = {
  table: string
  partition: string
  round_no: number
  checks: number
  failed_checks: number
  batch_nos: number[]
  last_batch_no: number
  passed: boolean
}

export type Round = {
  round_no: number
  source_table: string
  partition: string
  status: string
  created_at: string
  closed_at: string | null
  batches: Batch[]
  batch_count: number
  claimable_count: number
  table_results_summary: TableSummary[]
  results?: {
    table: string
    partition: string
    round_no: number
    batch_no: number
    rule_id: number
    rule_name: string
    dimension: string
    field: string
    passed: boolean
    score: number
    threshold: number
    rows_checked: number
    failed_rows: FailedRow[]
    conclusion: string
    checked_at: string
  }[]
  lineage_writebacks?: {
    lineage_id: number
    target_table: string
    sources: string
    quality_status: string
    partition: string
    round_no: number
    batch_no: number
    conclusion: string
  }[]
  duplicate_checks?: { table: string; partition: string; rule_id: number; batch_nos: number[] }[]
  checked_rows?: {
    table: string
    partition: string
    rule_id: number
    row_ids: number[]
    failed_row_ids: number[]
    batch_no: number
  }[]
}

export type SubmitResult = {
  ok: boolean
  message: string
  round: Round | null
  batch: Batch | null
  created_round?: boolean
  already_checked?: unknown[]
}

export type ClaimResult = {
  ok: boolean
  message: string
  batch: Batch | null
}

export type ExecuteResult = {
  ok: boolean
  message: string
  batch: Batch | null
  table_results?: Record<string, unknown>
}

export type ImpactGroup = Batch & { claimable: boolean; downstream_tables: string[] }

export type ImpactResult = {
  ok: boolean
  entry_id: number
  table: string
  downstream_tables: string[]
  review_groups: ImpactGroup[]
  unfinished_count: number
  message: string
}

export async function loadPartitions(): Promise<PartitionOption[]> {
  const payload = await fetchJson<{ items: PartitionOption[] }>('/api/quality/source-partitions')
  return payload.items
}

export async function submitPartition(
  sourceTable: string,
  partition: string,
): Promise<SubmitResult> {
  const response = await request('/api/quality/submit', {
    method: 'POST',
    body: JSON.stringify({ source_table: sourceTable, partition }),
  })
  return (await response.json()) as SubmitResult
}

export async function loadClaimable(): Promise<Batch[]> {
  const payload = await fetchJson<{ items: Batch[] }>('/api/quality/batches/claimable')
  return payload.items
}

export async function claimBatch(batchNo: number): Promise<ClaimResult> {
  const response = await request(`/api/quality/batches/${batchNo}/claim`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
  return (await response.json()) as ClaimResult
}

export async function claimNext(roundNo?: number): Promise<ClaimResult> {
  const suffix = roundNo ? `?round_no=${roundNo}` : ''
  const response = await request(`/api/quality/batches/claim-next${suffix}`, { method: 'POST' })
  return (await response.json()) as ClaimResult
}

export async function executeBatch(batchNo: number): Promise<ExecuteResult> {
  const response = await request(`/api/quality/batches/${batchNo}/execute`, { method: 'POST' })
  return (await response.json()) as ExecuteResult
}

export async function loadRounds(): Promise<Round[]> {
  const payload = await fetchJson<{ items: Round[] }>('/api/quality/rounds')
  return payload.items
}

export async function loadRound(roundNo: number): Promise<Round> {
  return await fetchJson<Round>(`/api/quality/rounds/${roundNo}`)
}

export async function closeRound(roundNo: number): Promise<{ ok: boolean; message: string }> {
  const response = await request(`/api/quality/rounds/${roundNo}/close`, { method: 'POST' })
  return await response.json()
}

export async function loadImpact(entryId: number): Promise<ImpactResult> {
  return await fetchJson<ImpactResult>(`/api/quality/impact/${entryId}`)
}

export async function claimFromLineage(entryId: number, batchNo?: number): Promise<ClaimResult> {
  const response = await request(`/api/quality/impact/${entryId}/claim`, {
    method: 'POST',
    body: JSON.stringify({ batch_no: batchNo }),
  })
  return (await response.json()) as ClaimResult
}
