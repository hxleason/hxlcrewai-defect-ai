# test_defect_grader.py
from defect_grader import grade_defect

# 裂纹深度2mm，壁厚20mm，长度15mm
assert grade_defect('裂纹', {'depth':2, 'length':15}, 20) == 2  # 0.1，无长度修正 -> 2
# 深度8mm，壁厚20mm
assert grade_defect('裂纹', {'depth':8, 'length':20}, 20) == 4  # 0.4 -> 4
# 深度10mm，壁厚20mm，长度60mm
assert grade_defect('裂纹', {'depth':10, 'length':60}, 20) == 5  # 0.5 -> 5

# 气孔
assert grade_defect('气孔', {'diameter':4, 'density':'密集'}, 20) == 4
assert grade_defect('气孔', {'diameter':2, 'density':'分散'}, 20) == 2

print("所有测试通过 ✅")