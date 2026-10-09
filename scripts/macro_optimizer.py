# -*- coding: utf-8 -*-
"""MacroBuddy · 点单方案优化内核（零依赖纯 Python）

设计原则
--------
1. **数值不进 LLM**：候选枚举、套餐价判定、优惠券选择、多目标打分排序全部在本模块
   完成。LLM 只做意图解析与话术生成，因此方案中每一个数字都可复现、可追溯、可解释。
2. **零第三方依赖**：仅使用标准库，任何人 clone 后直接 ``python3 scripts/demo_offline.py``
   即可在无 Token 环境下验证算法正确性。
3. **可解释**：每个方案都返回扣分/加分明细，便于回答"为什么推荐它"。

打分约定：**分数越低越好**。分数由各项惩罚项加权求和，再减去优惠券利用加分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from nutrition_target import MealTarget

# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MenuItem:
    """菜单项。字段命名与麦当劳 MCP 返回结构解耦，便于替换数据源。"""

    sku: str
    name: str
    category: str                 # main / side / drink / dessert
    kcal: float
    protein_g: float
    fat_g: float
    carb_g: float
    sodium_mg: float
    price: float                  # 单品价（元）
    set_price: Optional[float] = None   # 套餐价（含 1 份小食 + 1 份饮品），仅主食可有
    tags: Tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> "MenuItem":
        return cls(
            sku=str(d["sku"]),
            name=str(d["name"]),
            category=str(d.get("category", "main")),
            kcal=float(d.get("kcal", 0)),
            protein_g=float(d.get("protein_g", 0)),
            fat_g=float(d.get("fat_g", 0)),
            carb_g=float(d.get("carb_g", 0)),
            sodium_mg=float(d.get("sodium_mg", 0)),
            price=float(d.get("price", 0)),
            set_price=(float(d["set_price"]) if d.get("set_price") is not None else None),
            tags=tuple(d.get("tags", ()) or ()),
        )


@dataclass(frozen=True)
class Coupon:
    """优惠券。

    ``discount_type``：
      - ``amount``  ：直减 ``value`` 元
      - ``percent`` ：按 ``value`` 打折（0.8 表示 8 折）
    券的适用范围由 ``applies_categories`` / ``applies_skus`` 描述，二者皆空表示全场可用。
    """

    coupon_id: str
    title: str
    discount_type: str
    value: float
    applies_categories: Tuple[str, ...] = ()
    applies_skus: Tuple[str, ...] = ()
    min_spend: float = 0.0
    max_discount: Optional[float] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Coupon":
        return cls(
            coupon_id=str(d["coupon_id"]),
            title=str(d.get("title", d["coupon_id"])),
            discount_type=str(d.get("discount_type", "amount")),
            value=float(d.get("value", 0)),
            applies_categories=tuple(d.get("applies_categories", ()) or ()),
            applies_skus=tuple(d.get("applies_skus", ()) or ()),
            min_spend=float(d.get("min_spend", 0)),
            max_discount=(float(d["max_discount"]) if d.get("max_discount") is not None else None),
        )


@dataclass
class Combo:
    """一个候选组合及其价格拆解。"""

    items: Tuple[MenuItem, ...]
    subtotal: float           # 未用券前金额（已含套餐价优惠）
    set_saving: float         # 套餐价相对单品价省下的金额
    coupon: Optional[Coupon]
    discount: float           # 券带来的减免
    payable: float            # 实付

    @property
    def kcal(self) -> float:
        return sum(i.kcal for i in self.items)

    @property
    def protein_g(self) -> float:
        return sum(i.protein_g for i in self.items)

    @property
    def fat_g(self) -> float:
        return sum(i.fat_g for i in self.items)

    @property
    def carb_g(self) -> float:
        return sum(i.carb_g for i in self.items)

    @property
    def sodium_mg(self) -> float:
        return sum(i.sodium_mg for i in self.items)

    @property
    def total_saving(self) -> float:
        return self.set_saving + self.discount

    def describe_items(self) -> str:
        return " + ".join(i.name for i in self.items)


@dataclass
class ScoredCombo:
    """带分数与解释的组合。

    ``in_band`` 标记热量是否落在单餐目标的可接受带内。排序时该标记优先于分数：
    营养达标的方案永远排在热量偏离目标带的方案之前，保证"优惠券加分"不会把
    明显偏离营养目标的方案顶到推荐位。
    """

    combo: Combo
    score: float
    in_band: bool = True
    breakdown: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 价格计算
# --------------------------------------------------------------------------- #

def _apply_set_price(items: Sequence[MenuItem]) -> Tuple[float, float]:
    """判定套餐价是否可用，返回 (subtotal, set_saving)。

    规则：当组合恰为「主食 + 小食 + 饮品」各一份，且主食带套餐价时，
    按 min(单品价之和, 套餐价) 计算。这是真实点单中最常见的省钱路径。
    """
    raw = sum(i.price for i in items)
    if len(items) != 3:
        return raw, 0.0

    by_cat = {i.category: i for i in items}
    if set(by_cat) != {"main", "side", "drink"}:
        return raw, 0.0

    main = by_cat["main"]
    if main.set_price is None:
        return raw, 0.0

    if main.set_price < raw:
        return main.set_price, raw - main.set_price
    return raw, 0.0


def _coupon_applies(coupon: Coupon, items: Sequence[MenuItem], subtotal: float) -> bool:
    if subtotal < coupon.min_spend:
        return False
    if not coupon.applies_categories and not coupon.applies_skus:
        return True
    for it in items:
        if it.sku in coupon.applies_skus or it.category in coupon.applies_categories:
            return True
    return False


def _coupon_discount(coupon: Coupon, subtotal: float) -> float:
    if coupon.discount_type == "percent":
        cut = subtotal * (1.0 - coupon.value)
    else:
        cut = coupon.value
    if coupon.max_discount is not None:
        cut = min(cut, coupon.max_discount)
    return max(0.0, min(cut, subtotal))


def best_coupon(coupons: Iterable[Coupon], items: Sequence[MenuItem],
                subtotal: float) -> Tuple[Optional[Coupon], float]:
    """在可用券中选出减免最大的一张。不允许叠加，符合真实券包规则。"""
    best: Optional[Coupon] = None
    best_cut = 0.0
    for c in coupons:
        if not _coupon_applies(c, items, subtotal):
            continue
        cut = _coupon_discount(c, subtotal)
        if cut > best_cut + 1e-9:
            best, best_cut = c, cut
    return best, round(best_cut, 2)


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #

@dataclass
class ScoreWeights:
    kcal: float = 45.0
    protein: float = 30.0
    fat: float = 12.0
    sodium: float = 8.0
    price: float = 5.0
    coupon_bonus: float = 6.0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def evaluate(combo: Combo, target: MealTarget, weights: ScoreWeights,
             price_ceiling: float) -> ScoredCombo:
    """多目标打分：热量带内最优、蛋白质缺口重罚、脂肪钠超标扣分、价格轻罚、用券加分。"""
    bd: Dict[str, float] = {}
    reasons: List[str] = []

    # --- 热量：落在 [low, high] 区间内不扣分，偏离越多扣越多 ------------------
    k = combo.kcal
    if k < target.kcal_low:
        dev = (target.kcal_low - k) / target.kcal_low
        bd["kcal"] = _clamp01(dev / 0.35) * weights.kcal
        reasons.append(f"热量偏低 {target.kcal_low - k:.0f} kcal")
    elif k > target.kcal_high:
        dev = (k - target.kcal_high) / target.kcal_high
        bd["kcal"] = _clamp01(dev / 0.35) * weights.kcal
        reasons.append(f"热量偏高 {k - target.kcal_high:.0f} kcal")
    else:
        bd["kcal"] = 0.0
        reasons.append(f"热量 {k:.0f} kcal 落在目标带内")

    # --- 蛋白质：缺口重罚，轻度过量不罚，极端过量轻罚 ------------------------
    p, p_need = combo.protein_g, target.protein_g
    pen_p = 0.0
    if p_need > 0:
        if p < 0.9 * p_need:
            # 缺口相对"90% 需求线"归一化：蛋白为 0 时恰好满罚，缺口越大罚分越高，
            # 不使用额外的饱和系数，避免 40% 缺口就触及上限而丧失区分度。
            pen_p = _clamp01((0.9 * p_need - p) / (0.9 * p_need))
            reasons.append(f"蛋白质缺口 {0.9 * p_need - p:.0f} g")
        elif p > 2.5 * p_need:
            pen_p = 0.30
            reasons.append("蛋白质远超需求，性价比下降")
        else:
            reasons.append(f"蛋白质 {p:.0f} g 达标")
    bd["protein"] = pen_p * weights.protein

    # --- 脂肪：只罚超标，低脂本身不是问题 -----------------------------------
    f, f_cap = combo.fat_g, target.fat_g
    if f_cap > 0 and f > 1.15 * f_cap:
        bd["fat"] = _clamp01(((f - 1.15 * f_cap) / (1.15 * f_cap)) / 0.50) * weights.fat
        reasons.append(f"脂肪超出上限 {f - 1.15 * f_cap:.0f} g")
    else:
        bd["fat"] = 0.0

    # --- 钠 ----------------------------------------------------------------
    s, s_cap = combo.sodium_mg, target.sodium_mg
    if s_cap > 0 and s > s_cap:
        bd["sodium"] = _clamp01(((s - s_cap) / (s_cap * 0.5))) * weights.sodium
        reasons.append(f"钠超出单餐参考值 {s - s_cap:.0f} mg")
    else:
        bd["sodium"] = 0.0

    # --- 价格：同营养水平下越便宜越好 ---------------------------------------
    bd["price"] = (combo.payable / price_ceiling) * weights.price if price_ceiling > 0 else 0.0

    # --- 优惠券利用 ---------------------------------------------------------
    bonus = 0.0
    if combo.coupon is not None and combo.subtotal > 0:
        ratio = combo.discount / (0.15 * combo.subtotal)
        bonus = weights.coupon_bonus * _clamp01(ratio)
        reasons.append(f"已用「{combo.coupon.title}」立减 {combo.discount:.1f} 元")
    if combo.set_saving > 0:
        reasons.append(f"套餐价较单品省 {combo.set_saving:.1f} 元")

    total = sum(bd.values()) - bonus
    bd["coupon_bonus"] = -bonus
    return ScoredCombo(combo=combo, score=round(total, 3),
                       in_band=(target.kcal_low <= k <= target.kcal_high),
                       breakdown=bd, reasons=reasons)


# --------------------------------------------------------------------------- #
# 候选枚举与优化
# --------------------------------------------------------------------------- #

def build_combos(menu: Sequence[MenuItem], coupons: Sequence[Coupon],
                 allow_dessert: bool = False) -> List[Combo]:
    """枚举「主食 1 份 + 小食 0/1 份 + 饮品 0/1 份（+ 甜品 0/1 份）」的全部组合。"""
    mains = [i for i in menu if i.category == "main"]
    sides = [i for i in menu if i.category == "side"]
    drinks = [i for i in menu if i.category == "drink"]
    desserts = [i for i in menu if i.category == "dessert"]

    side_opts: List[Optional[MenuItem]] = [None] + sides
    drink_opts: List[Optional[MenuItem]] = [None] + drinks
    dessert_opts: List[Optional[MenuItem]] = ([None] + desserts) if allow_dessert else [None]

    combos: List[Combo] = []
    for m, s, d, de in product(mains, side_opts, drink_opts, dessert_opts):
        items = tuple(x for x in (m, s, d, de) if x is not None)
        subtotal, set_saving = _apply_set_price(items)
        coupon, discount = best_coupon(coupons, items, subtotal)
        combos.append(Combo(
            items=items,
            subtotal=round(subtotal, 2),
            set_saving=round(set_saving, 2),
            coupon=coupon,
            discount=round(discount, 2),
            payable=round(subtotal - discount, 2),
        ))
    return combos


@dataclass
class OptimizationResult:
    """优化结果：三套推荐 + 备选池。"""

    target: MealTarget
    best_overall: Optional[ScoredCombo] = None
    best_protein: Optional[ScoredCombo] = None
    best_value: Optional[ScoredCombo] = None
    relax_note: Optional[str] = None
    in_band_count: int = 0
    evaluated_count: int = 0

    def picks(self) -> List[Tuple[str, ScoredCombo]]:
        """返回去重后的推荐清单，键为方案定位。"""
        out: List[Tuple[str, ScoredCombo]] = []
        seen = set()
        for label, sc in (("综合最优", self.best_overall),
                          ("高蛋白优选", self.best_protein),
                          ("极致省钱", self.best_value)):
            if sc is None:
                continue
            key = tuple(i.sku for i in sc.combo.items)
            if key in seen:
                continue
            seen.add(key)
            out.append((label, sc))
        return out


def _is_feasible(combo: Combo, target: MealTarget) -> bool:
    """硬约束：热量不得偏离目标带过远，钠不得超过参考值 1.5 倍。"""
    return (target.kcal * 0.78 <= combo.kcal <= target.kcal * 1.35
            and combo.sodium_mg <= target.sodium_mg * 1.5)


def optimize(menu: Sequence[MenuItem], coupons: Sequence[Coupon],
             target: MealTarget, allow_dessert: bool = False,
             weights: Optional[ScoreWeights] = None) -> OptimizationResult:
    """主入口：产出「综合最优 / 高蛋白 / 极致省钱」三套方案。"""
    weights = weights or ScoreWeights()
    combos = build_combos(menu, coupons, allow_dessert=allow_dessert)
    if not combos:
        return OptimizationResult(target=target)

    ceiling = max(c.subtotal for c in combos) or 1.0
    scored = [evaluate(c, target, weights, ceiling) for c in combos]
    # 字典序排序：先按"营养是否达标"，再按综合分。保证营养优先级高于优惠力度。
    scored.sort(key=lambda s: (0 if s.in_band else 1, s.score))

    in_band = [s for s in scored if s.in_band]
    feasible = [s for s in scored if _is_feasible(s.combo, target)]

    relax_note: Optional[str] = None
    if in_band:
        pool = in_band
    elif feasible:
        pool = feasible
        relax_note = "无完全落在热量目标带内的组合，已放宽到可接受区间后择优"
    else:
        # 连可接受区间都为空时，退化为"热量不低于目标 60%"并在报告中显式声明
        pool = [s for s in scored if s.combo.kcal >= target.kcal * 0.6]
        if pool:
            relax_note = "无达标组合，已放宽约束并优先保证蛋白质与热量下限"
        else:
            pool = scored[:50]
            relax_note = "当前菜单无法满足目标，已按最接近原则降级推荐"

    res = OptimizationResult(target=target, relax_note=relax_note,
                             in_band_count=len(in_band), evaluated_count=len(scored))
    if pool:
        res.best_overall = pool[0]
        res.best_protein = max(
            pool, key=lambda s: (s.combo.protein_g, -s.combo.payable, -s.score))
        res.best_value = min(
            pool, key=lambda s: (s.combo.payable, s.score))
    return res


def load_menu(payload: dict) -> Tuple[List[MenuItem], List[Coupon]]:
    """从 ``{"menu": [...], "coupons": [...]}`` 结构加载数据。"""
    menu = [MenuItem.from_dict(d) for d in payload.get("menu", [])]
    coupons = [Coupon.from_dict(d) for d in payload.get("coupons", [])]
    return menu, coupons


def render_table(picks: Sequence[Tuple[str, ScoredCombo]]) -> str:
    """把推荐方案渲染成等宽文本表，便于复制进 Issue / 群聊。"""
    head = (f"{'定位':<6}{'组合':<40}{'kcal':>6}{'蛋白g':>7}"
            f"{'脂肪g':>7}{'钠mg':>8}{'原价':>8}{'实付':>8}")
    lines = [head, "-" * len(head)]
    for label, sc in picks:
        c = sc.combo
        name = c.describe_items()
        if len(name) > 38:
            name = name[:36] + ".."
        # 中文按 2 字符宽度对齐
        pad = 40 - sum(2 if ord(ch) > 127 else 1 for ch in name)
        lines.append(
            f"{label:<6}{name}{' ' * max(1, pad)}"
            f"{c.kcal:>6.0f}{c.protein_g:>7.0f}{c.fat_g:>7.0f}"
            f"{c.sodium_mg:>8.0f}{c.subtotal:>8.1f}{c.payable:>8.1f}"
        )
    return "\n".join(lines)
