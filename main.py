from fastapi import FastAPI, UploadFile, File, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from backend.account_service import (
    add_record_by_text,
    query_by_natural_language,
    add_record_by_image,
    parse_text_record,
    parse_image_record,
    commit_record
)
from backend.db import set_budget, get_report
from fastapi import Query

#增强兼容
import re
from datetime import datetime

def normalize_month(month: str) -> str:
    """兼容多种月份输入，统一转为 YYYY-MM 格式"""
    month = str(month).strip()
    
    # 标准格式：2026-11
    if re.match(r"^\d{4}-\d{2}$", month):
        return month
    
    # 6位纯数字：202611
    if re.match(r"^\d{6}$", month):
        return f"{month[:4]}-{month[4:]}"
    
    # 1-2位月份：11 / 3，自动补当前年份
    if re.match(r"^\d{1,2}$", month):
        current_year = datetime.now().year
        return f"{current_year}-{int(month):02d}"
    
    raise ValueError("月份格式应为 YYYY-MM，例如：2026-11")

# 1. 创建 FastAPI 应用实例
app = FastAPI(title="AI 记账系统后端", version="1.0")

# 2. 配置跨域，方便前端调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. 健康检查接口
@app.get("/health", summary="服务健康检查")
def health_check():
    return {"status": "ok", "message": "AI记账后端服务运行正常"}

# 4. 自然语言记账接口（快捷模式：解析后自动入库）
@app.post("/record/text", summary="一句话快捷记账（自动入库）")
def text_record(text: str):
    success, result = add_record_by_text(text)
    if not success:
        raise HTTPException(status_code=400, detail=result.get("error", "记账失败"))
    return {"success": True, "data": result}

# 5. 自然语言查账接口
@app.get("/query", summary="自然语言查账")
def natural_query(question: str):
    success, answer = query_by_natural_language(question)
    if not success:
        raise HTTPException(status_code=400, detail="查账解析失败")
    return {"success": True, "answer": answer}

# 6. 小票OCR识别接口（快捷模式：识别后自动入库）
@app.post("/record/image", summary="小票/截图快捷记账（自动入库）")
async def image_record(file: UploadFile = File(...)):
    img_bytes = await file.read()
    success, result = add_record_by_image(img_bytes)
    if not success:
        raise HTTPException(status_code=400, detail=result.get("error", "识别失败"))
    return {"success": True, "data": result}

# ===================== 新增：两步流程接口（满足「确认/修改后入库」考核要求） =====================

# 7. 文字记账预解析（仅解析不入库，用于用户核对修改）
@app.post("/parse/text", summary="文字记账预解析（不入库，可修改后再确认）")
def parse_text(text: str):
    success, result = parse_text_record(text)
    if not success:
        raise HTTPException(status_code=400, detail=result.get("error", "解析失败"))
    return {"success": True, "data": result}

# 8. 图片记账预解析（仅识别不入库，用于用户核对修改）
@app.post("/parse/image", summary="图片记账预解析（不入库，可修改后再确认）")
async def parse_image(file: UploadFile = File(...)):
    img_bytes = await file.read()
    success, result = parse_image_record(img_bytes)
    if not success:
        raise HTTPException(status_code=400, detail=result.get("error", "解析失败"))
    return {"success": True, "data": result}

# 9. 确认账单入库（接收用户修改后的账单数据，正式写入数据库）
@app.post("/record/commit", summary="确认账单并入库（支持修改金额、分类、备注后提交）")
def commit_bill(
    amount: float = Body(..., description="消费金额，必须大于0"),
    category: str = Body(..., description="消费分类，必须在白名单内"),
    merchant: str = Body(default="", description="商户名称"),
    trade_time: str = Body(default="", description="消费时间，格式YYYY-MM-DD HH:MM:SS，留空则使用当前时间"),
    remark: str = Body(default="", description="备注说明")
):
    record_data = {
        "amount": amount,
        "category": category,
        "merchant": merchant,
        "trade_time": trade_time,
        "remark": remark
    }
    success, result = commit_record(record_data)
    if not success:
        raise HTTPException(status_code=400, detail=result.get("error", "入库失败"))
    return {"success": True, "data": result}


# ===================== 预算报表模块（预算负责人） =====================

@app.post("/budget/set", summary="设定某月某类预算")
def set_budget_api(
    month: str = Body(..., description="月份，格式 YYYY-MM，如 2026-11"),
    category: str = Body(..., description="类别，总预算传 __TOTAL__"),
    amount: float = Body(..., description="预算金额")
):
    """设定/更新预算，有则改无则增"""
    set_budget(month, category, amount)
    return {"success": True, "month": month, "category": category, "amount": amount}


@app.get("/budget/report", summary="获取某月预算报表")
def budget_report(month: str = Query(..., description="支持 YYYY-MM / 6位数字 / 纯月份，例如 2026-11、202611、11", example="2026-11")):
    """返回 {month, total, categories, alerts}，前端按此画图"""
    try:
        month = normalize_month(month)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "data": get_report(month)}