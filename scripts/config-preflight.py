#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""售后知识库 · 生产配置部署前体检（config preflight）。

用途
----
扫描生产环境变量配置（.env / prod.env.example），在发布前拦截以下**放行阻塞**项，
任一 ERROR 命中即以非零码退出，避免把「关闭 TLS / 跳过证书校验 / 默认账号 / H2 /
示例密钥 / 未替换占位符」的生产配置带上线：

  1. MySQL 连接关闭 TLS 或跳过证书校验（useSSL=false / sslMode=DISABLED|PREFERRED）；
  2. 使用 H2 内存库或内存向量库（非生产）；
  3. 业务账号使用默认/特权账号（root/sa/admin/…）或弱口令；
  4. 示例密钥（minioadmin 等）或过短的 JWT/内部 token；
  5. 模板占位符 `__CHANGE_ME__` 未替换。

背景
----
`deploy/compose.env.example` 是**本地/演示**模板；生产必须使用
`deploy/prod.env.example`（启用 MySQL TLS + 证书校验）。本脚本是二者的门禁：
生产发布前对实际注入的 `.env` 运行，必须 0 退出。

退出码
------
0 = 通过（或仅 WARN 且未加 --strict）；1 = 存在 ERROR（或 --strict 下存在 WARN）；2 = 用法/IO/环境错误。

用法
----
    python scripts/config-preflight.py                       # 校验仓库根 .env
    python scripts/config-preflight.py deploy/prod.env.example
    python scripts/config-preflight.py .env --strict         # WARN 也视为失败
    python scripts/config-preflight.py --list                # 仅列出检查规则
    python scripts/config-preflight.py .env --compose docker-compose.yml
                                                             # 额外校验 compose 端口绑定

说明：`--compose` 需要 PyYAML；**显式传入 `--compose` 时**若 PyYAML 缺失或 compose 解析失败，
本脚本以退出码 2 报错终止（**不静默降级**，避免 CI 漏掉端口暴露校验）。未传 `--compose` 时不依赖 PyYAML。
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# ---------------------------------------------------------------------------
# 1. 规则常量
# ---------------------------------------------------------------------------

PLACEHOLDER_MARK = "change_me"

# 生产业务账号禁用名单（这些是默认/特权账号，不应作为应用账号）
FORBIDDEN_DB_USERS = {"root", "sa", "admin", "postgres", "mysql", "dba"}

# 弱口令 / 示例口令
WEAK_SECRETS = {
    "", "root", "password", "passwd", "123456", "12345678", "123456789",
    "admin", "administrator", "changeme", "change_me", "minioadmin",
    "test", "secret", "kb_app", "aftersales_kb", "default", "letmein",
}

# 过短即判不合格的随机凭据（字节/字符数下限）
MIN_TOKEN_LEN = 32

# 示例 MinIO 凭据
EXAMPLE_MINIO = {"minioadmin", "minio", "minioadmin123", "admin", "password"}

# 非生产向量库实现
NON_PROD_VECTOR_STORES = {"memory", "in-memory", "inmemory", "faiss", "none", ""}

# 生产要求的 MySQL TLS 校验级别（Connector/J 8 语义）
STRICT_SSL_MODES = {"verify_identity", "verify_ca"}


# ---------------------------------------------------------------------------
# 2. .env 解析
# ---------------------------------------------------------------------------

def parse_env_file(path: str) -> dict[str, str]:
    """解析 KEY=VALUE 形式的 env 文件；忽略空行与 # 注释，支持引号包裹值。"""
    cfg: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                cfg[key] = val
    return cfg


# ---------------------------------------------------------------------------
# 3. 规则引擎
# ---------------------------------------------------------------------------

def _is_placeholder(val: str) -> bool:
    return val is None or PLACEHOLDER_MARK in (val or "").lower()


