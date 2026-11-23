"""批量质量校验与断点续做的业务接口。

值班员的入口在这里：先取源表分区，再提交一个分区开批；随后从批次或
血缘影响分析领取整组检查并执行，最后结案。所有结论都能按轮次回查。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.quality_batch import quality_batches
from app.store import store

router = APIRouter(prefix="/api/quality", tags=["批量质量校验"])


class SubmitPayload(BaseModel):
    source_table: str = Field(min_length=1)
    partition: str = Field(min_length=1)


class ClaimPayload(BaseModel):
    operator: str = "数据工程组值班员"
    batch_no: int | None = None


@router.get("/source-partitions")
def source_partitions() -> dict[str, Any]:
    """供值班员选择待检分区的源表分区清单。"""
    items = quality_batches.source_partitions()
    return {"items": items, "total": len(items)}


@router.post("/submit")
def submit_partition(payload: SubmitPayload) -> dict[str, Any]:
    return quality_batches.submit_partition(payload.source_table.strip(), payload.partition.strip())


@router.get("/batches")
def list_batches(
    status: str | None = Query(default=None),
    round_no: int | None = Query(default=None),
    source_table: str | None = Query(default=None),
    partition: str | None = Query(default=None),
) -> dict[str, Any]:
    items = quality_batches.list_batches(
        status=status, round_no=round_no, source_table=source_table, partition=partition)
    return {"items": items, "total": len(items)}


@router.get("/batches/claimable")
def claimable_batches() -> dict[str, Any]:
    items = quality_batches.claimable_batches()
    return {"items": items, "total": len(items)}


@router.post("/batches/claim-next")
def claim_next(round_no: int | None = Query(default=None)) -> dict[str, Any]:
    return quality_batches.claim_next(round_no=round_no)


@router.get("/batches/{batch_no}")
def get_batch(batch_no: int) -> dict[str, Any]:
    batch = quality_batches.get_batch(batch_no)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"复查批次 {batch_no} 不存在")
    return batch


@router.post("/batches/{batch_no}/claim")
def claim_batch(batch_no: int, payload: ClaimPayload | None = None) -> dict[str, Any]:
    operator = (payload.operator if payload else None) or "数据工程组值班员"
    return quality_batches.claim_batch(batch_no, operator=operator)


@router.post("/batches/{batch_no}/execute")
def execute_batch(batch_no: int) -> dict[str, Any]:
    return quality_batches.execute_batch(batch_no)


@router.get("/rounds")
def list_rounds(status: str | None = Query(default=None)) -> dict[str, Any]:
    items = quality_batches.list_rounds(status=status)
    return {"items": items, "total": len(items)}


@router.get("/rounds/{round_no}")
def get_round(round_no: int) -> dict[str, Any]:
    round_row = quality_batches.get_round(round_no)
    if round_row is None:
        raise HTTPException(status_code=404, detail=f"校验轮次 {round_no} 不存在")
    return round_row


@router.post("/rounds/{round_no}/close")
def close_round(round_no: int) -> dict[str, Any]:
    return quality_batches.close_round(round_no)


@router.get("/impact/{entry_id}")
def impact_review_groups(entry_id: int) -> dict[str, Any]:
    """血缘影响分析：返回尚未完成、可以直接领取的整组检查。"""
    if store.find("lineage_graph", entry_id) is None:
        raise HTTPException(status_code=404, detail=f"血缘条目 {entry_id} 不存在")
    return quality_batches.impact_review_groups(entry_id)


@router.post("/impact/{entry_id}/claim")
def claim_from_lineage(entry_id: int, payload: ClaimPayload | None = None) -> dict[str, Any]:
    if store.find("lineage_graph", entry_id) is None:
        raise HTTPException(status_code=404, detail=f"血缘条目 {entry_id} 不存在")
    operator = (payload.operator if payload else None) or "数据工程组值班员"
    if payload and payload.batch_no is not None:
        return quality_batches.claim_from_lineage(entry_id, payload.batch_no)
    result = quality_batches.impact_review_groups(entry_id)
    groups = result.get("review_groups", [])
    if not groups:
        return {"ok": True, "message": result.get("message", "没有尚未完成的下游检查组"), "batch": None}
    claimable = next(
        (g for g in groups if g["status"] == "待领取"),
        next((g for g in groups if g["status"] == "已领取"), groups[0]),
    )
    return quality_batches.claim_batch(claimable["batch_no"], operator=operator)
