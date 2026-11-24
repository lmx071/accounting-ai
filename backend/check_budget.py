"""预算报表模块验证脚本：造示例数据 → 打印报表 → 检查三档状态。

运行：python check_budget.py
（只依赖 Python 自带的 sqlite3，不需要装 dashscope/fastapi）
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.db import (
    get_conn, set_budget, get_month_spending,
    get_budget_status, check_alerts, get_report,
)

MONTH = "2026-11"


def seed_demo():
    """造一组故意覆盖 safe/warning/over 三档的示例数据"""
    conn = get_conn()
    conn.execute("DELETE FROM records")
    conn.execute("DELETE FROM budgets")

    # 交易流水（走队友的 records 表，trade_time 用队友的日期格式）
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    txns = [
        (980,  "餐饮", f"{MONTH}-02 12:00:00", now),
        (120,  "交通", f"{MONTH}-03 08:30:00", now),
        (850,  "购物", f"{MONTH}-05 19:00:00", now),
        (1000, "居住", f"{MONTH}-01 09:00:00", now),
        (210,  "医疗", f"{MONTH}-08 10:00:00", now),
    ]
    conn.executemany(
        "INSERT INTO records (amount, category, trade_time, created_at) VALUES (?, ?, ?, ?)",
        txns
    )

    # 预算：餐饮 980/1000→接近；购物 850/800→超支；交通 120/300→安全
    budgets = [
        (MONTH, "__TOTAL__", 3000),
        (MONTH, "餐饮", 1000),
        (MONTH, "交通", 300),
        (MONTH, "购物", 800),
        (MONTH, "居住", 1000),
        (MONTH, "医疗", 300),
    ]
    conn.executemany(
        "INSERT INTO budgets (month, category, amount) VALUES (?, ?, ?)",
        budgets
    )
    conn.commit()
    conn.close()


def run_checks():
    print("=== 1. 本月各类实际支出 ===")
    print(get_month_spending(MONTH))

    print("\n=== 2. 预算 vs 实际（含三档状态）===")
    status = get_budget_status(MONTH)
    print(f"总计: 预算 {status['total']['budget']} / 已花 {status['total']['spent']} "
          f"/ 使用率 {status['total']['ratio']} / {status['total']['status']}")
    for c in status["categories"]:
        print(f"  {c['category']}: {c['spent']}/{c['budget']} "
              f"({int(c['ratio']*100)}%) → {c['status']}")

    print("\n=== 3. 超支提醒 ===")
    for a in check_alerts(MONTH):
        print("  -", a)

    print("\n=== 4. 交给前端的完整报表 ===")
    report = get_report(MONTH)
    import json
    print(json.dumps(report, ensure_ascii=False, indent=2))

    # 断言：验证三档判断正确
    cat_status = {c["category"]: c["status"] for c in report["categories"]}
    assert cat_status["交通"] == "safe", "交通应为 safe"
    assert cat_status["餐饮"] == "warning", "餐饮应为 warning"
    assert cat_status["购物"] == "over", "购物应为 over"
    print("\n✅ 全部断言通过：safe / warning / over 三档判断正确")


if __name__ == "__main__":
    seed_demo()
    run_checks()
