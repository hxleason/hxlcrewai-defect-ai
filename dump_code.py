"""
把项目所有 .py 文件的内容导出到一个 txt 文件里。
用法：python dump_code.py
"""
import os

# ========== 配置区 ==========
PROJECT_ROOT = r"C:\Users\32175\Desktop\multiple ai agent"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "project_dump.txt")

EXTENSIONS = [".py"]

EXCLUDE_DIRS = {
    "__pycache__", ".git", ".venv", "venv", "env",
    "model", ".cache", ".idea", ".vscode",
}
EXCLUDE_FILES = {"dump_code.py"}
# ============================

def should_skip_dir(d):
    return d.lower() in EXCLUDE_DIRS

def collect_files(root):
    collected = []
    for dp, dnames, fnames in os.walk(root):
        dnames[:] = [d for d in dnames if not should_skip_dir(d)]
        for fn in fnames:
            if fn in EXCLUDE_FILES:
                continue
            if os.path.splitext(fn)[1].lower() in EXTENSIONS:
                collected.append(os.path.join(dp, fn))
    collected.sort()
    return collected

def main():
    print(f"项目目录: {PROJECT_ROOT}")
    if not os.path.isdir(PROJECT_ROOT):
        print("❌ 目录不存在！请检查 PROJECT_ROOT 路径！")
        return

    files = collect_files(PROJECT_ROOT)
    print(f"找到 {len(files)} 个 .py 文件\n")

    count = 0
    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        out.write("=" * 70 + "\n")
        out.write(f"  项目代码导出 — 共 {len(files)} 个文件\n")
        out.write("=" * 70 + "\n\n")

        for fp in files:
            rel = os.path.relpath(fp, PROJECT_ROOT)
            count += 1
            print(f"  ✅ {rel}")  # 屏幕上显示进度

            out.write(f"\n{'=' * 70}\n")
            out.write(f"  📄 {rel}\n")
            out.write(f"{'=' * 70}\n\n")

            try:
                content = open(fp, "r", encoding="utf-8").read()
            except UnicodeDecodeError:
                content = open(fp, "r", encoding="gbk").read()
            except Exception as e:
                content = f"# 读取失败: {e}"

            out.write(content)
            if not content.endswith("\n"):
                out.write("\n")

    size = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"\n🎉 完成！导出了 {count} 个文件")
    print(f"📦 文件大小: {size:.1f} KB")
    print(f"📍 位置: {OUTPUT_FILE}")
    if size < 2:
        print("\n⚠️ 文件太小，可能路径不对！")

if __name__ == "__main__":
    main()