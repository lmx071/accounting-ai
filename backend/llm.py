import socket
_orig = socket.getaddrinfo
def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _orig(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = ipv4_only
import os
import json
import base64
import requests
import dashscope
from dotenv import load_dotenv
from datetime import datetime
from typing import Dict, Optional, List

# 加载环境变量
load_dotenv()

dashscope.api_key = os.getenv("DASHSCOPE_API_KEY")
DEFAULT_PROVIDER = os.getenv("DEFAULT_LLM_PROVIDER", "qwen")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# ===================== 全局统一规则（解析+入库共用）=====================
# 分类白名单，所有场景只能用这8类
CATEGORY_WHITELIST = ["餐饮", "交通", "购物", "娱乐", "居住", "医疗", "教育", "其他"]

# 统一分类映射规则，文字解析、图片解析、查询解析共用一套
CATEGORY_MAP_RULE = """
【餐饮】：吃饭、早餐、午餐、晚餐、夜宵、奶茶、咖啡、饮料、水果、零食、烧烤、火锅、外卖、餐厅、小吃、甜品
【交通】：高铁、火车、动车、地铁、公交、打车、网约车、出租车、共享单车、加油、停车费、过路费、汽车维修、车票、出行
【购物】：衣服、鞋子、日用品、超市、网购、饰品、数码、化妆品、文具
【娱乐】：电影、KTV、演唱会、剧本杀、游乐园、游戏充值、短视频会员、动漫、桌游
【居住】：房租、物业费、水费、电费、燃气费、宽带、家政、家具
【医疗】：买药、体检、挂号、看病、疫苗、牙科、核酸、医院
【教育】：学费、书本、网课、培训班、资料、文具、考试报名费
【其他】：无法归入上面任意一类的消费
"""


# ---------------------- 工具函数 ----------------------
def image_bytes_to_base64(img_bytes: bytes) -> str:
    """二进制图片转base64字符串（不再读取本地文件）"""
    return base64.b64encode(img_bytes).decode("utf-8")


def _safe_json_parse(text: str) -> Optional[Dict]:
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"【JSON解析失败】原始内容：{text}")
        return None


# ---------------------- 千问云端实现 ----------------------
def _qwen_text_chat(prompt: str, system_prompt: str) -> str:
    response = dashscope.Generation.call(
        model=os.getenv("QWEN_TEXT_MODEL", "qwen-plus"),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        result_format="message"
    )
    return response.output.choices[0].message.content


def _qwen_vl_chat(img_bytes: bytes, prompt: str) -> str:
    image_base64 = image_bytes_to_base64(img_bytes)
    image_url = f"data:image/jpeg;base64,{image_base64}"

    response = dashscope.MultiModalConversation.call(
        model=os.getenv("QWEN_VL_MODEL", "qwen3-vl-plus"),
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": image_url},
                    {"text": prompt}
                ]
            }
        ]
    )
    # 兼容两种返回格式：列表 / 纯文本
    raw_content = response.output.choices[0].message.content
    if isinstance(raw_content, list):
        return raw_content[0].get("text", "")
    else:
        return raw_content