def check_config(cfg: dict[str, str]) -> list[tuple[str, str, str]]:
    """返回 [(severity, code, message), ...]；severity ∈ {ERROR, WARN}。"""
    findings: list[tuple[str, str, str]] = []

    def err(code: str, msg: str) -> None:
        findings.append(("ERROR", code, msg))

    def warn(code: str, msg: str) -> None:
        findings.append(("WARN", code, msg))

    # --- 0) 占位符未替换 -------------------------------------------------
    for key, val in cfg.items():
        if _is_placeholder(val):
            err("PLACEHOLDER", f"{key} 仍为占位符（__CHANGE_ME__），生产必须由 secret 注入真实值")

    # --- 1) MySQL TLS / H2 ----------------------------------------------
    mysql_url = cfg.get("MYSQL_URL", "")
    if mysql_url and not _is_placeholder(mysql_url):
        low = mysql_url.lower()
        if "jdbc:h2" in low:
            err("H2_DB", "MYSQL_URL 指向 H2 内存库（jdbc:h2:*），生产必须使用 MySQL")
        if "usessl=false" in low:
            err("MYSQL_TLS_OFF", "MYSQL_URL 含 useSSL=false（显式关闭 TLS），生产禁止")
        m = re.search(r"sslmode=([a-z_]+)", low)
        ssl_mode = m.group(1) if m else ""
        if ssl_mode in {"disabled", "preferred"}:
            err("MYSQL_TLS_WEAK", f"MYSQL_URL sslMode={ssl_mode} 不满足生产要求（需 VERIFY_IDENTITY/VERIFY_CA）")
        has_strict = ssl_mode in STRICT_SSL_MODES or "requiressl=true" in low
        if not has_strict:
            err("MYSQL_TLS_MISSING",
                "MYSQL_URL 未启用 TLS 证书校验：需 sslMode=VERIFY_IDENTITY（推荐）或 VERIFY_CA/requireSSL=true")
        if "allowpublickeyretrieval=true" in low and ssl_mode != "verify_identity":
            warn("MYSQL_PUBKEY",
                 "MYSQL_URL allowPublicKeyRetrieval=true：仅在证书校验（VERIFY_IDENTITY）下可接受，请确认")

    # --- 2) Spring profile / 向量库 -------------------------------------
    profile = cfg.get("SPRING_PROFILES_ACTIVE", "")
    if profile and not _is_placeholder(profile):
        if "h2" in profile.lower():
            err("H2_PROFILE", "SPRING_PROFILES_ACTIVE 含 h2，生产必须为 mysql")
        elif "mysql" not in profile.lower():
            warn("PROFILE", f"SPRING_PROFILES_ACTIVE={profile} 不含 mysql，请确认生产 profile")

    vector_store = cfg.get("VECTOR_STORE", "")
    if vector_store and not _is_placeholder(vector_store):
        if vector_store.strip().lower() in NON_PROD_VECTOR_STORES:
            err("VECTOR_STORE", f"VECTOR_STORE={vector_store} 非持久化生产实现（需 milvus/pgvector）")
        elif vector_store.strip().lower() not in {"milvus", "pgvector", "postgres", "pgsql"}:
            warn("VECTOR_STORE_UNKNOWN", f"VECTOR_STORE={vector_store} 非已知生产实现，请确认")

    # --- 3) 数据库账号 / 口令 -------------------------------------------
    db_user = cfg.get("MYSQL_USER", "")
    if db_user and not _is_placeholder(db_user):
        if db_user.strip().lower() in FORBIDDEN_DB_USERS:
            err("DB_USER_DEFAULT", f"MYSQL_USER={db_user} 为默认/特权账号，生产业务账号禁止使用")

    for key in ("MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "REDIS_PASSWORD",
                "MINIO_ROOT_PASSWORD", "LLM_API_KEY", "VLM_API_KEY", "MINERU_TOKEN"):
        val = cfg.get(key, "")
        if val and not _is_placeholder(val) and val.strip().lower() in WEAK_SECRETS:
            err("WEAK_SECRET", f"{key} 为弱口令/示例值，生产禁止")

    # --- 4) 示例密钥 / token 强度 ---------------------------------------
    for key in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
        val = cfg.get(key, "")
        if val and not _is_placeholder(val) and val.strip().lower() in EXAMPLE_MINIO:
            err("MINIO_EXAMPLE", f"{key}={val} 为示例值（minioadmin 等），生产必须强随机")

    for key in ("JWT_SECRET", "INTERNAL_TOKEN", "AI_INTERNAL_TOKEN", "AI_SESSION_OWNER_SALT"):
        val = cfg.get(key, "")
        if val and not _is_placeholder(val) and len(val) < MIN_TOKEN_LEN:
            err("TOKEN_SHORT", f"{key} 长度 {len(val)} < {MIN_TOKEN_LEN}，生产必须强随机且足够长")

    # --- 5) Java↔AI 内部 token 一致性（若两侧都提供了真实值）-----------
    java_tok = cfg.get("AI_INTERNAL_TOKEN", "")
    ai_tok = cfg.get("INTERNAL_TOKEN", "")
    if (java_tok and ai_tok and not _is_placeholder(java_tok) and not _is_placeholder(ai_tok)
            and java_tok != ai_tok):
        err("TOKEN_MISMATCH", "AI_INTERNAL_TOKEN(Java) 与 INTERNAL_TOKEN(AI) 不一致，内部调用将 401")

    return findings


# ---------------------------------------------------------------------------
# 4. compose 端口绑定校验（可选，--compose）
# ---------------------------------------------------------------------------

