import sqlite3
from app.db.database import engine, Base
from app.models import Task          # 确保模型注册

DB_PATH = "app/db/database.db"      # 请根据实际路径修改

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# 检查 tasks 表是否存在
c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
table_exists = c.fetchone() is not None

if table_exists:
    print("✅ tasks 表已存在，检查并添加缺失字段...")
    columns = [
        ("started_at", "TIMESTAMP"),
        ("completed_at", "TIMESTAMP"),
        ("last_heartbeat", "TIMESTAMP"),
        ("celery_task_id", "TEXT")
    ]
    for col_name, col_type in columns:
        try:
            c.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}")
            print(f"   + 已添加 {col_name}")
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                print(f"   - {col_name} 已存在，跳过")
            else:
                raise
    conn.commit()
    print("✅ 字段补齐完成")
else:
    print("⚠️  tasks 表不存在，正在创建所有表...")
    Base.metadata.create_all(engine)
    print("✅ 所有表创建完毕（包含 tasks 等）")

conn.close()