from app.core.config import settings
from app.db.database import create_tables, SessionLocal
from app.models.db_models import FMEAProject

print("数据库地址:", settings.DATABASE_URL)

# 建表
create_tables()
print("✅ 表已创建（如有）")

# 测试写入
db = SessionLocal()
try:
    p = FMEAProject(name="测试项目", description="验证")
    db.add(p)
    db.commit()
    print("✅ 测试数据写入成功, id:", p.id)
except Exception as e:
    db.rollback()
    print("❌ 写入失败:", e)
finally:
    db.close()