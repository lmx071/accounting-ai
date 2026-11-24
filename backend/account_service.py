import json
import re
from datetime import datetime
from typing import Dict, Tuple, List
from backend.llm import parse_account_record, parse_query, parse_receipt, CATEGORY_WHITELIST
from backend.db import add_record, get_records_by_time, get_category_sum


# ===================== 第一步：纯解析（不入库，用于用户确认/修改） =====================
def parse_text_record(text: str) -> Tuple[bool, Dict]:
    """文字记账预解析：仅调用大模型解析，不写入数据库，支持单笔/多笔"""
    records = parse_account_record(text)
    if not records or not isinstance(records, list):
        return False, {"error": "无法识别记账内容，请换更清晰的描述"}
    return True, {"records": records}


def parse_image_record(img_bytes: bytes) -> Tuple[bool, Dict]:
    """图片记账预解析：仅调用大模型识别，不写入数据库"""
    record = parse_receipt(img_bytes)
    if not record or "amount" not in record:
        return False, {"error": "无法识别图片内容，请换更清晰的小票"}
    return True, {"record": record}


# ===================== 第二步：确认入库（接收用户修改后的数据，支持修正后提交） =====================
def commit_record(record_data: Dict) -> Tuple[bool, Dict]:
    """校验单条账单并正式写入数据库，支持用户修改金额、分类、备注等字段"""
    # 1. 校验金额合法性
    amount = record_data.get("amount")
    if not isinstance(amount, (int, float)) or amount <= 0:
        return False, {"error": "金额无效，必须为大于0的数字"}

    # 2. 校验分类白名单（防止用户修改成非法分类）
    category = str(record_data.get("category", "")).strip()
    if category not in CATEGORY_WHITELIST:
        return False, {"error": f"分类无效，仅支持：{','.join(CATEGORY_WHITELIST)}"}

    # 3. 处理时间：为空则填充当前时间
    trade_time = record_data.get("trade_time", "").strip()
    if not trade_time:
        trade_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 4. 提取其余字段
    merchant = record_data.get("merchant", "").strip()
    remark = record_data.get("remark", "").strip()

    # 5. 写入数据库
    try:
        record_id = add_record(
            amount=amount,
            category=category,
            merchant=merchant,
            trade_time=trade_time,
            remark=remark
        )
        return True, {
            "id": record_id,
            "amount": amount,
            "category": category,
            "merchant": merchant,
            "trade_time": trade_time,
            "remark": remark
        }
    except Exception as e:
        return False, {"error": f"入库失败：{str(e)}"}


