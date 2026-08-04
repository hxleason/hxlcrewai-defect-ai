"""
铸造缺陷诊断器 测试用例
运行方式：python test_defect_diagnoser.py
"""
import unittest
import sys
import os

# 确保能从当前目录导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ✅ 关键修正：导入模块本身（用来取文件路径）+ 导入函数
import defect_diagnoser
from defect_diagnoser import diagnose_defect


class TestDiagnoseDefect(unittest.TestCase):
    """diagnose_defect 函数测试"""

    def test_basic_diagnosis(self):
        """基本诊断：气孔 2级，应返回通用原因"""
        reasons = diagnose_defect("气孔", "2级")
        self.assertIsInstance(reasons, list)
        self.assertGreater(len(reasons), 0)
        self.assertTrue(any("砂型或砂芯排气不良" in r for r in reasons))

    def test_specific_grade_rule(self):
        """等级特殊规则：裂纹 4级 应包含贯穿性裂纹"""
        reasons = diagnose_defect("裂纹", "4级")
        self.assertTrue(any("贯穿性裂纹" in r for r in reasons))

    def test_unknown_defect_type(self):
        """未知缺陷类型：应返回一条提示"""
        reasons = diagnose_defect("宇宙射线", "1级")
        self.assertEqual(len(reasons), 1)
        self.assertIn("未知缺陷类型", reasons[0])

    def test_quantity_rule(self):
        """动态规则：数量 >=10 时应提示"""
        reasons = diagnose_defect("气孔", "1级", quantity=12)
        self.assertTrue(any("数量较多" in r for r in reasons))

    def test_diameter_rule(self):
        """动态规则：直径 >5mm 时应提示"""
        reasons = diagnose_defect("夹杂", "2级", diameter_mm=8.0)
        self.assertTrue(any("尺寸较大" in r for r in reasons))

    def test_edge_no_grade_rule(self):
        """无对应等级规则时，仍应返回通用原因"""
        reasons = diagnose_defect("缩松", "9级")   # 字典里没有9级
        self.assertIsInstance(reasons, list)
        self.assertGreater(len(reasons), 0)
        self.assertTrue(any("冒口补缩不足" in r for r in reasons))


if __name__ == "__main__":
    # 打印实际使用的文件路径（证明导入正确）
    print("✅ 测试正在使用的 defect_diagnoser 文件是：")
    print("   ", os.path.abspath(defect_diagnoser.__file__))
    print("-" * 50)

    # 运行测试
    unittest.main(verbosity=2)