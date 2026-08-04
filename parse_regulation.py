import os
import re

REGULATION_DB = {}   # 全局法规库

def parse_regulation_file(filepath: str) -> dict:
    """
    解析一个法规文本文件，返回 {(defect_type, level): {refs, measures, advice}}
    """
    records = {}
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('|')
            if len(parts) < 5:
                continue   # 格式不对的行跳过
            defect_type = parts[0].strip()
            try:
                level = int(parts[1].strip())
            except ValueError:
                continue
            refs_raw = parts[2].strip()
            refs = [r.strip() for r in refs_raw.split(';') if r.strip()]
            measures = parts[3].strip()
            advice = parts[4].strip()

            key = (defect_type, level)
            # 如果同一类型等级有多行，则合并 refs（去重）
            if key in records:
                existing = records[key]
                existing_refs = set(existing["refs"])
                existing_refs.update(refs)
                existing["refs"] = sorted(existing_refs)
                # 措施和意见取最后一条（或可自行决定）
                existing["measures"] = measures
                existing["advice"] = advice
            else:
                records[key] = {
                    "refs": refs,
                    "measures": measures,
                    "advice": advice
                }
    return records

def load_regulation_from_folder(folder_path: str = "regulation"):
    """
    遍历文件夹下所有 .txt 文件，合并到全局 REGULATION_DB
    """
    global REGULATION_DB
    if not os.path.isdir(folder_path):
        print(f"警告：法规文件夹 '{folder_path}' 不存在")
        return

    for filename in os.listdir(folder_path):
        if filename.endswith(".txt"):
            filepath = os.path.join(folder_path, filename)
            print(f"正在加载法规文件：{filepath}")
            new_records = parse_regulation_file(filepath)
            # 合并到全局库（如果有重复键，会完全覆盖）
            REGULATION_DB.update(new_records)
    print(f"法规库加载完成，共 {len(REGULATION_DB)} 条记录。")

# 可以选择自动加载
# load_regulation_from_folder()