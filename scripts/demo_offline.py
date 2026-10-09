#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MacroBuddy · 离线验证入口（无需 MCP Token）

用途
----
在没有麦当劳 MCP Token 的环境下，用 ``examples/sample_menu.json`` 的示例数据
完整跑通「用户档案 -> 每日目标 -> 单餐目标 -> 候选枚举 -> 三套方案」全链路，
用于验证算法正确性与可复现性。

用法
----
    python3 scripts/demo_offline.py
    python3 scripts/demo_offline.py --goal cut --weight 72 --height 175 --meals 3 --index 1
    python3 scripts/demo_offline.py --json

注意：示例数据为近似估计，非麦当劳官方实时数据。
真实运行时，菜单与价格必须来自 mcd-mcp 的实时工具返回。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from macro_optimizer import load_menu, optimize, render_table          # noqa: E402
from nutrition_target import (                                          # noqa: E402
    ACTIVITY_FACTORS, GOAL_LABELS, UserProfile,
    allocate_meal, derive_daily_target, describe_daily_target,
)

SAMPLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "sample_menu.json",
)

#: 常见餐次的自定义标签，让输出更贴近真实对话
MEAL_LABELS = {
    2: ["第一餐", "第二餐"],
    3: ["早餐", "午餐", "晚餐"],
    4: ["早餐", "午餐", "下午加餐", "晚餐"],
    5: ["早餐", "上午加餐", "午餐", "下午加餐", "晚餐"],
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MacroBuddy 离线算法验证")
    p.add_argument("--sex", default="male", choices=["male", "female", "unspecified"])
    p.add_argument("--age", type=int, default=30)
    p.add_argument("--height", type=float, default=175.0, help="身高 cm")
    p.add_argument("--weight", type=float, default=72.0, help="体重 kg")
    p.add_argument("--activity", default="light", choices=sorted(ACTIVITY_FACTORS))
    p.add_argument("--goal", default="cut", choices=sorted(GOAL_LABELS))
    p.add_argument("--meals", type=int, default=3, help="每日餐次")
    p.add_argument("--index", type=int, default=1, help="本餐序号，0 起")
    p.add_argument("--allow-dessert", action="store_true", help="允许纳入甜品")
    p.add_argument("--json", action="store_true", help="以 JSON 输出，便于程序消费")
    p.add_argument("--menu", default=SAMPLE_PATH, help="菜单数据路径")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    with open(args.menu, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    menu, coupons = load_menu(payload)

    profile = UserProfile(
        sex=args.sex, age=args.age, height_cm=args.height, weight_kg=args.weight,
        activity=args.activity, goal=args.goal, meals_per_day=args.meals,
    )
    daily = derive_daily_target(profile)
    meal_label = MEAL_LABELS.get(profile.meals_per_day, [])
    meal_label = meal_label[args.index] if 0 <= args.index < len(meal_label) \
        else f"第 {args.index + 1} 餐"
    target = allocate_meal(daily, profile.meals_per_day, meal_index=args.index,
                           label=meal_label)
    result = optimize(menu, coupons, target, allow_dessert=args.allow_dessert)
    picks = result.picks()

    if args.json:
        print(json.dumps({
            "profile": {
                "sex": profile.sex, "age": profile.age, "height_cm": profile.height_cm,
                "weight_kg": profile.weight_kg, "activity": profile.activity,
                "goal": profile.goal, "meals_per_day": profile.meals_per_day,
            },
            "daily_target": daily.to_dict(),
            "meal_target": target.to_dict(),
            "stats": {"evaluated": result.evaluated_count,
                      "in_band": result.in_band_count,
                      "relax_note": result.relax_note},
            "picks": [{
                "role": role,
                "items": [i.name for i in sc.combo.items],
                "skus": [i.sku for i in sc.combo.items],
                "kcal": round(sc.combo.kcal, 1),
                "protein_g": round(sc.combo.protein_g, 1),
                "fat_g": round(sc.combo.fat_g, 1),
                "carb_g": round(sc.combo.carb_g, 1),
                "sodium_mg": round(sc.combo.sodium_mg, 1),
                "subtotal": sc.combo.subtotal,
                "coupon": (sc.combo.coupon.title if sc.combo.coupon else None),
                "discount": sc.combo.discount,
                "set_saving": sc.combo.set_saving,
                "payable": sc.combo.payable,
                "score": sc.score,
                "reasons": sc.reasons,
            } for role, sc in picks],
        }, ensure_ascii=False, indent=2))
        return 0

    line = "=" * 78
    print(line)
    print("MacroBuddy · 离线算法验证")
    print("（数据源：examples/sample_menu.json，示例数据，非麦当劳官方实时数据）")
    print(line)

    print("\n[1] 用户档案")
    print(f"    {profile.sex} / {profile.age} 岁 / {profile.height_cm:.0f} cm / "
          f"{profile.weight_kg:.0f} kg / 活动量 {profile.activity} / "
          f"目标 {GOAL_LABELS.get(profile.goal, profile.goal)} / "
          f"每日 {profile.meals_per_day} 餐")

    print("\n[2] 每日宏量目标")
    print(f"    {describe_daily_target(daily)}")

    print(f"\n[3] {target.label}目标")
    print(f"    热量 {target.kcal:.0f} kcal（可接受 {target.kcal_low:.0f}-{target.kcal_high:.0f}）"
          f"｜蛋白 {target.protein_g:.0f} g｜脂肪 {target.fat_g:.0f} g"
          f"｜碳水 {target.carb_g:.0f} g｜钠 {target.sodium_mg:.0f} mg")

    print("\n[4] 搜索空间")
    print(f"    枚举组合 {result.evaluated_count} 个，其中热量落在目标带内 {result.in_band_count} 个")
    if result.relax_note:
        print(f"    ⚠ {result.relax_note}")

    print("\n[5] 推荐方案")
    for line_ in render_table(picks).splitlines():
        print("    " + line_)

    print("\n[6] 推荐理由")
    for role, sc in picks:
        print(f"    · {role}（得分 {sc.score}）")
        print(f"      组合：{sc.combo.describe_items()}")
        print(f"      计价：单品/套餐原价 {sc.combo.subtotal:.1f} 元"
              f"｜套餐价省 {sc.combo.set_saving:.1f} 元"
              f"｜用券省 {sc.combo.discount:.1f} 元"
              f"｜实付 {sc.combo.payable:.1f} 元")
        for r in sc.reasons:
            print(f"      - {r}")

    print("\n" + line)
    print("提示：方案中的热量、价格、优惠均由上述确定性算法计算得出，LLM 不参与算术。")
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