# ===================== 快捷入库（原有能力保留，解析后自动确认入库） =====================
def add_record_by_text(text: str) -> Tuple[bool, Dict]:
    """一句话快捷记账：解析 → 自动校验 → 批量入库，支持一次性多笔消费"""
    ok, data = parse_text_record(text)
    if not ok:
        return False, data

    records = data["records"]
    success_list = []
    fail_list = []
    time_keywords = ["昨天", "前天", "上周", "上个月", "号", "日", "月"]
    has_time_info = any(k in text for k in time_keywords)

    for item in records:
        # 用户原文没有时间关键词，强制覆盖为当前时间
        if not has_time_info:
            item["trade_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        ok_item, res = commit_record(item)
        if ok_item:
            success_list.append(res)
        else:
            fail_list.append({"item": item, "reason": res["error"]})

    return True, {
        "total": len(records),
        "success_count": len(success_list),
        "fail_count": len(fail_list),
        "success_records": success_list,
        "fail_items": fail_list
    }


def add_record_by_image(img_bytes: bytes) -> Tuple[bool, Dict]:
    """图片快捷记账：识别 → 自动校验 → 入库"""
    ok, data = parse_image_record(img_bytes)
    if not ok:
        return False, data

    record = data["record"]
    # 时间兜底：识别结果无有效时间则填充当前时间
    time_keywords = ["昨天", "前天", "上周", "上个月", "号", "日", "月", "年", "点", "分"]
    record_str = json.dumps(record, ensure_ascii=False)
    has_time_info = any(k in record_str for k in time_keywords)
    if not has_time_info or not record.get("trade_time"):
        record["trade_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ok_commit, res = commit_record(record)
    return ok_commit, res


# 兼容旧函数名（保留原有代码引用）
def confirm_receipt_record(record: Dict) -> int:
    """用户确认OCR结果后正式入库，兼容旧调用"""
    ok, res = commit_record(record)
    return res.get("id", 0) if ok else 0


# ===================== 自然语言查账 =====================
def _normalize_category_param(category):
    """统一分类参数格式：字符串/列表 → 清洗后的列表；空则返回None"""
    if category is None:
        return None
    if isinstance(category, str):
        category = category.strip()
        if not category:
            return None
        category = re.split(r"[、,，+ 和与及 ]", category)
    category = [c.strip() for c in category if c and str(c).strip()]
    return category if len(category) > 0 else None


# 分类别名映射：标准分类 → 常用口语说法
CATEGORY_ALIAS = {
    "餐饮": ["吃饭", "餐饮", "外卖", "聚餐", "奶茶", "饭钱", "伙食", "早餐", "午餐", "晚餐"],
    "交通": ["交通", "打车", "地铁", "公交", "车费", "油费", "停车", "出行"],
    "购物": ["购物", "买东西", "超市", "网购", "淘宝", "衣服", "日用品"],
    "娱乐": ["娱乐", "电影", "唱歌", "游戏", "旅游", "玩", "剧本杀"],
    "居住": ["居住", "房租", "水电", "物业费", "住宿"],
    "医疗": ["医疗", "看病", "买药", "医院", "药店", "体检"],
    "教育": ["教育", "上课", "买书", "学费", "培训"],
    "其他": ["其他", "别的", "杂费", "其他支出"]
}

def _extract_categories_by_keyword(text: str) -> List[str]:
    """关键词兜底：支持别名匹配，自动映射到标准分类"""
    found = set()
    for standard_cat, aliases in CATEGORY_ALIAS.items():
        for alias in aliases:
            if alias in text:
                found.add(standard_cat)
                break
    return list(found) if found else None




def query_by_natural_language(question: str) -> Tuple[bool, str]:
    """自然语言查账：解析问题 → 查询数据库 → 生成回答"""
    query_params = parse_query(question)
    if not query_params:
        return False, "抱歉，我没理解你的问题，可以换个说法吗？"

    time_start = query_params.get("time_start")
    time_end = query_params.get("time_end")
    category = query_params.get("category")
    query_type = query_params.get("query_type")
    template = query_params.get("answer_template", "结果为{result}")

    # ========== 核心修复：大模型解析为空时，关键词兜底提取分类 ==========
    if not category:
        category = _extract_categories_by_keyword(question)
    
    # 统一分类格式
    category = _normalize_category_param(category)

    try:
        if query_type == "sum":
            answer = generate_query_text(time_start, time_end, category)

        elif query_type == "category_rank":
            stats = get_category_sum(time_start, time_end, category)
            if not stats:
                answer = "这段时间没有消费记录哦"
            else:
                top = stats[0]
                answer = template.replace("{result}", f"{top['category']}（{top['total_amount']:.2f}元）")

        elif query_type == "detail":
            records = get_records_by_time(time_start, time_end, category)
            if not records:
                answer = "没有找到符合条件的账单"
            else:
                details = "\n".join([
                    f"{r['trade_time'][:10]} {r['category']} {r['amount']}元 {r.get('merchant','')}"
                    for r in records[:5]
                ])
                answer = f"为你找到以下账单：\n{details}"

        else:
            answer = "不支持的查询类型"

        return True, answer

    except Exception as e:
        return False, f"查询出错：{str(e)}"


# ===================== 查询结果组装 =====================
def build_query_summary(start_time: str = None, end_time: str = None,
                        categories: List[str] = None) -> Dict:
    """组装查询汇总结果：总金额 + 分类明细"""
    category_list = get_category_sum(start_time, end_time, categories)

    total_amount = sum(item["total_amount"] for item in category_list)
    total_count = sum(item["count"] for item in category_list)

    return {
        "total_amount": round(total_amount, 2),
        "total_count": total_count,
        "categories": category_list
    }


def generate_query_text(start_time: str = None, end_time: str = None,
                        categories: List[str] = None) -> str:
    """生成自然语言回答文案，直接返回给用户"""
    data = build_query_summary(start_time, end_time, categories)

    # 时间描述
    if start_time and end_time:
        time_desc = f"{start_time[:7]} 月"
    elif start_time:
        time_desc = f"{start_time} 以来"
    elif end_time:
        time_desc = f"截至 {end_time}"
    else:
        time_desc = "全部"

    if data["total_count"] == 0:
        return f"{time_desc}暂无消费记录。"

    # 拼接分类明细
    lines = [f"{time_desc}总消费 {data['total_amount']:.2f} 元，共 {data['total_count']} 笔，各分类花费如下："]
    for item in data["categories"]:
        lines.append(f"- {item['category']}：{item['total_amount']:.2f} 元（{item['count']}笔）")

    return "\n".join(lines)


# 测试代码
if __name__ == "__main__":
    print("===== 解析查询模块测试 =====")

    # 测试1：单笔自然语言记账
    print("\n1. 单笔记账测试：")
    success, res = add_record_by_text("今天中午吃麦当劳花了32块钱")
    if success:
        print(f"✅ 记账完成，成功{res['success_count']}条，失败{res['fail_count']}条")
        print(f"   成功记录：{res['success_records']}")
    else:
        print(f"❌ 记账失败：{res.get('error')}")

    # 测试2：多笔自然语言记账
    print("\n2. 多笔记账测试：")
    success, res = add_record_by_text("早餐8元，上班打车18元，下午奶茶15元")
    if success:
        print(f"✅ 记账完成，共{res['total']}条，成功{res['success_count']}条，失败{res['fail_count']}条")
    else:
        print(f"❌ 记账失败：{res.get('error')}")

    # 测试3：自然语言查账
    print("\n3. 自然语言查账测试：")
    ok, ans = query_by_natural_language("今天餐饮花了多少钱？")
    print(f"回答：{ans}")
