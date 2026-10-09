# -*- coding: utf-8 -*-
"""MacroBuddy · 营养目标推导内核（零依赖纯 Python）

职责边界（重要）
----------------
本模块只做"确定性数值计算"：不调用任何 LLM、不做任何自然语言理解。
用户档案 -> 每日宏量目标 -> 单餐目标，全部由纯函数推导。
LLM 只负责两件事：把用户口语转成 ``UserProfile``；把本模块的输出翻译成人话。

方法与出处
----------
- 基础代谢率（BMR）采用 Mifflin-St Jeor (1990) 公式。该式在非肥胖与超重人群中
  对静息代谢率的预测误差普遍小于 Harris-Benedict 修正式。
- 活动系数采用通行的 PAL（Physical Activity Level）分级。
- 目标热量调整采用保守区间：减脂 -20% TDEE，维持 0%，增肌 +10% TDEE。
- 蛋白质摄入参考 ACSM / AND 关于运动人群 1.4-2.0 g/kg 体重的共识区间。
- 脂肪下限取 0.6 g/kg 体重，用于保障必需脂肪酸与内分泌功能。
- 钠参考 WHO 成人每日低于 2000 mg 的膳食建议，按餐次摊分。

免责声明
--------
以上均为公开通用营养学区间，仅用于餐品搭配参考，不构成医疗或营养建议。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

# --------------------------------------------------------------------------- #
# 常量表
# --------------------------------------------------------------------------- #

#: 活动系数（PAL）
ACTIVITY_FACTORS: Dict[str, float] = {
    "sedentary": 1.2,   # 久坐，几乎无运动
    "light": 1.375,     # 每周 1-3 次轻度运动
    "moderate": 1.55,   # 每周 3-5 次中等强度运动
    "active": 1.725,    # 每周 6-7 次高强度运动
    "athlete": 1.9,     # 体力劳动或一日两练
}

#: 目标预设：热量系数 / 蛋白质 g·kg⁻¹ / 脂肪供能比
GOAL_PRESETS: Dict[str, Dict[str, float]] = {
    "cut": {"kcal_factor": 0.80, "protein_per_kg": 1.8, "fat_ratio": 0.25},
    "maintain": {"kcal_factor": 1.00, "protein_per_kg": 1.4, "fat_ratio": 0.28},
    "bulk": {"kcal_factor": 1.10, "protein_per_kg": 1.8, "fat_ratio": 0.25},
    "glucose_control": {"kcal_factor": 0.95, "protein_per_kg": 1.6, "fat_ratio": 0.32},
}

GOAL_LABELS: Dict[str, str] = {
    "cut": "减脂",
    "maintain": "维持",
    "bulk": "增肌",
    "glucose_control": "控糖",
}

KCAL_PER_G_PROTEIN = 4.0
KCAL_PER_G_CARB = 4.0
KCAL_PER_G_FAT = 9.0
FAT_FLOOR_PER_KG = 0.6
CARB_FLOOR_RATIO = 0.15
SODIUM_DAILY_CAP_MG = 2000.0


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class UserProfile:
    """用户档案。字段缺失时由调用方（LLM）向用户追问，不要臆造。"""

    sex: str = "unspecified"          # male / female / unspecified
    age: int = 30
    height_cm: float = 170.0
    weight_kg: float = 65.0
    activity: str = "light"           # ACTIVITY_FACTORS 的键
    goal: str = "maintain"            # GOAL_PRESETS 的键
    meals_per_day: int = 3
    sodium_daily_cap_mg: float = SODIUM_DAILY_CAP_MG

    def normalized(self) -> "UserProfile":
        activity = self.activity if self.activity in ACTIVITY_FACTORS else "light"
        goal = self.goal if self.goal in GOAL_PRESETS else "maintain"
        meals = max(1, min(6, int(self.meals_per_day)))
        return UserProfile(
            sex=self.sex if self.sex in ("male", "female") else "unspecified",
            age=max(14, min(100, int(self.age))),
            height_cm=max(120.0, min(220.0, float(self.height_cm))),
            weight_kg=max(35.0, min(200.0, float(self.weight_kg))),
            activity=activity,
            goal=goal,
            meals_per_day=meals,
            sodium_daily_cap_mg=max(800.0, float(self.sodium_daily_cap_mg)),
        )


@dataclass(frozen=True)
class DailyTarget:
    """每日宏量营养素目标。"""

    kcal: float
    protein_g: float
    fat_g: float
    carb_g: float
    sodium_mg: float
    bmr: float
    tdee: float
    goal: str
    activity: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MealTarget:
    """单餐目标及其可接受区间。"""

    kcal: float
    protein_g: float
    fat_g: float
    carb_g: float
    sodium_mg: float
    kcal_low: float
    kcal_high: float
    label: str = "本餐"

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 推导函数
# --------------------------------------------------------------------------- #

def mifflin_st_jeor(sex: str, weight_kg: float, height_cm: float, age: float) -> float:
    """Mifflin-St Jeor 基础代谢率，单位 kcal/day。"""
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age
    if sex == "male":
        return base + 5.0
    if sex == "female":
        return base - 161.0
    # 未指定性别：取两式常数中点，避免把结果强行归到某一性别
    return base - 78.0


def _split_macros(kcal: float, protein_g: float, fat_g: float, weight_kg: float):
    """按"蛋白质与脂肪优先、碳水填余量"的顺序分配三大营养素。

    当碳水被挤到低于总热量 15% 时，回撤脂肪至下限，把空间还给碳水，
    以保证方案在真实餐品上仍然可组合得出来。
    """
    fat_g = max(fat_g, FAT_FLOOR_PER_KG * weight_kg)
    protein_kcal = protein_g * KCAL_PER_G_PROTEIN
    fat_kcal = fat_g * KCAL_PER_G_FAT
    carb_kcal = kcal - protein_kcal - fat_kcal

    if carb_kcal < CARB_FLOOR_RATIO * kcal:
        carb_kcal = CARB_FLOOR_RATIO * kcal
        fat_kcal = max(FAT_FLOOR_PER_KG * weight_kg * KCAL_PER_G_FAT,
                       kcal - protein_kcal - carb_kcal)
        fat_g = fat_kcal / KCAL_PER_G_FAT
        carb_kcal = max(0.0, kcal - protein_kcal - fat_kcal)

    return fat_g, carb_kcal / KCAL_PER_G_CARB


def derive_daily_target(profile: UserProfile) -> DailyTarget:
    """由用户档案推导每日宏量目标。"""
    p = profile.normalized()
    bmr = mifflin_st_jeor(p.sex, p.weight_kg, p.height_cm, p.age)
    tdee = bmr * ACTIVITY_FACTORS[p.activity]

    preset = GOAL_PRESETS[p.goal]
    kcal = tdee * preset["kcal_factor"]
    protein_g = preset["protein_per_kg"] * p.weight_kg
    fat_g = kcal * preset["fat_ratio"] / KCAL_PER_G_FAT
    fat_g, carb_g = _split_macros(kcal, protein_g, fat_g, p.weight_kg)

    return DailyTarget(
        kcal=round(kcal, 1),
        protein_g=round(protein_g, 1),
        fat_g=round(fat_g, 1),
        carb_g=round(carb_g, 1),
        sodium_mg=p.sodium_daily_cap_mg,
        bmr=round(bmr, 1),
        tdee=round(tdee, 1),
        goal=p.goal,
        activity=p.activity,
    )


def allocate_meal(
    daily: DailyTarget,
    meals_per_day: int,
    meal_share: Optional[List[float]] = None,
    meal_index: int = 1,
    intake_so_far: Optional[dict] = None,
    label: str = "本餐",
) -> MealTarget:
    """把每日目标摊到单餐，并扣除当日已摄入部分。

    ``meal_share`` 为各餐占比（如 [0.25, 0.35, 0.4]），缺省则均分。
    ``intake_so_far`` 支持 kcal / protein_g / fat_g / carb_g / sodium_mg 五个键。
    """
    if meal_share and len(meal_share) == meals_per_day:
        share = float(meal_share[meal_index])
    else:
        share = 1.0 / meals_per_day

    tgt = {
        "kcal": daily.kcal * share,
        "protein_g": daily.protein_g * share,
        "fat_g": daily.fat_g * share,
        "carb_g": daily.carb_g * share,
        "sodium_mg": daily.sodium_mg * share,
    }

    if intake_so_far:
        for key in tgt:
            tgt[key] -= float(intake_so_far.get(key, 0.0) or 0.0)
        # 已摄入超标时留出最小可执行空间，避免目标变成负数而无法点单
        tgt["kcal"] = max(tgt["kcal"], daily.kcal * share * 0.35)
        for key in ("protein_g", "fat_g", "carb_g", "sodium_mg"):
            tgt[key] = max(tgt[key], 0.0)

    return MealTarget(
        kcal=round(tgt["kcal"], 1),
        protein_g=round(tgt["protein_g"], 1),
        fat_g=round(tgt["fat_g"], 1),
        carb_g=round(tgt["carb_g"], 1),
        sodium_mg=round(tgt["sodium_mg"], 1),
        kcal_low=round(tgt["kcal"] * 0.90, 1),
        kcal_high=round(tgt["kcal"] * 1.06, 1),
        label=label,
    )


def describe_daily_target(daily: DailyTarget) -> str:
    """生成人类可读的每日目标摘要，供 Skill 直接引用。"""
    return (
        f"BMR {daily.bmr:.0f} kcal｜TDEE {daily.tdee:.0f} kcal｜"
        f"目标({GOAL_LABELS.get(daily.goal, daily.goal)}) {daily.kcal:.0f} kcal｜"
        f"蛋白 {daily.protein_g:.0f} g｜脂肪 {daily.fat_g:.0f} g｜"
        f"碳水 {daily.carb_g:.0f} g｜钠上限 {daily.sodium_mg:.0f} mg"
    )
