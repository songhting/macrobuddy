#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MacroBuddy · 自测用例（零依赖，直接 python scripts/selftest.py）

覆盖：BMR/TDEE 公式、宏量分配边界、单餐摊分与扣减、套餐价判定、
优惠券择优与门槛、多目标打分的各项惩罚、优化器可行集与三方案去重。
"""

from __future__ import annotations

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from macro_optimizer import (                                          # noqa: E402
    Coupon, MenuItem, ScoreWeights, _apply_set_price, _coupon_discount,
    best_coupon, build_combos, evaluate, load_menu, optimize,
)
from nutrition_target import (                                        # noqa: E402
    UserProfile, allocate_meal, derive_daily_target, mifflin_st_jeor,
)

SAMPLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "sample_menu.json",
)

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def approx(a, b, tol=0.05):
    return abs(a - b) <= tol


# --------------------------------------------------------------------------- #

@case
def test_bmr_mifflin():
    # 男性：10*72 + 6.25*175 - 5*30 + 5 = 1668.75
    assert approx(mifflin_st_jeor("male", 72, 175, 30), 1668.75), mifflin_st_jeor("male", 72, 175, 30)
    # 女性：同上 -161
    assert approx(mifflin_st_jeor("female", 72, 175, 30), 1502.75)
    # 未指定性别取两式中点
    assert approx(mifflin_st_jeor("unspecified", 72, 175, 30), (1668.75 + 1502.75) / 2)


@case
def test_daily_target_pipeline():
    p = UserProfile(sex="male", age=30, height_cm=175, weight_kg=72,
                    activity="light", goal="cut", meals_per_day=3)
    d = derive_daily_target(p)
    assert approx(d.bmr, 1668.75, 0.1)
    assert approx(d.tdee, 1668.75 * 1.375, 0.1)
    assert approx(d.kcal, 1668.75 * 1.375 * 0.80, 0.1)
    # 减脂蛋白质 1.8 g/kg
    assert approx(d.protein_g, 1.8 * 72, 0.1)
    # 三大营养素供能之和应接近总热量
    energy = d.protein_g * 4 + d.fat_g * 9 + d.carb_g * 4
    assert abs(energy - d.kcal) < d.kcal * 0.02, (energy, d.kcal)


@case
def test_profile_clamped():
    p = UserProfile(sex="alien", age=5, height_cm=90, weight_kg=5,
                    activity="nope", goal="nope", meals_per_day=99).normalized()
    assert p.sex == "unspecified" and p.age >= 14
    assert p.height_cm >= 120 and p.weight_kg >= 35
    assert p.activity == "light" and p.goal == "maintain"
    assert p.meals_per_day <= 6


@case
def test_meal_allocation_and_subtract():
    p = UserProfile(sex="male", age=30, height_cm=175, weight_kg=72, goal="cut")
    d = derive_daily_target(p)
    t = allocate_meal(d, 3, meal_index=1)
    assert approx(t.kcal, d.kcal / 3, 0.5)
    assert approx(t.kcal_low, t.kcal * 0.90, 0.1)

    t2 = allocate_meal(d, 3, meal_index=1, intake_so_far={"kcal": 300, "protein_g": 20})
    assert approx(t2.kcal, d.kcal / 3 - 300, 0.5)
    assert approx(t2.protein_g, d.protein_g / 3 - 20, 0.5)

    # 已摄入极端超标时保留最小可执行空间，不出现负数热量
    t3 = allocate_meal(d, 3, meal_index=1,
                       intake_so_far={"kcal": 99999, "protein_g": 9999})
    assert t3.kcal > 0 and t3.protein_g >= 0


@case
def test_set_price_logic():
    big = MenuItem("BIGMAC", "巨无霸", "main", 550, 25, 30, 43, 950, 26.5, set_price=38.0)
    fries = MenuItem("FRIESM", "薯条(中)", "side", 340, 4, 17, 44, 250, 12.0)
    coke = MenuItem("COKEM", "可乐(中)", "drink", 150, 0, 0, 38, 15, 9.0)

    subtotal, saving = _apply_set_price((big, fries, coke))
    assert approx(subtotal, 38.0), subtotal          # 47.5 走套餐价 38.0
    assert approx(saving, 9.5), saving

    # 缺饮品不构成套餐，走单品价
    subtotal2, saving2 = _apply_set_price((big, fries))
    assert approx(subtotal2, 38.5) and saving2 == 0.0

    # 无套餐价的主食不触发
    chs = MenuItem("CHSBG", "吉士汉堡", "main", 300, 15, 13, 30, 700, 14.0)
    subtotal3, saving3 = _apply_set_price((chs, fries, coke))
    assert approx(subtotal3, 35.0) and saving3 == 0.0


@case
def test_coupon_selection():
    items = (MenuItem("BIGMAC", "巨无霸", "main", 550, 25, 30, 43, 950, 26.5),
             MenuItem("FRIESM", "薯条(中)", "side", 340, 4, 17, 44, 250, 12.0),
             MenuItem("COKEM", "可乐(中)", "drink", 150, 0, 0, 38, 15, 9.0))
    subtotal = 47.5

    c5 = Coupon("A", "满30减5", "amount", 5, min_spend=30)
    c8 = Coupon("B", "巨无霸8折", "percent", 0.8, applies_skus=("BIGMAC",), max_discount=6)
    c4 = Coupon("C", "主食立减4", "amount", 4, applies_categories=("main",), min_spend=25)

    best, cut = best_coupon([c5, c8, c4], items, subtotal)
    # 8 折作用于整单 = 9.5 元，但被 max_discount=6 截断；满30减5 只减 5
    assert best.coupon_id == "B", best
    assert approx(cut, 6.0), cut

    # 未达门槛的券不可用
    best2, cut2 = best_coupon([c5], items, 20.0)
    assert best2 is None and cut2 == 0.0

    # 折扣不得超过订单金额
    assert _coupon_discount(Coupon("D", "大额券", "amount", 999), 30.0) == 30.0


@case
def test_score_penalties():
    from nutrition_target import MealTarget
    target = MealTarget(kcal=600, protein_g=40, fat_g=20, carb_g=70,
                        sodium_mg=700, kcal_low=540, kcal_high=636)
    w = ScoreWeights()

    hi = MenuItem("X", "高钠炸物", "main", 600, 40, 20, 70, 3000, 30.0)
    sc = evaluate(_mk_combo(hi), target, w, price_ceiling=40.0)
    assert sc.breakdown["sodium"] > 0, sc.breakdown
    assert sc.breakdown["kcal"] == 0.0

    low = MenuItem("Y", "热量过低", "main", 200, 40, 20, 5, 100, 10.0)
    sc2 = evaluate(_mk_combo(low), target, w, price_ceiling=40.0)
    assert sc2.breakdown["kcal"] > 0, sc2.breakdown

    # 蛋白质缺口应被重罚，且罚分随缺口单调增加
    p1 = MenuItem("Z1", "蛋白缺一半", "main", 600, 20, 20, 70, 600, 20.0)
    p2 = MenuItem("Z2", "蛋白更缺", "main", 600, 8, 20, 70, 600, 20.0)
    s1 = evaluate(_mk_combo(p1), target, w, price_ceiling=40.0).breakdown["protein"]
    s2 = evaluate(_mk_combo(p2), target, w, price_ceiling=40.0).breakdown["protein"]
    assert 0 < s1 < s2, (s1, s2)

    # 高脂应被罚
    fatty = MenuItem("F", "高脂", "main", 600, 40, 40, 10, 600, 20.0)
    assert evaluate(_mk_combo(fatty), target, w, price_ceiling=40.0).breakdown["fat"] > 0


def _mk_combo(item):
    from macro_optimizer import Combo
    return Combo(items=(item,), subtotal=item.price, set_saving=0.0,
                 coupon=None, discount=0.0, payable=item.price)


@case
def test_optimizer_end_to_end():
    with open(SAMPLE_PATH, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    menu, coupons = load_menu(payload)
    assert len(menu) >= 15 and len(coupons) >= 3

    p = UserProfile(sex="male", age=30, height_cm=175, weight_kg=72, goal="cut")
    d = derive_daily_target(p)
    target = allocate_meal(d, 3, meal_index=1)
    res = optimize(menu, coupons, target)
    picks = res.picks()

    assert res.evaluated_count > 0
    assert len(picks) >= 2, picks
    # 方案之间不得重复
    keys = [tuple(i.sku for i in sc.combo.items) for _, sc in picks]
    assert len(keys) == len(set(keys))

    best = res.best_overall
    assert best is not None
    # 综合最优必须落在热量可接受带内（或明确放宽）
    if res.relax_note is None:
        assert target.kcal_low <= best.combo.kcal <= target.kcal_high, best.combo.kcal
        assert best.combo.sodium_mg <= target.sodium_mg * 1.5

    # 极致省钱方案的实付必须不高于综合最优
    assert res.best_value.combo.payable <= best.combo.payable + 1e-6

    # 枚举数量应与「主食 × (小食+1) × (饮品+1)」一致
    n_main = sum(1 for i in menu if i.category == "main")
    n_side = sum(1 for i in menu if i.category == "side")
    n_drink = sum(1 for i in menu if i.category == "drink")
    assert res.evaluated_count == n_main * (n_side + 1) * (n_drink + 1)


@case
def test_goal_variants_differ():
    """不同目标应产出不同目标值，避免退化成同一套输出。"""
    with open(SAMPLE_PATH, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    menu, coupons = load_menu(payload)

    out = {}
    for goal in ("cut", "maintain", "bulk"):
        p = UserProfile(sex="female", age=28, height_cm=165, weight_kg=58, goal=goal)
        d = derive_daily_target(p)
        t = allocate_meal(d, 3, meal_index=1)
        r = optimize(menu, coupons, t)
        out[goal] = (d.kcal, r.best_overall.combo.kcal)
    assert out["cut"][0] < out["maintain"][0] < out["bulk"][0]


# --------------------------------------------------------------------------- #

def main() -> int:
    passed, failed = 0, []
    print("=" * 70)
    print("MacroBuddy 自测")
    print("=" * 70)
    for fn in CASES:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:                      # noqa: BLE001
            failed.append((fn.__name__, exc))
            print(f"  FAIL  {fn.__name__}: {exc}")
            traceback.print_exc()
    print("-" * 70)
    print(f"结果：{passed} 通过 / {len(failed)} 失败 / 共 {len(CASES)}")
    if failed:
        return 1
    print("所有用例通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
