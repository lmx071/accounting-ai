import sqlite3
import os
from datetime import datetime
from typing import List, Dict, Optional, Union

# 数据库文件路径
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "account_book.db")

def get_conn() -> sqlite3.Connection:
    """获取数据库连接，自动创建data目录"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库，创建核心账单表"""
    conn = get_conn()
    cursor = conn.cursor()

    # 账单记录表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        amount REAL NOT NULL,
        category TEXT NOT NULL,
        merchant TEXT DEFAULT '',
        trade_time TEXT NOT NULL,
        remark TEXT DEFAULT '',
        created_at TEXT NOT NULL
    )
    """)

    # ========== （预算模块在此处追加） ==========
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS budgets (
        month    TEXT NOT NULL,
        category TEXT NOT NULL,
        amount   REAL NOT NULL,
        UNIQUE(month, category)
    )
    """)

    # ============================================================

    conn.commit()
    conn.close()


# ---------------------- 账单核心操作 ----------------------
def add_record(amount: float, category: str, merchant: str = "", 
               trade_time: str = None, remark: str = "") -> int:
    """新增一条账单记录"""
    if trade_time is None:
        trade_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO records (amount, category, merchant, trade_time, remark, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (amount, category, merchant, trade_time, remark, created_at))
    conn.commit()
    record_id = cursor.lastrowid
    conn.close()
    return record_id


def update_record_category(record_id: int, category: str) -> bool:
    """按ID修改账单分类（人工纠错用）"""
    conn = get_conn()
    try:
        cursor = conn.execute(
            "UPDATE records SET category = ? WHERE id = ?",
            (category, record_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ---------------------- 查询统计操作----------------------
def get_records_by_time(start_time: str = None, end_time: str = None, 
                        category: Union[str, List[str], None] = None) -> List[Dict]:
    """按时间+分类查询账单明细
    - category 传字符串：查询单个分类（兼容原有写法）
    - category 传列表：一次性查询多个分类
    - category 为空：不限制分类，查询全部
    """
    conn = get_conn()
    cursor = conn.cursor()
    
    sql = "SELECT * FROM records WHERE 1=1"
    params = []
    
    if start_time:
        sql += " AND trade_time >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND trade_time <= ?"
        params.append(end_time)
    
    # 分类参数归一化：兼容字符串和列表两种输入
    if category is not None:
        if isinstance(category, str):
            categories = [category]
        else:
            categories = category
        
        if len(categories) > 0:
            placeholders = ",".join(["?"] * len(categories))
            sql += f" AND category IN ({placeholders})"
            params.extend(categories)
    
    sql += " ORDER BY trade_time DESC"
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def get_category_sum(start_time: str = None, end_time: str = None,
                     category: Union[str, List[str], None] = None) -> List[Dict]:
    """按分类汇总消费金额（用于类别排行、分类求和查询）
    - category 传字符串：统计单个分类（兼容原有写法）
    - category 传列表：一次性统计多个分类
    - category 为空：统计全部分类
    """
    conn = get_conn()
    cursor = conn.cursor()
    
    sql = """
        SELECT category, SUM(amount) as total_amount, COUNT(*) as count
        FROM records 
        WHERE 1=1
    """
    params = []
    if start_time:
        sql += " AND trade_time >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND trade_time <= ?"
        params.append(end_time)
    
    # 分类参数归一化
    if category is not None:
        if isinstance(category, str):
            categories = [category]
        else:
            categories = category
        
        if len(categories) > 0:
            placeholders = ",".join(["?"] * len(categories))
            sql += f" AND category IN ({placeholders})"
            params.extend(categories)
    
    sql += " GROUP BY category ORDER BY total_amount DESC"
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ========== 预算模块在此处追加函数==========
# 分类白名单与 llm.py 保持一致（8 类）
BUDGET_CATEGORIES = ["餐饮", "交通", "购物", "娱乐", "居住", "医疗", "教育", "其他"]
TOTAL_KEY = "__TOTAL__"   # 总预算在 budgets 表里的类别占位符


def set_budget(month: str, category: str, amount: float) -> None:
    """设定/更新某月某类预算：有则改、无则增（总预算 category 传 __TOTAL__）"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO budgets (month, category, amount) VALUES (?, ?, ?) "
        "ON CONFLICT(month, category) DO UPDATE SET amount=excluded.amount",
        (month, category, amount)
    )
    conn.commit()
    conn.close()


def get_month_spending(month: str) -> Dict[str, float]:
    """汇总某月各类实际支出，返回 {类别: 金额}（数据来自队友的 records 表）"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT category, SUM(amount) AS total FROM records "
        "WHERE substr(trade_time, 1, 7) = ? GROUP BY category",
        (month,)
    ).fetchall()
    conn.close()
    return {row["category"]: row["total"] for row in rows}


def _judge_status(ratio: float) -> str:
    """按使用率判三档：over 超支 / warning 接近 / safe 安全"""
    if ratio >= 1.0:
        return "over"
    elif ratio >= 0.8:
        return "warning"
    return "safe"


def get_budget_status(month: str) -> Dict:
    """预算 vs 实际：算总预算和各类别的已花/剩余/使用率/状态"""
    spending = get_month_spending(month)
    conn = get_conn()
    rows = conn.execute(
        "SELECT category, amount FROM budgets WHERE month = ?", (month,)
    ).fetchall()
    conn.close()
    budgets = {row["category"]: row["amount"] for row in rows}

    def build(category: str, budget: float, spent: float = None) -> Dict:
        if spent is None:
            spent = spending.get(category, 0.0)
        ratio = spent / budget if budget > 0 else 0.0
        return {
            "category": category,
            "budget": budget,
            "spent": round(spent, 2),
            "remain": round(budget - spent, 2),
            "ratio": round(ratio, 2),
            "status": _judge_status(ratio),
        }

    # 总计的"已花"= 各类支出之和（__TOTAL__ 只是预算占位，不是真实消费类别）
    total = build(TOTAL_KEY, budgets.get(TOTAL_KEY, 0.0), sum(spending.values()))
    total["category"] = "总计"
    categories = [build(c, budgets[c]) for c in BUDGET_CATEGORIES if c in budgets]
    return {"month": month, "total": total, "categories": categories}


def check_alerts(month: str) -> List[str]:
    """按状态生成超支/接近预算的提醒文案"""
    status = get_budget_status(month)
    alerts = []
    total = status["total"]
    if total["status"] == "over":
        alerts.append(f"{month} 总预算已超支 {abs(total['remain']):.2f} 元")
    elif total["status"] == "warning":
        alerts.append(f"{month} 总预算已用掉 {int(total['ratio'] * 100)}%，接近上限")
    for c in status["categories"]:
        if c["status"] == "over" and c["remain"] < 0:
            alerts.append(f"{c['category']} 已超支 {abs(c['remain']):.2f} 元")
        elif c["status"] == "over":            # 刚好 100%，没超但已用满
            alerts.append(f"{c['category']} 预算已用满")
        elif c["status"] == "warning":
            alerts.append(f"{c['category']} 已用掉 {int(c['ratio'] * 100)}% 预算，接近上限")
    return alerts


def get_report(month: str) -> Dict:
    """打包成统一字典交给业务层/前端：{month, total, categories, alerts}"""
    report = get_budget_status(month)
    report["alerts"] = check_alerts(month)
    return report

# ==============================================================


# 导入模块时自动初始化数据库
init_db()
