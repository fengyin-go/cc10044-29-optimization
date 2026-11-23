"""批量质量校验使用的实际行数据。

这里保存的是源表分区和下游表分区的真实行记录，质量校验必须逐条读取这些行，
不能只看元数据或上游结论。所有表共用 ``dt`` 分区字段。
"""
from __future__ import annotations

from typing import Any

SOURCE_TABLE = "ods_orders"
PARTITION_FIELD = "dt"


def _order_rows(partition: str, start_id: int, count: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(count):
        row_id = start_id + index
        customer_no = 1 + index % 6
        rows.append({
            "id": row_id,
            "order_id": f"SO{row_id}",
            "dt": partition,
            "customer_id": f"C{customer_no:03d}",
            "amount": f"{120 + index * 17}.50",
            "channel": "app" if index % 2 == 0 else "web",
        })
    return rows


def _sales_rows(partition: str, start_id: int, count: int, broken_row: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(count):
        row_id = start_id + index
        customer_id = f"C{1 + index % 6:03d}"
        if broken_row is not None and index == broken_row:
            customer_id = "C999"
        rows.append({
            "id": row_id,
            "dt": partition,
            "customer_id": customer_id,
            "amount": f"{980 + index * 35}.00",
            "order_cnt": str(3 + index),
        })
    return rows


def _report_rows(partition: str, start_id: int, count: int, missing_row: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(count):
        row_id = start_id + index
        customer_id = f"C{1 + index % 6:03d}"
        if missing_row is not None and index == missing_row:
            customer_id = ""
        rows.append({
            "id": row_id,
            "dt": partition,
            "customer_id": customer_id,
            "amount": f"{760 + index * 41}.00",
            "order_cnt": str(2 + index),
        })
    return rows


# 源表分区：值班员一次只提交其中一个分区。
SOURCE_PARTITIONS: dict[str, list[dict[str, Any]]] = {
    SOURCE_TABLE: [
        *_order_rows("dt=2026-11-21", 9001, 12),
        *_order_rows("dt=2026-11-20", 9101, 12),
    ],
}

# 下游表实际落盘行。分区键与源表分区一一对应。
DOWNSTREAM_ROWS: dict[str, dict[str, list[dict[str, Any]]]] = {
    "dws_sales_daily": {
        # 日销售表本轮没有空客户号，金额也全部非负。
        "dt=2026-11-21": _sales_rows("dt=2026-11-21", 3001, 8),
        "dt=2026-11-20": _sales_rows("dt=2026-11-20", 3101, 8),
    },
    "ads_customer_daily_report": {
        # 客户日报第 3 行客户号缺失，这是本轮真实存在的质量问题。
        "dt=2026-11-21": _report_rows("dt=2026-11-21", 5001, 6, missing_row=2),
        "dt=2026-11-20": _report_rows("dt=2026-11-20", 5101, 6),
    },
}

SOURCE_TABLE_INPUTS: dict[str, str] = {
    "dws_sales_daily": SOURCE_TABLE,
    "ads_customer_daily_report": SOURCE_TABLE,
}


def source_rows(source_table: str, partition: str) -> list[dict[str, Any]]:
    return [
        row for row in SOURCE_PARTITIONS.get(source_table, [])
        if row.get("dt") == partition
    ]


def downstream_rows(table: str, partition: str) -> list[dict[str, Any]]:
    return list(DOWNSTREAM_ROWS.get(table, {}).get(partition, []))


def has_downstream_partition(table: str, partition: str) -> bool:
    return partition in DOWNSTREAM_ROWS.get(table, {})


def source_partition_options() -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for source_table, rows in SOURCE_PARTITIONS.items():
        partitions = sorted({str(row["dt"]) for row in rows}, reverse=True)
        for partition in partitions:
            options.append({
                "source_table": source_table,
                "partition": partition,
                "source_row_count": sum(1 for row in rows if row.get("dt") == partition),
            })
    return options
