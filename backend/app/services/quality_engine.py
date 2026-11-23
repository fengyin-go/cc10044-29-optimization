"""质量规则的行级判定引擎。

批量校验和单规则校验共用这里的逻辑：结论只来自实际读取到的行，
每条判定都保留下游分区、规则、检查行数和失败行，便于回查。
"""
from __future__ import annotations

from typing import Any

# 停用（已忽略）的检查项不读取数据，也不进入逐表结论。
DISABLED_RULE_STATUSES = frozenset({"已忽略"})


def rule_enabled(rule: dict[str, Any]) -> bool:
    if rule.get("启用") is False or str(rule.get("启用", "")).strip() in {"否", "停用", "0"}:
        return False
    return rule.get("status") not in DISABLED_RULE_STATUSES


def rule_field(rule: dict[str, Any]) -> str:
    field = str(rule.get("校验字段") or "").strip()
    if field:
        return field
    dimension = rule.get("校验维度", "")
    return {
        "completeness": "customer_id",
        "uniqueness": "order_id",
        "accuracy": "amount",
        "consistency": "format",
        "timeliness": "dt",
    }.get(dimension, "customer_id")


def _bad_row(row: dict[str, Any], field: str, reason: str) -> dict[str, Any]:
    return {
        "row_id": row.get("id"),
        "field": field,
        "value": row.get(field),
        "reason": reason,
    }


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def evaluate_rule_rows(
    rule: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    downstream_table: str,
    partition: str,
    source_table: str | None = None,
    source_partition: str | None = None,
) -> dict[str, Any]:
    """按规则逐条核查下游分区实际行，返回可回查的判定结果。

    源表行只用于上下文记录，不参与 ``passed`` 的推导；如果下游分区没有数据，
    直接判定为未通过，而不是拿源表正常来顶替。
    """
    dimension = str(rule.get("校验维度") or "")
    field = rule_field(rule)
    threshold = float(rule.get("阈值") or 0.0)
    rows_checked = len(rows)

    if rows_checked == 0:
        return {
            "passed": False,
            "score": 0.0,
            "threshold": threshold,
            "dimension": dimension,
            "field": field,
            "rows_checked": 0,
            "failed_rows": [],
            "reason": f"下游分区 {downstream_table}/{partition} 没有实际行，无法证明质量合格",
            "conclusion": (
                f"{downstream_table} 分区 {partition} 未读取到实际行，规则"
                f"「{rule.get('规则名称')}」判定未通过；源表行数不能替代下游检查"
            ),
        }

    failed_rows: list[dict[str, Any]] = []

    if dimension == "completeness":
        for row in rows:
            if _is_blank(row.get(field)):
                failed_rows.append(_bad_row(row, field, f"{field} 为空"))
    elif dimension == "uniqueness":
        seen: dict[str, int] = {}
        for row in rows:
            seen[str(row.get(field, ""))] = seen.get(str(row.get(field, "")), 0) + 1
        for row in rows:
            if seen.get(str(row.get(field, "")), 0) > 1:
                failed_rows.append(_bad_row(row, field, f"{field} 重复"))
    elif dimension == "accuracy":
        for row in rows:
            value = row.get(field)
            try:
                number = float(str(value).replace(",", ""))
            except (TypeError, ValueError):
                failed_rows.append(_bad_row(row, field, f"{field} 不是合法数值"))
                continue
            if number < 0:
                failed_rows.append(_bad_row(row, field, f"{field} 为负数"))
    elif dimension == "consistency":
        for row in rows:
            if _is_blank(row.get(field)):
                failed_rows.append(_bad_row(row, field, f"{field} 格式为空"))
    elif dimension == "timeliness":
        for row in rows:
            if str(row.get("dt", "")) != partition:
                failed_rows.append(_bad_row(row, "dt", "行分区与待检分区不一致"))
    else:
        failed_rows = [_bad_row(row, field, f"未知校验维度 {dimension}") for row in rows]

    good_rows = rows_checked - len(failed_rows)
    score = round(good_rows / rows_checked, 4)
    passed = score >= threshold

    if failed_rows:
        sample = "、".join(str(item["row_id"]) for item in failed_rows[:5])
        detail = f"失败行 {len(failed_rows)} 条（行号：{sample}）"
    else:
        detail = "未发现失败行"

    return {
        "passed": passed,
        "score": score,
        "threshold": threshold,
        "dimension": dimension,
        "field": field,
        "rows_checked": rows_checked,
        "failed_rows": failed_rows,
        "reason": detail,
        "conclusion": (
            f"{downstream_table} 分区 {partition} 按规则「{rule.get('规则名称')}」"
            f"检查实际行 {rows_checked} 条，{detail}，"
            f"得分 {score}（阈值 {threshold}），判定{'通过' if passed else '未通过'}"
            + (
                f"；上游 {source_table}/{source_partition} 仅供参考，不作为下游合格依据"
                if source_table
                else ""
            )
        ),
    }


def apply_rule_result(
    rule: dict[str, Any],
    result: dict[str, Any],
    *,
    downstream_table: str,
    partition: str,
    round_no: int,
    batch_no: int,
    checked_at: str,
) -> None:
    """把一次实际校验结果回写到规则行，保留最近一次检查的定位信息。"""
    if not rule_enabled(rule):
        return
    rule["最近分数"] = str(result["score"])
    rule["status"] = "已通过" if result["passed"] else "未通过"
    rule["pending"] = not result["passed"]
    rule["abnormal"] = not result["passed"]
    rule["最近校验分区"] = partition
    rule["最近校验轮次"] = round_no
    rule["最近校验批次"] = batch_no
    rule["最近校验时间"] = checked_at
    rule["最近校验表"] = downstream_table
