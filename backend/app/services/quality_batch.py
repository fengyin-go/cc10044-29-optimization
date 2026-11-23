"""批量质量校验与断点续做。

一次提交只针对一个源表分区：先沿已建立的血缘找出读取该分区下游表，
再筛出有启用检查项的表进入本批；停用规则不读数据，无关表不入批。
每条结论都定位到实际检查的下游分区、规则、行号和批次，供之后回查。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.quality_data import (
    downstream_rows,
    has_downstream_partition,
    source_partition_options,
)
from app.services.quality_engine import (
    apply_rule_result,
    evaluate_rule_rows,
    rule_enabled,
)
from app.store import store

BATCH_PENDING = "待领取"
BATCH_CLAIMED = "已领取"
BATCH_DONE = "已完成"
ROUND_OPEN = "进行中"
ROUND_CLOSED = "已结案"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class QualityBatchService:
    def __init__(self) -> None:
        self.rounds: dict[int, dict[str, Any]] = {}
        self.batches: dict[int, dict[str, Any]] = {}
        self._round_seq = 0
        self._batch_seq = 0
        self._item_seq = 0

    # ------------------------------------------------------------------ 查询
    def source_partitions(self) -> list[dict[str, Any]]:
        """列出可提交的源表分区；决定待检分区的是源表实际分区，不是下游表。"""
        return source_partition_options()

    def _round_by_no(self, round_no: int) -> dict[str, Any] | None:
        return self.rounds.get(round_no)

    def _round_for(self, source_table: str, partition: str, *, open_only: bool = False) -> dict[str, Any] | None:
        for round_row in sorted(self.rounds.values(), key=lambda item: item["round_no"], reverse=True):
            if round_row["source_table"] != source_table or round_row["partition"] != partition:
                continue
            if open_only and round_row["status"] != ROUND_OPEN:
                continue
            return round_row
        return None

    def list_batches(
        self,
        *,
        status: str | None = None,
        round_no: int | None = None,
        source_table: str | None = None,
        partition: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self.batches.values())
        if status:
            rows = [row for row in rows if row["status"] == status]
        if round_no is not None:
            rows = [row for row in rows if row["round_no"] == round_no]
        if source_table:
            rows = [row for row in rows if row["source_table"] == source_table]
        if partition:
            rows = [row for row in rows if row["partition"] == partition]
        return [self._batch_view(row) for row in sorted(rows, key=lambda item: item["batch_no"])]

    def claimable_batches(self) -> list[dict[str, Any]]:
        return self.list_batches(status=BATCH_PENDING)

    def get_batch(self, batch_no: int) -> dict[str, Any] | None:
        batch = self.batches.get(batch_no)
        return self._batch_view(batch, detail=True) if batch else None

    def list_rounds(self, *, status: str | None = None) -> list[dict[str, Any]]:
        rows = sorted(self.rounds.values(), key=lambda item: item["round_no"])
        if status:
            rows = [row for row in rows if row["status"] == status]
        return [self._round_view(row) for row in rows]

    def get_round(self, round_no: int) -> dict[str, Any] | None:
        round_row = self._round_by_no(round_no)
        if round_row is None:
            return None
        view = self._round_view(round_row)
        view["results"] = self._round_results(round_row)
        view["lineage_writebacks"] = self._lineage_writebacks(round_row)
        view["duplicate_checks"] = self._duplicate_checks(round_row)
        view["checked_rows"] = self._checked_rows(round_row)
        return view

    # ------------------------------------------------------------------ 开批
    def submit_partition(self, source_table: str, partition: str) -> dict[str, Any]:
        """值班员提交一个源表分区，返回本批可领用的检查和复查入口。"""
        options = {
            (item["source_table"], item["partition"]) for item in self.source_partitions()
        }
        if (source_table, partition) not in options:
            return {
                "ok": False,
                "message": f"源表分区 {source_table}/{partition} 不存在，无法提交下游检查",
                "batch": None,
                "round": None,
            }

        round_row = self._round_for(source_table, partition, open_only=True)
        created_round = False
        if round_row is None:
            # 已有同源分区的历史批次说明本轮已结案，重新提交才允许开启新一轮；
            # 否则直接复用进行中的轮次，避免同一分区重复开盘。
            self._round_seq += 1
            round_row = {
                "round_no": self._round_seq,
                "source_table": source_table,
                "partition": partition,
                "status": ROUND_OPEN,
                "created_at": _now(),
                "closed_at": None,
                "batch_nos": [],
            }
            self.rounds[round_row["round_no"]] = round_row
            created_round = True

        batch, message = self._materialize_batch(round_row, trigger="初始检查")
        payload = {
            "ok": True,
            "message": message,
            "round": self._round_view(round_row),
            "batch": self._batch_view(batch, detail=True) if batch else None,
            "created_round": created_round,
        }
        if batch is None:
            payload["already_checked"] = self._round_results(round_row)
        return payload

    # ------------------------------------------------------------------ 领取
    def claim_batch(self, batch_no: int, *, operator: str = "数据工程组值班员") -> dict[str, Any]:
        batch = self.batches.get(batch_no)
        if batch is None:
            return {"ok": False, "message": f"复查批次 {batch_no} 不存在", "batch": None}
        round_row = self.rounds[batch["round_no"]]
        if round_row["status"] != ROUND_OPEN:
            return {"ok": False, "message": f"轮次 {round_row['round_no']} 已结案，不能再领取检查", "batch": None}
        if batch["status"] == BATCH_DONE:
            return {"ok": False, "message": f"批次 {batch_no} 已完成，结论可回查但无需重复校验", "batch": self._batch_view(batch, detail=True)}
        if batch["status"] == BATCH_CLAIMED:
            return {"ok": True, "message": f"批次 {batch_no} 已在 {batch['claimed_by']} 手中", "batch": self._batch_view(batch, detail=True)}
        batch["status"] = BATCH_CLAIMED
        batch["claimed_by"] = operator
        batch["claimed_at"] = _now()
        return {"ok": True, "message": f"已领取批次 {batch_no}，共 {len(batch['items'])} 张下游表的检查", "batch": self._batch_view(batch, detail=True)}

    def claim_next(self, *, round_no: int | None = None) -> dict[str, Any]:
        """领取尚未完成的一组检查；结案后返回空，不会重复核已核过的表。"""
        candidates = [
            batch for batch in sorted(self.batches.values(), key=lambda item: item["batch_no"])
            if batch["status"] == BATCH_PENDING
            and (round_no is None or batch["round_no"] == round_no)
            and self.rounds[batch["round_no"]]["status"] == ROUND_OPEN
        ]
        if not candidates:
            return {"ok": True, "message": "没有可领取的待检批次", "batch": None}
        return self.claim_batch(candidates[0]["batch_no"])

    # ------------------------------------------------------------------ 执行
    def execute_batch(self, batch_no: int) -> dict[str, Any]:
        """执行实际领到的检查项；每一项都读取下游分区真实行。"""
        batch = self.batches.get(batch_no)
        if batch is None:
            return {"ok": False, "message": f"复查批次 {batch_no} 不存在", "batch": None}
        round_row = self.rounds[batch["round_no"]]
        if round_row["status"] != ROUND_OPEN:
            return {"ok": False, "message": f"轮次 {round_row['round_no']} 已结案，不能再执行检查", "batch": self._batch_view(batch, detail=True)}
        if batch["status"] == BATCH_PENDING:
            return {"ok": False, "message": f"批次 {batch_no} 尚未领取，请先从血缘影响分析领取整组检查", "batch": self._batch_view(batch, detail=True)}
        if batch["status"] == BATCH_DONE:
            return {"ok": True, "message": f"批次 {batch_no} 已执行过，以下为已保留的检查结论", "batch": self._batch_view(batch, detail=True)}

        checked_at = _now()
        table_results: dict[str, dict[str, Any]] = {}
        for item in batch["items"]:
            table = item["table"]
            item_results: list[dict[str, Any]] = []
            for rule_id in item["rule_ids"]:
                rule = store.find("data_quality_rule", rule_id)
                if rule is None or not rule_enabled(rule):
                    # 停用的检查项不读取数据，直接记录跳过原因，不下结论。
                    item_results.append({
                        "rule_id": rule_id,
                        "skipped": True,
                        "reason": "规则已停用或不存在，未读取数据",
                    })
                    continue
                rows = downstream_rows(table, item["partition"])
                result = evaluate_rule_rows(
                    rule,
                    rows,
                    downstream_table=table,
                    partition=item["partition"],
                    source_table=batch["source_table"],
                    source_partition=batch["partition"],
                )
                apply_rule_result(
                    rule,
                    result,
                    downstream_table=table,
                    partition=item["partition"],
                    round_no=batch["round_no"],
                    batch_no=batch_no,
                    checked_at=checked_at,
                )
                result.update({
                    "rule_id": rule_id,
                    "rule_name": rule.get("规则名称"),
                    "rule_dimension": rule.get("校验维度"),
                    "table": table,
                    "partition": item["partition"],
                    "round_no": batch["round_no"],
                    "batch_no": batch_no,
                    "checked_at": checked_at,
                    # 完整保留实际读取的下游行，已核过的行可以回查。
                    "checked_rows": rows,
                })
                item_results.append(result)

            conclusions = [r for r in item_results if not r.get("skipped")]
            item["results"] = item_results
            item["status"] = BATCH_DONE
            item["executed_at"] = checked_at
            item["passed"] = bool(conclusions) and all(r["passed"] for r in conclusions)
            item["conclusion"] = self._item_conclusion(table, item, conclusions)
            table_results[table] = {
                "table": table,
                "partition": item["partition"],
                "passed": item["passed"],
                "conclusion": item["conclusion"],
                "rule_results": [
                    {
                        "rule_id": r["rule_id"],
                        "rule_name": r.get("rule_name"),
                        "passed": r.get("passed"),
                        "score": r.get("score"),
                        "rows_checked": r.get("rows_checked"),
                        "failed_rows": r.get("failed_rows"),
                        "conclusion": r.get("conclusion"),
                        "skipped": r.get("skipped", False),
                    }
                    for r in item_results
                ],
            }

        batch["status"] = BATCH_DONE
        batch["executed_at"] = checked_at
        batch["lineage_writebacks"] = self._write_back_lineage(batch)
        return {
            "ok": True,
            "message": f"批次 {batch_no} 已执行，实际检查 {len(batch['items'])} 张下游表",
            "batch": self._batch_view(batch, detail=True),
            "table_results": table_results,
        }

    # ------------------------------------------------------------------ 血缘影响
    def impact_review_groups(self, entry_id: int) -> dict[str, Any]:
        """血缘影响分析里给出尚未完成的整组检查，可直接领取。"""
        entry = store.find("lineage_graph", entry_id)
        if entry is None:
            return {"ok": False, "message": f"血缘条目 {entry_id} 不存在", "review_groups": []}

        from app.services.lineage_graph import LineageGraphService

        lineage = LineageGraphService()
        target_table = entry.get("目标表", "")
        downstream = lineage.trace_downstream(target_table)
        downstream_tables = {node["table"] for node in downstream["downstream_tree"]}

        groups: list[dict[str, Any]] = []
        for batch in sorted(self.batches.values(), key=lambda item: item["batch_no"]):
            round_row = self.rounds[batch["round_no"]]
            if round_row["status"] != ROUND_OPEN or batch["status"] == BATCH_DONE:
                continue
            tables = {item["table"] for item in batch["items"]}
            # 从上查看下游，或直接查看某张下游表自己的待检组，都应能领到。
            related = (
                tables & downstream_tables
                or batch["source_table"] == target_table
                or target_table in tables
            )
            if not related:
                continue
            groups.append({
                **self._batch_view(batch),
                "claimable": batch["status"] == BATCH_PENDING,
                "downstream_tables": sorted(tables),
            })

        return {
            "ok": True,
            "entry_id": entry_id,
            "table": target_table,
            "downstream_tables": sorted(downstream_tables),
            "review_groups": groups,
            "unfinished_count": len(groups),
            "message": (
                f"发现 {len(groups)} 组尚未完成的检查" if groups
                else "没有尚未完成的下游检查组"
            ),
        }

    def claim_from_lineage(self, entry_id: int, batch_no: int) -> dict[str, Any]:
        impact = self.impact_review_groups(entry_id)
        if not impact["ok"]:
            return {"ok": False, "message": impact["message"], "batch": None}
        match = next((g for g in impact["review_groups"] if g["batch_no"] == batch_no), None)
        if match is None:
            return {
                "ok": False,
                "message": f"批次 {batch_no} 不属于血缘条目 {entry_id} 尚未完成的下游检查",
                "batch": None,
            }
        return self.claim_batch(batch_no)

    # ------------------------------------------------------------------ 结案
    def close_round(self, round_no: int) -> dict[str, Any]:
        round_row = self._round_by_no(round_no)
        if round_row is None:
            return {"ok": False, "message": f"轮次 {round_no} 不存在", "round": None}
        if round_row["status"] == ROUND_CLOSED:
            return {"ok": True, "message": f"轮次 {round_no} 已结案", "round": self._round_view(round_row)}
        unfinished = [
            batch["batch_no"] for batch in self.batches.values()
            if batch["round_no"] == round_no and batch["status"] != BATCH_DONE
        ]
        if unfinished:
            return {
                "ok": False,
                "message": f"轮次 {round_no} 还有未完成的复查批次：{unfinished}，结案前请先领取并执行",
                "round": self._round_view(round_row),
            }
        round_row["status"] = ROUND_CLOSED
        round_row["closed_at"] = _now()
        return {
            "ok": True,
            "message": f"轮次 {round_no} 已结案，本轮结论和已核行保留可回查",
            "round": self.get_round(round_no),
        }

    # ------------------------------------------------------------------ 血缘登记挂钩
    def on_rule_registered(self, rule: dict[str, Any]) -> list[dict[str, Any]]:
        """新登记的启用规则要能进入本轮仍可领用的复查批次。"""
        if not rule_enabled(rule):
            return []
        table = str(rule.get("目标表") or "").strip()
        return [
            item for item in (
                self._supplement_round(round_row, trigger="规则新增复查")
                for round_row in sorted(self.rounds.values(), key=lambda row: row["round_no"])
                if round_row["status"] == ROUND_OPEN and table
            )
            if item is not None
        ]

    def _supplement_round(self, round_row: dict[str, Any], *, trigger: str) -> dict[str, Any] | None:
        batch, message = self._materialize_batch(round_row, trigger=trigger)
        if batch is None:
            return None
        return {
            "round_no": round_row["round_no"],
            "batch": self._batch_view(batch, detail=True),
            "message": message,
        }

    def on_lineage_registered(self, lineage_entry: dict[str, Any]) -> list[dict[str, Any]]:
        """血缘登记迟到时，把新增下游补进本分区尚未结案的复查批次。

        只补本轮还没核过的表；日销售表已核验则不会再被拉进复查。
        """
        if lineage_entry.get("status") != "已建立":
            return []
        sources = {
            part.strip()
            for part in str(lineage_entry.get("来源表列表", "")).split(",")
            if part.strip()
        }
        created: list[dict[str, Any]] = []
        for round_row in sorted(self.rounds.values(), key=lambda item: item["round_no"]):
            if round_row["status"] != ROUND_OPEN or round_row["source_table"] not in sources:
                continue
            batch, message = self._materialize_batch(round_row, trigger="血缘迟到复查")
            if batch is not None:
                created.append({
                    "round_no": round_row["round_no"],
                    "batch": self._batch_view(batch, detail=True),
                    "message": message,
                })
        return created

    # ------------------------------------------------------------------ 内部
    def _downstream_candidates(self, round_row: dict[str, Any]) -> list[dict[str, Any]]:
        """沿已建立血缘找出读取该源表的下游表，只保留有启用检查项的表。"""
        source_table = round_row["source_table"]
        candidates: dict[str, dict[str, Any]] = {}
        for entry in store.rows("lineage_graph"):
            if entry.get("status") != "已建立":
                continue
            sources = {
                part.strip()
                for part in str(entry.get("来源表列表", "")).split(",")
                if part.strip()
            }
            table = str(entry.get("目标表") or "").strip()
            if source_table not in sources or not table or table == source_table:
                continue
            enabled_rules = [
                rule for rule in store.rows("data_quality_rule")
                if rule.get("目标表") == table and rule_enabled(rule)
            ]
            if not enabled_rules:
                # 没有启用检查项的表不进本批，停用项也不读取数据。
                continue
            existing = candidates.get(table)
            rule_ids = [int(rule["id"]) for rule in enabled_rules]
            if existing is None:
                candidates[table] = {
                    "table": table,
                    "lineage_id": entry.get("id"),
                    "rule_ids": rule_ids,
                }
            else:
                # 同一张下游表可能有多条血缘条目，规则取并集，避免互相覆盖。
                existing["rule_ids"] = sorted({*existing["rule_ids"], *rule_ids})
        return list(candidates.values())

    def _already_scheduled(self, round_row: dict[str, Any], table: str, rule_id: int) -> bool:
        """同一轮里只排一次：待领取或已完成的检查都不会被重复拉进新批次。"""
        for batch in self.batches.values():
            if batch["round_no"] != round_row["round_no"]:
                continue
            for item in batch["items"]:
                if item["table"] == table and rule_id in item["rule_ids"]:
                    return True
        return False

    def _materialize_batch(
        self, round_row: dict[str, Any], *, trigger: str
    ) -> tuple[dict[str, Any] | None, str]:
        new_items: list[dict[str, Any]] = []
        for candidate in self._downstream_candidates(round_row):
            pending_rules = [
                rule_id for rule_id in candidate["rule_ids"]
                if not self._already_scheduled(round_row, candidate["table"], rule_id)
            ]
            if not pending_rules:
                continue
            self._item_seq += 1
            new_items.append({
                "item_id": self._item_seq,
                "table": candidate["table"],
                "lineage_id": candidate["lineage_id"],
                "partition": round_row["partition"],
                "rule_ids": pending_rules,
                "status": BATCH_PENDING,
                "results": [],
                "executed_at": None,
            })

        if not new_items:
            return None, "本轮该分区的下游检查项都已核过，没有新的待检表"

        pending_batch = next((
            batch for batch in sorted(self.batches.values(), key=lambda item: item["batch_no"], reverse=True)
            if batch["round_no"] == round_row["round_no"] and batch["status"] == BATCH_PENDING
        ), None)
        if pending_batch is not None:
            pending_batch["items"].extend(new_items)
            pending_batch["trigger"] = trigger if trigger != "初始检查" else pending_batch["trigger"]
            pending_batch["updated_at"] = _now()
            tables = sorted({item["table"] for item in new_items})
            return pending_batch, (
                f"新增下游 {len(tables)} 张表（{'、'.join(tables)}）的检查，"
                f"已并入待领取批次 {pending_batch['batch_no']}"
            )

        self._batch_seq += 1
        batch = {
            "batch_no": self._batch_seq,
            "round_no": round_row["round_no"],
            "source_table": round_row["source_table"],
            "partition": round_row["partition"],
            "trigger": trigger,
            "status": BATCH_PENDING,
            "created_at": _now(),
            "claimed_by": None,
            "claimed_at": None,
            "executed_at": None,
            "items": new_items,
        }
        self.batches[batch["batch_no"]] = batch
        round_row["batch_nos"].append(batch["batch_no"])
        tables = sorted({item["table"] for item in new_items})
        return batch, (
            f"已生成{'复查' if trigger != '初始检查' else ''}批次 {batch['batch_no']}，"
            f"待检下游 {len(tables)} 张表（{'、'.join(tables)}），可从血缘影响分析领取"
        )

    def _write_back_lineage(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        writebacks: list[dict[str, Any]] = []
        for item in batch["items"]:
            conclusions = [r for r in item.get("results", []) if not r.get("skipped")]
            passed = bool(conclusions) and all(r["passed"] for r in conclusions)
            entry = store.find("lineage_graph", int(item["lineage_id"])) if item.get("lineage_id") else None
            if entry is None:
                continue
            entry["质量状态"] = "质量通过" if passed else "质量未通过"
            entry["最近校验分区"] = item["partition"]
            entry["最近校验轮次"] = batch["round_no"]
            entry["最近校验批次"] = batch["batch_no"]
            entry["最近校验时间"] = item.get("executed_at")
            entry["质量结论"] = item.get("conclusion", "")
            entry["已核规则数"] = len(conclusions)
            writebacks.append({
                "lineage_id": entry.get("id"),
                "table": item["table"],
                "partition": item["partition"],
                "round_no": batch["round_no"],
                "batch_no": batch["batch_no"],
                "质量状态": entry["质量状态"],
                "质量结论": entry["质量结论"],
            })
        return writebacks

    def _item_conclusion(self, table: str, item: dict[str, Any], conclusions: list[dict[str, Any]]) -> str:
        if not conclusions:
            return f"{table} 分区 {item['partition']} 没有可执行的启用检查项"
        failed = [r for r in conclusions if not r["passed"]]
        head = f"{table} 分区 {item['partition']} 共 {len(conclusions)} 项检查，"
        if not failed:
            return head + "全部通过"
        details = "；".join(
            f"规则「{r.get('rule_name')}」未通过（{r.get('reason')}）" for r in failed
        )
        return head + details

    def _round_results(self, round_row: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for batch in sorted(self.batches.values(), key=lambda item: item["batch_no"]):
            if batch["round_no"] != round_row["round_no"]:
                continue
            for item in batch["items"]:
                for result in item.get("results", []):
                    if result.get("skipped"):
                        continue
                    results.append({
                        "table": result["table"],
                        "partition": result["partition"],
                        "round_no": result["round_no"],
                        "batch_no": result["batch_no"],
                        "rule_id": result["rule_id"],
                        "rule_name": result["rule_name"],
                        "dimension": result["dimension"],
                        "field": result["field"],
                        "passed": result["passed"],
                        "score": result["score"],
                        "threshold": result["threshold"],
                        "rows_checked": result["rows_checked"],
                        "failed_rows": result["failed_rows"],
                        "conclusion": result["conclusion"],
                        "checked_at": result["checked_at"],
                    })
        return results

    def _round_view(self, round_row: dict[str, Any]) -> dict[str, Any]:
        batches = [
            self._batch_view(batch)
            for batch in sorted(self.batches.values(), key=lambda item: item["batch_no"])
            if batch["round_no"] == round_row["round_no"]
        ]
        return {
            "round_no": round_row["round_no"],
            "source_table": round_row["source_table"],
            "partition": round_row["partition"],
            "status": round_row["status"],
            "created_at": round_row["created_at"],
            "closed_at": round_row["closed_at"],
            "batches": batches,
            "batch_count": len(batches),
            "claimable_count": sum(1 for b in batches if b["status"] == BATCH_PENDING),
            "table_results_summary": self._table_summary(round_row),
        }

    def _table_summary(self, round_row: dict[str, Any]) -> list[dict[str, Any]]:
        summary: dict[str, dict[str, Any]] = {}
        for result in self._round_results(round_row):
            table = result["table"]
            bucket = summary.setdefault(table, {
                "table": table,
                "partition": result["partition"],
                "round_no": round_row["round_no"],
                "checks": 0,
                "failed_checks": 0,
                "batch_nos": [],
                "last_batch_no": result["batch_no"],
            })
            bucket["checks"] += 1
            if not result["passed"]:
                bucket["failed_checks"] += 1
            if result["batch_no"] not in bucket["batch_nos"]:
                bucket["batch_nos"].append(result["batch_no"])
            bucket["last_batch_no"] = result["batch_no"]
            bucket["passed"] = bucket["failed_checks"] == 0
        return list(summary.values())

    def _lineage_writebacks(self, round_row: dict[str, Any]) -> list[dict[str, Any]]:
        """血缘回填按批次快照汇总；后续轮次覆盖最近状态也不会冲掉历史轮次。"""
        rows: list[dict[str, Any]] = []
        for batch in sorted(self.batches.values(), key=lambda item: item["batch_no"]):
            if batch["round_no"] != round_row["round_no"]:
                continue
            for writeback in batch.get("lineage_writebacks", []):
                rows.append({
                    "lineage_id": writeback["lineage_id"],
                    "target_table": writeback["table"],
                    "sources": (store.find("lineage_graph", int(writeback["lineage_id"])) or {}).get("来源表列表", ""),
                    "quality_status": writeback["质量状态"],
                    "partition": writeback["partition"],
                    "round_no": writeback["round_no"],
                    "batch_no": writeback["batch_no"],
                    "conclusion": writeback["质量结论"],
                })
        return rows

    def _duplicate_checks(self, round_row: dict[str, Any]) -> list[dict[str, Any]]:
        counts: dict[tuple[str, str, int], list[int]] = {}
        for batch in self.batches.values():
            if batch["round_no"] != round_row["round_no"]:
                continue
            for item in batch["items"]:
                for result in item.get("results", []):
                    if result.get("skipped"):
                        continue
                    key = (result["table"], result["partition"], result["rule_id"])
                    counts.setdefault(key, []).append(batch["batch_no"])
        return [
            {"table": key[0], "partition": key[1], "rule_id": key[2], "batch_nos": batch_nos}
            for key, batch_nos in counts.items()
            if len(batch_nos) > 1
        ]

    def _checked_rows(self, round_row: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for batch in self.batches.values():
            if batch["round_no"] != round_row["round_no"]:
                continue
            for item in batch["items"]:
                for result in item.get("results", []):
                    if result.get("skipped"):
                        continue
                    rows.append({
                        "table": result["table"],
                        "partition": result["partition"],
                        "rule_id": result["rule_id"],
                        "row_ids": [row.get("id") for row in result.get("checked_rows", [])],
                        "failed_row_ids": [row.get("row_id") for row in result.get("failed_rows", [])],
                        "batch_no": result["batch_no"],
                    })
        return rows

    def _batch_view(self, batch: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
        items = []
        for item in batch["items"]:
            entry = {
                "item_id": item["item_id"],
                "table": item["table"],
                "partition": item["partition"],
                "lineage_id": item.get("lineage_id"),
                "rule_ids": list(item["rule_ids"]),
                "status": item["status"],
                "executed_at": item.get("executed_at"),
                "passed": item.get("passed"),
                "conclusion": item.get("conclusion"),
            }
            if detail:
                entry["results"] = [
                    {
                        "rule_id": result["rule_id"],
                        "rule_name": result.get("rule_name"),
                        "passed": result.get("passed"),
                        "score": result.get("score"),
                        "threshold": result.get("threshold"),
                        "rows_checked": result.get("rows_checked"),
                        "failed_rows": result.get("failed_rows"),
                        "reason": result.get("reason"),
                        "conclusion": result.get("conclusion"),
                        "skipped": result.get("skipped", False),
                        "checked_rows": result.get("checked_rows", []),
                    }
                    for result in item.get("results", [])
                ]
            items.append(entry)

        view = {
            "batch_no": batch["batch_no"],
            "round_no": batch["round_no"],
            "source_table": batch["source_table"],
            "partition": batch["partition"],
            "trigger": batch["trigger"],
            "status": batch["status"],
            "created_at": batch["created_at"],
            "claimed_by": batch["claimed_by"],
            "claimed_at": batch["claimed_at"],
            "executed_at": batch["executed_at"],
            "item_count": len(items),
            "items": items,
            "downstream_partitions": {
                item["table"]: {
                    "partition": item["partition"],
                    "has_partition_data": has_downstream_partition(item["table"], item["partition"]),
                }
                for item in batch["items"]
            },
            "lineage_writebacks": batch.get("lineage_writebacks", []),
        }
        if detail:
            view["lineage_writebacks"] = batch.get("lineage_writebacks", [])
        return view


quality_batches = QualityBatchService()