# ---------------------- Ollama 本地实现 ----------------------
def _ollama_text_chat(prompt: str, system_prompt: str) -> str:
    model = os.getenv("OLLAMA_TEXT_MODEL", "qwen2.5:7b")
    url = f"{OLLAMA_BASE_URL}/api/chat"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "stream": False
    }

    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def _ollama_vl_chat(img_bytes: bytes, prompt: str) -> str:
    model = os.getenv("OLLAMA_VL_MODEL", "qwen2.5vl:7b")
    url = f"{OLLAMA_BASE_URL}/api/generate"

    image_base64 = image_bytes_to_base64(img_bytes)
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [image_base64],
        "stream": False
    }

    resp = requests.post(url, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()["response"]



# ---------------------- 统一对外接口 ----------------------
def parse_account_record(user_input: str) -> Optional[List[Dict]]:
    """自然语言记账解析 → 结构化账单数组（支持单笔/多笔消费，仅解析不入库）"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system_prompt = f"""
你是专业记账助手，将用户的自然语言描述解析为结构化账单数组。
用户可能说一笔消费，也可能同时说多笔消费，请全部解析为JSON数组格式。

分类映射规则（严格遵守）：
{CATEGORY_MAP_RULE}
分类只能从以下列表选择：餐饮、交通、购物、娱乐、居住、医疗、教育、其他

每条账单固定字段：
- amount: 数字，消费金额（元）
- category: 字符串，分类名称
- merchant: 字符串，商户名称，没有则为空字符串
- trade_time: 字符串，格式YYYY-MM-DD HH:MM:SS，没说明则用当前时间
- remark: 字符串，备注说明，没有则为空字符串

强制规则：
1. 用户没有明确说出时间，trade_time 默认使用【{now_str}】
2. 只返回纯JSON数组，不要解释、不要markdown、不要多余文字
3. 即使只有一笔消费，也必须包裹在数组里

示例（单笔）：[{{"amount":15.0,"category":"餐饮","merchant":"","trade_time":"{now_str}","remark":"奶茶"}}]
示例（多笔）：[{{"amount":8,"category":"餐饮","trade_time":"{now_str}","remark":"早餐"}},{{"amount":15,"category":"交通","trade_time":"{now_str}","remark":"打车"}}]
"""
    try:
        if DEFAULT_PROVIDER == "qwen":
            result = _qwen_text_chat(user_input, system_prompt)
        else:
            result = _ollama_text_chat(user_input, system_prompt)
        
        parsed = _safe_json_parse(result)
        if not parsed:
            return None
        # 兼容：模型偶尔返回单对象，自动包成数组
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return parsed
        return None
    except Exception as e:
        print(f"记账解析失败：{e}")
        if DEFAULT_PROVIDER == "qwen":
            print("尝试降级到本地Ollama...")
            try:
                result = _ollama_text_chat(user_input, system_prompt)
                parsed = _safe_json_parse(result)
                if isinstance(parsed, dict):
                    return [parsed]
                if isinstance(parsed, list):
                    return parsed
                return None
            except Exception as e2:
                print(f"Ollama降级也失败：{e2}")
        return None


def parse_query(user_question: str) -> Optional[Dict]:
    """自然语言查账解析 → 查询参数JSON"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    system_prompt = f"""
你是专业查账助手，解析用户的自然语言问题，输出查询参数JSON。
严格输出纯JSON，不要多余解释、不要markdown、不要多余文字。

固定字段：
- time_start: 字符串，开始时间，格式YYYY-MM-DD HH:MM:SS，不限时间则输出真正的null
- time_end: 字符串，结束时间，格式YYYY-MM-DD HH:MM:SS，不限时间则输出真正的null
- category: 字符串，查询类别，全量查询则输出真正的null
- query_type: 字符串，只能是：sum, category_rank, detail
- answer_template: 字符串，回答模板，用{{result}}占位金额，必须是完整通顺的自然语言，禁止只返回纯数字


强制规则：
1. 当前日期：{today_str}
2. 用户说【今天】：time_start="{today_str} 00:00:00"，time_end="{today_str} 23:59:59"
3. 用户没有明确指定时间范围，time_start和time_end必须输出真正的null，绝对不能编造日期，也不能输出字符串"null"
4. 分类严格按照映射规则匹配，不要自创分类，匹配不到就输出null
5. 回答模板必须是完整句子，例如「今天餐饮类消费总共{{result}}元」「本月交通花费合计{{result}}元」，禁止只输出数字加单位

分类映射规则（严格遵守）：
{CATEGORY_MAP_RULE}
"""

    def _clean_null(parsed_dict: Dict) -> Dict:
        """统一清洗空值：把字符串"null"、空字符串都转成真正的None"""
        for key in ["time_start", "time_end", "category"]:
            val = parsed_dict.get(key)
            if isinstance(val, str) and val.strip().lower() in ["null", "none", ""]:
                parsed_dict[key] = None
        return parsed_dict

    try:
        if DEFAULT_PROVIDER == "qwen":
            result = _qwen_text_chat(user_question, system_prompt)
        else:
            result = _ollama_text_chat(user_question, system_prompt)
        
        parsed = _safe_json_parse(result)
        if not parsed:
            return None
        return _clean_null(parsed)
    except Exception as e:
        print(f"查账解析失败：{e}")
        if DEFAULT_PROVIDER == "qwen":
            print("尝试降级到本地Ollama...")
            try:
                result = _ollama_text_chat(user_question, system_prompt)
                parsed = _safe_json_parse(result)
                if not parsed:
                    return None
                return _clean_null(parsed)
            except Exception as e2:
                print(f"Ollama降级也失败：{e2}")
        return None


def parse_receipt(img_bytes: bytes) -> Optional[Dict]:
    """小票OCR识别（接收图片二进制bytes）→ 结构化账单JSON（单笔，仅解析不入库）"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = f"""
识别这张消费小票/支付截图，提取关键信息，严格输出JSON对象：
- amount: 数字，总消费金额（元），提取实付金额，不要原价
- category: 字符串，消费类别，只能选：餐饮、交通、购物、娱乐、居住、医疗、教育、其他
- merchant: 字符串，商户名称，没有则为空字符串
- trade_time: 字符串，交易时间，格式YYYY-MM-DD HH:MM:SS，图片没有日期则使用当前日期
- remark: 字符串，其他备注，没有则为空字符串

强制规则：
图片没有时间的话，trade_time 默认使用【{now_str}】

分类映射规则（严格遵守）：
{CATEGORY_MAP_RULE}

禁止输出任何解释文字，禁止markdown，只返回纯JSON对象！
"""
    try:
        if DEFAULT_PROVIDER == "qwen":
            result = _qwen_vl_chat(img_bytes, prompt)
        else:
            result = _ollama_vl_chat(img_bytes, prompt)
        print(f"【VL模型原始输出】：{result}")
        return _safe_json_parse(result)
    except Exception as e:
        print(f"OCR识别失败：{e}")
        if DEFAULT_PROVIDER == "qwen":
            print("尝试降级到本地Ollama...")
            try:
                result = _ollama_vl_chat(img_bytes, prompt)
                print(f"【Ollama VL原始输出】：{result}")
                return _safe_json_parse(result)
            except Exception as e2:
                print(f"Ollama VL降级失败：{e2}")
        return None
