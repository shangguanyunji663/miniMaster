"""从已有项目同步 LLM 配置到 miniMaster/.env（一次性工具，不含任何密钥字面量）。

用法：
    python scripts/sync_env.py --src "D:/PythonProject/DeepResearchSearch/backend/.env"

只提取下列变量；已存在的 .env 中同名变量会被覆盖，其余行保留。
"""
import argparse
from pathlib import Path

WANTED = [
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
]


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key in WANTED and val:
            values[key] = val
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True, help="源 .env 文件路径")
    parser.add_argument("--dst", default=str(Path(__file__).resolve().parent.parent / ".env"))
    args = parser.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)
    if not src.exists():
        raise SystemExit(f"源文件不存在: {src}")

    found = parse_env_file(src)
    missing = [k for k in WANTED if k not in found and k != "LLM_MAX_TOKENS"]
    if missing:
        raise SystemExit(f"源文件缺少必需变量: {missing}")

    # 保留 dst 中已有的其他行（如手动调过的 Harness 预算参数）
    existing_other: list[str] = []
    if dst.exists():
        for raw in dst.read_text(encoding="utf-8", errors="replace").splitlines():
            key = raw.partition("=")[0].strip()
            if raw.strip() and not raw.strip().startswith("#") and key not in WANTED:
                existing_other.append(raw)

    header = [
        "# miniMaster 运行配置 —— 含密钥，勿提交/外传（已在 .gitignore 忽略）",
        f"# 由 scripts/sync_env.py 从 {src} 同步",
        "",
    ]
    lines = header + [f"{k}={found[k]}" for k in WANTED if k in found]
    if existing_other:
        lines += ["", "# ---- 本项目自有配置 ----"] + existing_other
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入 {dst}（{len(found)} 个变量，密钥不回显）")


if __name__ == "__main__":
    main()
