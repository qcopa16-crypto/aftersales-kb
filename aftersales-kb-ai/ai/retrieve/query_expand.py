"""Lightweight query expansion for aftersales paraphrase / synonym recall.

Rule-based only (no LLM). Produces extra query strings for dense multi-embed
and extra terms for lexical BM25.
"""

from __future__ import annotations

import re

# Synonym groups: if any surface form appears, all forms are added.
_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("碎屏", "屏幕碎", "屏裂", "裂屏", "显示屏碎", "摔裂", "换屏", "屏幕坏"),
    ("保修", "质保", "保修期", "质保期"),
    ("卡纸", "夹纸", "塞纸"),
    ("信号差", "信号不好", "信号弱", "无线信号", "信号"),
    ("恢复出厂", "重置", "reset", "恢复默认", "出厂设置"),
    ("报价", "报价单", "费用", "多少钱", "预算", "价位", "价格"),
    ("电池", "电芯", "鼓包"),
    ("上门", "上门服务", "到场"),
    ("退货", "退款", "无理由退货"),
    ("换货", "更换新机"),
    ("投诉", "升级投诉"),
    ("备件", "配件", "零件", "旧件"),
    ("工单", "报修单", "服务单"),
    ("错误码", "错误代码", "故障码", "报错", "E-05", "E05"),
    ("定影", "定影单元", "定影组件"),
    ("盖板", "上盖", "后盖"),
    ("搓纸轮", "进纸轮"),
    ("管理地址", "管理页面", "登录地址", "192.168"),
    ("对账", "结算"),
    ("折扣", "打折", "优惠"),
    ("目录价", "渠道供货价", "供货价", "项目价", "项目特价"),
    ("竞品", "竞品话术", "对比话术", "销售话术"),
    ("盖板", "上盖", "后盖", "盖板未关"),
    ("恢复", "复位", "出厂"),
)

# Directed phenomenon -> error-code / jargon mapping (asymmetric, unlike the
# symmetric synonym groups above). A user often describes a *symptom* ("总是卡纸")
# while the gold chunk is keyed on the *code* ("E-01 卡纸"); adding the code lets
# both dense and lexical paths bridge that vocabulary gap.
#
# Every expansion token below is verified against docs/demo-knowledge:
#   sop/故障代码-打印机错误码.md  -> E-01..E-08
#   sop/故障代码-路由器与网关.md  -> WAN/PWR/SYS rows (光猫/网线/适配器/启动失败)
#   sop/故障代码-商用电脑.md      -> 风扇狂转 -> 清灰
_PHENOMENON_TO_CODE: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("卡纸", "夹纸", "塞纸"), ("E-01",)),
    (("缺纸", "进纸失败", "不进纸", "无法进纸"), ("E-02",)),
    (("硒鼓", "墨粉", "碳粉", "粉盒"), ("E-03",)),
    (("定影",), ("E-04",)),
    (("盖板", "上盖", "后盖"), ("E-05",)),
    (("通信", "网络错误", "网络故障"), ("E-06",)),
    (("打印头", "激光单元", "激光器"), ("E-07",)),
    (("进稿器", "ADF", "输稿器", "原稿进稿器"), ("E-08",)),
    (("WAN", "WAN 口"), ("光猫", "网线")),
    (("PWR",), ("适配器",)),
    (("SYS",), ("启动失败",)),
    (("风扇狂转", "风扇一直转", "风扇转得", "狂转"), ("清灰",)),
)

_CODE_RE = re.compile(r"[A-Za-z]{1,4}-?\d{1,4}")

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_CJK_RE = re.compile(r"[一-鿿]+")


def expand_terms(question: str, *, enable_codes: bool = True) -> list[str]:
    """Unique expansion tokens (not including the raw question).

    ``enable_codes`` toggles the directed phenomenon->error-code mapping
    (``_PHENOMENON_TO_CODE``); off keeps the legacy synonym-only behaviour.
    """
    q = question or ""
    low = q.lower()
    out: list[str] = []
    seen: set[str] = set()
    for group in _SYNONYM_GROUPS:
        if any(s.lower() in low or s in q for s in group):
            for s in group:
                if s.lower() not in seen:
                    seen.add(s.lower())
                    out.append(s)
    if enable_codes:
        for triggers, expansions in _PHENOMENON_TO_CODE:
            if any(t.lower() in low or t in q for t in triggers):
                for s in expansions:
                    if s.lower() not in seen:
                        seen.add(s.lower())
                        out.append(s)
    # Keep ASCII codes (E-05, LA2608) as extra lexical anchors if present.
    for m in _CODE_RE.finditer(q):
        u = m.group(0).upper()
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def expand_codes(question: str) -> list[str]:
    """Only the phenomenon->error-code expansions (E-01, E-08, …).

    Unlike `expand_terms`, this returns *codes only* (no 光猫/网线/清灰 jargon).
    The demo corpus' error codes (E-01..E-08) are synthetic and appear **only**
    in `sop/故障代码-打印机错误码.md`, so anchoring the rerank stage on them is
    high-precision: it cannot accidentally promote an unrelated product manual.
    """
    q = question or ""
    low = q.lower()
    out: list[str] = []
    seen: set[str] = set()
    for triggers, expansions in _PHENOMENON_TO_CODE:
        if any(t.lower() in low or t in q for t in triggers):
            for s in expansions:
                if any(ch.isdigit() for ch in s) and s.lower() not in seen:
                    seen.add(s.lower())
                    out.append(s)
    return out


def expand_terms_for_lexical(
    question: str, *, max_extra: int = 3, enable_codes: bool = True
) -> str:
    """Compact lexical query: original + few high-value extras (avoid posting explosion)."""
    q = (question or "").strip()
    extra = [t for t in expand_terms(q, enable_codes=enable_codes) if t.lower() not in q.lower()]
    if not extra:
        return q
    # Prefer longer surface forms (more specific) and ASCII codes.
    extra.sort(key=lambda s: (not any(ch.isdigit() for ch in s), -len(s)))
    return q + " " + " ".join(extra[:max_extra])


def expand_query_texts(
    question: str, *, max_variants: int = 2, enable_codes: bool = True
) -> list[str]:
    """Original question first, then up to max_variants synonym-augmented queries."""
    q = (question or "").strip()
    terms = expand_terms(q, enable_codes=enable_codes)
    extra = [t for t in terms if t.lower() not in q.lower()]
    texts = [q]
    if extra:
        texts.append(q + " " + " ".join(extra[:8]))
    if len(extra) > 4 and max_variants > 1:
        texts.append(" ".join(extra[:12]))
    return texts[: max(1, max_variants + 1)]