def check_compose_ports(compose_path: str) -> list[tuple[str, str, str]]:
    """校验 compose 中所有发布端口的宿主绑定必须是 127.0.0.1（生产不直接对外）。"""
    import yaml  # 延迟导入；仅 --compose 时需要

    findings: list[tuple[str, str, str]] = []
    with open(compose_path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    services = doc.get("services") or {}
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        for port in (svc.get("ports") or []):
            spec = port if isinstance(port, str) else str(port)
            # 形如 127.0.0.1:8081:80 / 8081:80 / 3306:3306
            host_part = spec.split(":")[0]
            bound_loopback = host_part in ("127.0.0.1", "localhost", "::1")
            if not bound_loopback:
                findings.append((
                    "ERROR", "PORT_PUBLIC",
                    f"service '{name}' 发布端口 '{spec}' 未绑定回环，生产可能对公网暴露（应改为 127.0.0.1:HOST:CONTAINER）",
                ))
    return findings


# ---------------------------------------------------------------------------
# 5. CLI
# ---------------------------------------------------------------------------

RULES_HELP = """\
检查规则（ERROR 即非零退出）：
  PLACEHOLDER      未替换的 __CHANGE_ME__ 占位符
  MYSQL_TLS_OFF    MYSQL_URL 含 useSSL=false
  MYSQL_TLS_WEAK   MYSQL_URL sslMode=DISABLED/PREFERRED
  MYSQL_TLS_MISSING 未启用证书校验（需 VERIFY_IDENTITY / VERIFY_CA / requireSSL=true）
  MYSQL_PUBKEY     allowPublicKeyRetrieval=true（WARN，需 VERIFY_IDENTITY 兜底）
  H2_DB / H2_PROFILE  H2 内存库或 h2 profile
  VECTOR_STORE     内存向量库（memory/faiss/…）
  DB_USER_DEFAULT  MYSQL_USER 为 root/sa/admin 等默认账号
  WEAK_SECRET      弱口令 / 示例口令
  MINIO_EXAMPLE    MinIO 示例凭据 minioadmin 等
  TOKEN_SHORT      JWT/内部 token 长度 < 32
  TOKEN_MISMATCH   AI_INTERNAL_TOKEN 与 INTERNAL_TOKEN 不一致
  PORT_PUBLIC      （--compose，需 PyYAML）发布端口未绑定 127.0.0.1；缺 PyYAML 时以退出码 2 报错
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生产配置部署前体检（发现关闭 TLS/默认账号/H2/示例密钥即非零退出）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=RULES_HELP,
    )
    parser.add_argument("env_file", nargs="?", default=".env", help="要校验的 env 文件（默认 .env）")
    parser.add_argument("--strict", action="store_true", help="将 WARN 也视为失败")
    parser.add_argument("--compose", metavar="FILE", help="额外校验 compose 文件的端口绑定")
    parser.add_argument("--list", action="store_true", help="仅列出检查规则后退出")
    args = parser.parse_args(argv)

    if args.list:
        sys.stdout.write(RULES_HELP)
        return 0

    if not os.path.isfile(args.env_file):
        sys.stderr.write(f"[config-preflight] 错误：找不到配置文件 {args.env_file}\n")
        sys.stderr.write("  用法：python scripts/config-preflight.py <env文件> [--strict] [--compose FILE]\n")
        return 2

    try:
        cfg = parse_env_file(args.env_file)
    except OSError as exc:
        sys.stderr.write(f"[config-preflight] 读取失败：{exc}\n")
        return 2

    findings = check_config(cfg)

    if args.compose:
        if not os.path.isfile(args.compose):
            sys.stderr.write(f"[config-preflight] 错误：找不到 compose 文件 {args.compose}\n")
            return 2
        # ⚠️ 显式指定 --compose 时**不得静默降级**：缺 PyYAML / 解析失败都视为环境错误并非零退出，
        #    否则 CI 可能漏掉端口暴露校验却显示通过。
        try:
            findings += check_compose_ports(args.compose)
        except ImportError:
            sys.stderr.write("[config-preflight] 错误：显式指定 --compose 但当前解释器缺少 PyYAML，无法校验端口绑定。\n")
            sys.stderr.write("  处理：安装 PyYAML（pip install pyyaml）或改用含 yaml 的解释器，例如：\n")
            sys.stderr.write("        D:/ProgramData/anaconda3/python.exe scripts/config-preflight.py .env --compose docker-compose.yml\n")
            return 2
        except Exception as exc:  # noqa: BLE001 - compose 解析/结构异常一律视为环境错误
            sys.stderr.write(f"[config-preflight] 错误：compose 校验失败（{exc}）\n")
            return 2

    errors = [f for f in findings if f[0] == "ERROR"]
    warns = [f for f in findings if f[0] == "WARN"]

    print(f"[config-preflight] 目标配置：{args.env_file}（{len(cfg)} 项）")
    for sev, code, msg in findings:
        print(f"  [{sev}] {code}: {msg}")
    if not findings:
        print("  ✅ 未发现放行阻塞项")

    print(f"[config-preflight] 结果：ERROR={len(errors)} WARN={len(warns)}")
    if errors:
        print("[config-preflight] ❌ 不通过：存在 ERROR，禁止用于生产发布")
        return 1
    if warns and args.strict:
        print("[config-preflight] ❌ 不通过（--strict）：存在 WARN")
        return 1
    print("[config-preflight] ✅ 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
