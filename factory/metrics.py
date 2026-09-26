from __future__ import annotations
import csv
import io
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .util import FactoryError, digest, integer

COLUMNS = ["placement_id","date","timezone","channel","impressions","clicks","orders","metric_definition","source_kind"]

def parse_metrics(content: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    if not reader.fieldnames or len(reader.fieldnames)!=len(COLUMNS) or set(reader.fieldnames)!=set(COLUMNS):
        raise FactoryError("CSV 列不匹配 templates/metrics.csv；请先完成渠道列映射。")
    result,seen = [],set()
    for line,row in enumerate(reader,2):
        if None in row or any(v is None for v in row.values()):
            raise FactoryError(f"CSV 第 {line} 行列数错误。")
        for k,v in row.items():
            if v.lstrip().startswith(("=","+","-","@","\t","\r")):
                raise FactoryError(f"CSV 第 {line} 行含潜在公式前缀。")
        try:
            date.fromisoformat(row["date"])
            ZoneInfo(row["timezone"])
            for k in ("impressions","clicks","orders"):
                # No percentages, decimals, commas or NaN in raw counts.
                if not row[k].isdigit():
                    raise ValueError(k)
                row[k]=integer(int(row[k]),k)
        except (ValueError,ZoneInfoNotFoundError) as exc:
            raise FactoryError(f"CSV 第 {line} 行日期、时区或原始计数无效。") from exc
        if row["clicks"]>row["impressions"]:
            raise FactoryError("点击不能大于曝光；检查指标口径。")
        if not all(row[k].strip() for k in ("placement_id","channel","metric_definition")):
            raise FactoryError("使用ID、渠道和指标口径不能为空。")
        if row["source_kind"] not in {"platform_export","verified_manual","demo"}:
            raise FactoryError("source_kind 须为 platform_export、verified_manual 或 demo。")
        row["key"] = digest({k:row[k] for k in ("placement_id","date","timezone","channel","metric_definition")})
        if row["key"] in seen:
            raise FactoryError("同一 CSV 出现重复的使用ID/日期/时区/渠道/口径，停止导入。")
        seen.add(row["key"])
        row["ctr"] = row["clicks"]/row["impressions"] if row["impressions"] else None
        # Orders/clicks is explicitly not an interchangeable platform conversion rate.
        row["orders_per_click"] = row["orders"]/row["clicks"] if row["clicks"] else None
        row["sample_note"] = "仅作描述；不能证明图片造成提升" if row["impressions"]>=1000 else "样本较少；不能判定优胜或爆款"
        result.append(row)
    if not result:
        raise FactoryError("CSV 没有数据行。")
    return result
