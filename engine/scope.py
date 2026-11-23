"""
引擎分析范围。

P1 起只看高位下拉 B0GLYW51HY。积加里对应投放组合名称是「JL-高位下拉」。
改范围只改这里，不要在规则里写死活动名。
"""

SCOPE_ASIN = "B0GLYW51HY"
SCOPE_PORTFOLIOS = ("JL-高位下拉",)
SCOPE_LABEL = "高位下拉 B0GLYW51HY（投放组合 JL-高位下拉）"


def campaign_predicate(alias: str = "") -> str:
    col = f"{alias}.campaign_id" if alias else "campaign_id"
    names = ", ".join("'" + p.replace("'", "''") + "'" for p in SCOPE_PORTFOLIOS)
    return (
        f"{col} IN (SELECT DISTINCT campaign_id FROM fact_campaign_daily "
        f"WHERE portfolio_name IN ({names}))"
    )


def list_campaigns(conn) -> list[dict]:
    names = ", ".join("'" + p.replace("'", "''") + "'" for p in SCOPE_PORTFOLIOS)
    cur = conn.execute(
        f"""
        SELECT campaign_id, MAX(campaign_name) AS campaign_name,
               MAX(portfolio_name) AS portfolio_name
        FROM fact_campaign_daily
        WHERE portfolio_name IN ({names})
        GROUP BY campaign_id
        ORDER BY campaign_name
        """
    )
    if hasattr(cur, "fetchall"):
        rows = cur.fetchall()
        if rows and hasattr(rows[0], "keys"):
            return [dict(r) for r in rows]
        return [{"campaign_id": r[0], "campaign_name": r[1], "portfolio_name": r[2]} for r in rows]
    return []
