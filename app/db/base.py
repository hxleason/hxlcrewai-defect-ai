from sqlalchemy.orm import declarative_base

# 创建 SQLAlchemy 基类（所有模型需继承此类）
Base = declarative_base()

# ============================================================
# 🔧 关键：导入所有模型类，确保 Base.metadata 能够自动发现它们
# 这样任何地方调用 Base.metadata.create_all() 都会创建全部表
# 导入必须放在 Base 定义之后，避免循环依赖
# ============================================================
# 未来如果新增其他模型（如 AuditLog, User 等）也在此处添加导入
# from app.models import AuditLog