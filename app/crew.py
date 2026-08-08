# crew.py（占位符文件）
from app.crews import (
    create_analysis_crew as _real_create,
    run_fmea_evaluation as _real_fmea,
    run_full_fmea_evaluation as _real_full
)

def create_analysis_crew(input_text: str) -> dict:
    return _real_create(input_text)

def run_fmea_evaluation(input_text: str) -> dict:
    return _real_fmea(input_text)

def run_full_fmea_evaluation(input_text: str) -> dict:
    return _real_full(input_text)