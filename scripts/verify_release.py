#!/usr/bin/env python3
"""GGUF-Manager 交付前版本一致性校验（含 .env 派生）。

唯一版本来源是 app/__init__.py 的四个字段：
    __version__ / __release_status__ / __test_sequence__ / __docker_tag__

用法：
    python scripts/verify_release.py            # 校验 + 从 __init__.py 派生/更新 .env
    python scripts/verify_release.py --check   # 只校验，不写任何文件

失败时退出码 1，并逐条列出原因。
"""

from __future__ import annotations

import re
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "app" / "__init__.py"
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
CHANGELOG = ROOT / "CHANGELOG.md"
# .env 供本机 compose 插值（通常被 .gitignore 忽略）；.env.example 随仓库提交，
# 克隆后跑一次本脚本即可生成 .env。两者都是 app/__init__.py 的派生产物。
ENV_FILES = (ROOT / ".env", ROOT / ".env.example")
FIELDS = ("__version__", "__release_status__", "__test_sequence__", "__docker_tag__")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def main() -> int:
    errors: list[str] = []
    check_only = "--check" in sys.argv

    if not INIT.exists():
        print("[FAIL] app/__init__.py 不存在")
        return 1
    meta = runpy.run_path(str(INIT))

    missing = [f for f in FIELDS if f not in meta]
    if missing:
        print("[FAIL] app/__init__.py 缺少字段：" + ", ".join(missing))
        return 1

    version = meta["__version__"]
    status = meta["__release_status__"]
    seq = meta["__test_sequence__"]
    tag = meta["__docker_tag__"]

    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        errors.append(f"__version__ 格式非法：{version!r}（应为 x.y.z）")
    if status not in ("test", "formal"):
        errors.append(f"__release_status__ 只能为 test / formal，当前 {status!r}")
    if not isinstance(seq, int) or seq < 0:
        errors.append(f"__test_sequence__ 必须是非负整数，当前 {seq!r}")

    expected = f"{version}-test.{seq}" if status == "test" else version
    if tag != expected:
        errors.append(f"__docker_tag__={tag!r} 与派生值 {expected!r} 不一致")
    if status == "formal" and (seq != 0 or "test" in tag):
        errors.append("正式版必须 __test_sequence__=0 且 tag 不含测试标识")

    dockerfile = read(DOCKERFILE)
    if "ARG APP_VERSION" not in dockerfile:
        errors.append("Dockerfile 缺少 ARG APP_VERSION")
    if "__docker_tag__" not in dockerfile:
        errors.append("Dockerfile 缺少与 app/__init__.py 的构建期比对")
    if 'LABEL org.opencontainers.image.version="${APP_VERSION}"' not in dockerfile:
        errors.append("Dockerfile 缺少由 APP_VERSION 派生的版本 LABEL")

    compose = read(COMPOSE)
    active_images = [
        ln.strip() for ln in compose.splitlines() if ln.strip().startswith("image:")
    ]
    if not any("gguf-manager:${GGUF_DOCKER_TAG}" in ln for ln in active_images):
        errors.append("Compose 镜像名未从 ${GGUF_DOCKER_TAG} 派生（或被注释）")
    if "APP_VERSION: ${GGUF_DOCKER_TAG}" not in compose:
        errors.append("Compose 构建参数未从 ${GGUF_DOCKER_TAG} 派生")
    if status == "test" and ":latest" in compose:
        errors.append("测试版 Compose 残留 :latest 干净标签（仅正式版允许）")
    hardcoded = re.findall(r"image:\s*\S*:\d+\.\d+\.\d+", compose)
    if hardcoded:
        errors.append(f"Compose 存在硬编码版本 tag：{hardcoded}")

    changelog = read(CHANGELOG)
    first_entry = re.search(r"^##\s*\[([^\]]+)\]", changelog, re.M)
    want_entry = tag if status == "test" else version
    if not first_entry or first_entry.group(1) != want_entry:
        got = first_entry.group(1) if first_entry else "未找到"
        errors.append(f"CHANGELOG 顶部第一条应为 [{want_entry}]，实际 [{got}]")

    if not errors or check_only:
        for path in ENV_FILES:
            text = read(path)
            match = re.search(r"^GGUF_DOCKER_TAG=(.*)$", text, re.M)
            value = match.group(1).strip() if match else None
            if check_only:
                if value != tag:
                    errors.append(
                        f"{path.name} 的 GGUF_DOCKER_TAG={value!r} 与派生值 {tag!r} "
                        "不一致（--check 不自动修复）"
                    )
                continue
            if value == tag:
                continue
            lines = [
                ln for ln in text.splitlines()
                if ln.strip()
                and not ln.startswith("GGUF_DOCKER_TAG=")
                and not ln.startswith("# 由 scripts/verify_release.py")
            ]
            lines.append("# 由 scripts/verify_release.py 从 app/__init__.py 自动派生，请勿手工改版本")
            lines.append(f"GGUF_DOCKER_TAG={tag}")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"[write] {path.name} 已派生：GGUF_DOCKER_TAG={tag}")

    if errors:
        print("[FAIL] 版本一致性校验未通过：")
        for e in errors:
            print("  - " + e)
        return 1

    print("[OK] 版本一致性校验通过")
    print(f"     状态：{'测试版 #' + str(seq) if status == 'test' else '正式版 ' + version}")
    print(f"     版本：{version}    tag：{tag}")
    print("     Dockerfile / Compose / .env / CHANGELOG 均由 app/__init__.py 派生")
    return 0


if __name__ == "__main__":
    sys.exit(main())
