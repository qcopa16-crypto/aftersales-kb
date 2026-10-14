"""仓库级 pytest 配置：统一默认离线收集口径，隔离真实链路用例（P2-1）。

背景
----
CI 的离线命令曾显式 `--ignore=tests/test_langgraph_real.py`，而本地直接
`pytest tests` 会一并收集真实依赖用例（真实 Milvus / 本地 BGE / MinerU / 云 LLM），
导致同一份代码在本地得到 670 例、在 CI 得到 663 例的口径差异，难以比对结果。

契约（本文件是唯一权威定义）
------------------------------
* 默认（REAL_CHAIN_ENABLED 未设置或 != 1）：**不收集** tests/test_langgraph_real.py。
  于是 `pytest tests -q` 在本地与 CI 得到完全相同的用例集合（离线口径一致）。
* 显式真实链路：`REAL_CHAIN_ENABLED=1 pytest tests/test_langgraph_real.py -v`
  仍按原样收集并运行 7 例；真实链路行为与既有流程完全一致，未被破坏。
* 真实链路用例统一打上 `real_chain` marker（在 pyproject.toml 注册），
  便于按标记筛选：`-m real_chain` / `-m 'not real_chain'`。

注意：不要在此处改动真实链路用例本身的语义或环境；env 装配仍由
tests/conftest.py（REAL_CHAIN_ENABLED 分支）与 tests/test_langgraph_real.py 负责。
"""

from __future__ import annotations

import os

# 真实链路用例（真实 Milvus/BGE/MinerU/云 LLM）——仅显式启用时收集。
_REAL_CHAIN_FILE = "tests/test_langgraph_real.py"

# collect_ignore 的路径相对本 conftest.py 所在目录（即仓库根 aftersales-kb-ai）。
# 未显式启用真实链路时排除该文件，保证默认离线口径与 CI 一致。
if os.environ.get("REAL_CHAIN_ENABLED", "0") != "1":
    collect_ignore = [_REAL_CHAIN_FILE]


def pytest_collection_modifyitems(config, items):
    """给真实链路用例补 `real_chain` marker（默认离线不收集，显式运行时生效）。"""
    import pytest

    for item in items:
        path = str(getattr(item, "fspath", None) or getattr(item, "path", ""))
        normalized = path.replace("\\", "/")
        if normalized.endswith(_REAL_CHAIN_FILE):
            item.add_marker(pytest.mark.real_chain)
