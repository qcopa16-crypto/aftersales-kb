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

_CODE_RE = re.compile(r"[A-Za-z]{1,4}-?\d{1,4}")

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_CJK_RE = re.compile(r"[一-鿿]+")


def expand_terms(question: str) -> list[str]:
    """Unique expansion tokens (not including the raw question)."""
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
    # Keep ASCII codes (E-05, LA2608) as extra lexical anchors if present.
    for m in _CODE_RE.finditer(q):
        u = m.group(0).upper()
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def expand_terms_for_lexical(question: str, *, max_extra: int = 3) -> str:
    """Compact lexical query: original + few high-value extras (avoid posting explosion)."""
    q = (question or "").strip()
    extra = [t for t in expand_terms(q) if t.lower() not in q.lower()]
    if not extra:
        return q
    # Prefer longer surface forms (more specific) and ASCII codes.
    extra.sort(key=lambda s: (not any(ch.isdigit() for ch in s), -len(s)))
    return q + " " + " ".join(extra[:max_extra])


def expand_query_texts(question: str, *, max_variants: int = 2) -> list[str]:
    """Original question first, then up to max_variants synonym-augmented queries."""
    q = (question or "").strip()
    terms = expand_terms(q)
    extra = [t for t in terms if t.lower() not in q.lower()]
    texts = [q]
    if extra:
        texts.append(q + " " + " ".join(extra[:8]))
    if len(extra) > 4 and max_variants > 1:
        texts.append(" ".join(extra[:12]))
    return texts[: max(1, max_variants + 1)]
