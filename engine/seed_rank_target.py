"""把卡位配置写进生产库。默认不执行，先 --dry 看会写什么。

配置依据（窗口 2026-10-31~11-13 实测）：
  · pulldown machine 21937639215436 —— bid 0.5 / 20 击 / 1 单 / ACOS 13.9%，是 scope 内唯一健康词
  · lat pulldown 277840567753078 —— bid 0.4 / 60 击 / 1 单 / ACOS 53.6%，勉强在闸内
  · lat pulldown 151878211767486 —— ACOS 123%，不进卡位（该降价，不是该抢位）
"""
import sqlite3, sys
from datetime import datetime

DB = "/Users/baoyanan/自动化广告平台/ingest/data/ads.sqlite3"
ASIN = "B0GLYW51HY"

TARGETS = [
    # keyword_id, text, campaign_id, ad_group_id, max_bid, step, guard, max_pushes
    ("21937639215436", "pulldown machine", "46818486452618", "37057323685447", 1.20, 0.10, 1.5, 6),
    ("277840567753078", "lat pulldown", "237761921757002", "201416328459856", 0.80, 0.05, 1.2, 4),
]

def main(dry: bool) -> None:
    conn = sqlite3.connect(DB)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for kid, text, camp, adg, max_bid, step, guard, pushes in TARGETS:
        row = dict(
            market_id=1, keyword_id=kid, keyword_text=text, campaign_id=camp,
            ad_group_id=adg, asin=ASIN,
            goal_page=1, goal_position=4,          # 卡到搜索结果顶部（第 1 页 1-4 位）
            push_mode="bid", max_bid=max_bid, max_tos_pct=900.0,
            step_mode="fixed", step_value=step,
            success_streak_required=5, decrease_mode="percent", decrease_value=5.0,
            restore_on_end=1, acos_guard_mult=guard, max_pushes=pushes,
            status="active", created_at=now, expires_at=None,
        )
        print(f"{'[dry] ' if dry else ''}卡位 {text} ({kid}) 目标第1页第4位 "
              f"bid上限${max_bid} 每次+${step} 闸={guard}× 上限{pushes}次")
        if not dry:
            cols = ",".join(row)
            ph = ",".join("?" * len(row))
            conn.execute(f"insert into rank_target ({cols}) values ({ph})", tuple(row.values()))
    if not dry:
        conn.commit()
        print(f"已写入 {len(TARGETS)} 条")
    conn.close()

if __name__ == "__main__":
    main("--dry" in sys.argv)
