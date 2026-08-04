# main.py（正式运行版，已修正所有导入）
import warnings
import logging
import sys
import traceback

# ---- 抑制弃用警告 ----
warnings.filterwarnings("ignore", message=".*langchain.*deprecated.*")
warnings.filterwarnings("ignore", message=".*langchain-community.*")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI

# ========== 导入 API 路由 ==========
# 这里如果失败会直接打印详细错误并退出
try:
    from app.api.v1_router import router as v1_router
except Exception as e:
    print("\n❌ 导入 v1_router 失败，错误详情如下：\n")
    traceback.print_exc()
    print("\n⚠️ 请确保 app/api/v1_router.py 存在且正确定义了 router 对象\n")
    sys.exit(1)

app = FastAPI(title="特种设备缺陷解析与FMEA评估 v4.3")
app.include_router(v1_router)

if __name__ == "__main__":
    import uvicorn

    # 尝试预加载法规（非致命，失败只记录警告）
    try:
        from app.core.regulation_loader import load_regulation_folder   # 修正后的路径
        load_regulation_folder()
    except Exception as e:
        logging.warning(f"法规预加载失败（不影响核心功能）: {e}")

    # 启动服务
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")