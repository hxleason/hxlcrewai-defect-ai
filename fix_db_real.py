from sqlalchemy import text
from app.db.database import engine

with engine.connect() as conn:
    # 检查 tasks 表是否存在
    res = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"))
    table_exists = res.fetchone() is not None

    if not table_exists:
        print("⚠️  tasks 表不存在，正在创建所有表...")
        # 由于没有表，我们需要用 create_all，但要确保 engine 正确
        # 注意：下面这行需要 SQLAlchemy 版本支持，如果报错可改用手动执行 create_all
        from app.models import Task
        from app.db.database import Base
        Base.metadata.create_all(engine)
        print("✅ 所有表已创建")
    else:
        print("✅ tasks 表存在，检查并添加缺失字段...")

    # 无论表是否存在，都尝试补字段（如果表刚创建，字段已存在会忽略）
    missing_columns = [
        ("started_at", "TIMESTAMP"),
        ("completed_at", "TIMESTAMP"),
        ("last_heartbeat", "TIMESTAMP"),
        ("celery_task_id", "TEXT"),
    ]
    for col_name, col_type in missing_columns:
        try:
            conn.execute(text(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}"))
            print(f"   + 添加 {col_name}")
        except Exception as e:
            if "duplicate column" in str(e).lower():
                print(f"   - {col_name} 已存在，跳过")
            else:
                print(f"   ⚠️ 添加 {col_name} 失败：{e}")
    conn.commit()
    print("✅ 数据库修复完成")