# app/tasks/test_env.py
import os
from app.celery_app import celery_app

@celery_app.task
def test_env_vars():
    return {
        "STANDARDS_FOLDER": os.getenv("STANDARDS_FOLDER"),
        "CHROMA_PERSIST_DIR": os.getenv("CHROMA_PERSIST_DIR")
    }