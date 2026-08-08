import os

# 要跳过的文件夹（这些不用发给 AI）
SKIP_DIRS = {
    "__pycache__", ".git", ".venv", ".cache",
    "node_modules", "__pycache__", "bge-small-zh-v1.5"
}

# 要收集的文件类型
INCLUDE_EXT = {".py", ".json", ".txt", ".md", ".env.example"}

# 输出文件
OUTPUT = "project_dump.txt"

# 项目根目录（脚本所在目录）
ROOT = os.path.dirname(os.path.abspath(__file__))

count = 0
with open(os.path.join(ROOT, OUTPUT), "w", encoding="utf-8") as out:
    out.write(f"# 项目代码导出\n")
    out.write(f"# 根目录: {ROOT}\n")
    out.write(f"# 文件总数: 待统计\n\n")

    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 过滤掉不需要的目录
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in INCLUDE_EXT:
                continue

            fpath = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(fpath, ROOT)

            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
            except UnicodeDecodeError:
                # 二进制文件跳过
                continue

            # 写入分隔符 + 文件名 + 内容
            out.write(f"\n{'='*70}\n")
            out.write(f"📄 {rel_path}\n")
            out.write(f"{'='*70}\n\n")
            out.write(content)
            out.write("\n\n")

            count += 1
            print(f"  ✅ {rel_path}")

print(f"\n🎉 共导出 {count} 个文件 → {OUTPUT}")