#!/usr/bin/env python3
"""
Windsurf/Devin API Key 提取工具（跨平台：macOS / Windows / Linux）

从 Windsurf/Devin 本地安装中提取 API Key，无需额外依赖。

用法:
  python src/extract_key.py                    # 自动检测平台并提取
  python src/extract_key.py --json             # JSON 格式输出
  python src/extract_key.py --db-path /tmp/state.vscdb
  python src/extract_key.py --db-path ~/.local/share/devin/credentials.toml
"""

import json
import os
import platform
import re
import sqlite3
import sys
from pathlib import Path


TOML_API_KEY_FIELDS = (
    "api_key",
    "apiKey",
    "devin_api_key",
    "devinApiKey",
    "windsurf_api_key",
    "windsurfApiKey",
    "access_token",
    "accessToken",
    "token",
)


def get_db_path_candidates(
    system: str | None = None,
    home: Path | None = None,
    env: dict[str, str] | None = None,
) -> list[Path]:
    """获取 Windsurf/Devin state.vscdb 候选路径（跨平台）。"""
    system = system or platform.system()
    home = home or Path.home()
    env = env or os.environ
    app_names = ("Deviv", "Devin", "Windsurf")

    if system == "Darwin":  # macOS
        return [
            home / "Library" / "Application Support" / app_name / "User" / "globalStorage" / "state.vscdb"
            for app_name in app_names
        ]
    if system == "Windows":
        appdata = env.get("APPDATA", "")
        if not appdata:
            raise RuntimeError("无法获取 APPDATA 环境变量")
        return [Path(appdata) / app_name / "User" / "globalStorage" / "state.vscdb" for app_name in app_names]

    config = env.get("XDG_CONFIG_HOME", str(home / ".config"))
    return [Path(config) / app_name / "User" / "globalStorage" / "state.vscdb" for app_name in app_names]


def get_db_path() -> Path:
    """获取首选 Windsurf/Devin state.vscdb 路径（跨平台）。"""
    return get_db_path_candidates()[0]


def get_cli_credential_path_candidates(
    system: str | None = None,
    home: Path | None = None,
) -> list[Path]:
    """获取 Devin CLI credentials.toml 候选路径。WSL/Linux 优先读这里。"""
    system = system or platform.system()
    home = home or Path.home()
    if system != "Linux":
        return []
    return [home / ".local" / "share" / "devin" / "credentials.toml"]


def get_credential_sources() -> list[dict[str, Path | str]]:
    toml_sources = [{"type": "toml", "path": path} for path in get_cli_credential_path_candidates()]
    sqlite_sources = [{"type": "sqlite", "path": path} for path in get_db_path_candidates()]
    return [*toml_sources, *sqlite_sources]


def extract_api_key_from_toml(text: str) -> str:
    """从 Devin CLI credentials.toml 内容中提取 API key。"""
    for field in TOML_API_KEY_FIELDS:
        match = re.search(rf"^\s*{field}\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s#]+))", text, re.MULTILINE)
        if not match:
            continue
        value = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        if value:
            return value

    fallback = re.search(r"\bsk-[A-Za-z0-9_-]+\b", text)
    return fallback.group(0) if fallback else ""


def extract_key_from_toml(credentials_path: str | Path) -> dict:
    credentials_path = Path(credentials_path)
    if not credentials_path.exists():
        return {
            "error": f"Devin CLI credentials 未找到: {credentials_path}",
            "hint": "请先在 WSL/Linux 中运行 devin login。",
            "db_path": str(credentials_path),
            "source_type": "devin_cli_credentials",
        }

    try:
        text = credentials_path.read_text(encoding="utf-8")
    except Exception as e:
        return {
            "error": f"读取 Devin CLI credentials 失败: {e}",
            "db_path": str(credentials_path),
            "source_type": "devin_cli_credentials",
        }

    api_key = extract_api_key_from_toml(text)
    if not api_key:
        return {
            "error": "Devin CLI credentials 中未找到 API key",
            "hint": "请先在 WSL/Linux 中运行 devin login。",
            "db_path": str(credentials_path),
            "source_type": "devin_cli_credentials",
        }

    return {
        "api_key": api_key,
        "db_path": str(credentials_path),
        "source_type": "devin_cli_credentials",
    }


def extract_key_from_db(db_path: str | Path) -> dict:
    db_path = Path(db_path)
    if not db_path.exists():
        return {
            "error": f"Windsurf/Devin 数据库未找到: {db_path}",
            "hint": "请确保 Windsurf 或 Devin 已安装并登录。",
            "db_path": str(db_path),
        }

    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = 'windsurfAuthStatus'"
        ).fetchone()
        conn.close()
    except Exception as e:
        return {"error": f"读取数据库失败: {e}", "db_path": str(db_path)}

    if not row:
        return {
            "error": "未找到 windsurfAuthStatus 记录",
            "hint": "请确保 Windsurf 或 Devin 已登录。",
            "db_path": str(db_path),
        }

    try:
        data = json.loads(row[0])
    except json.JSONDecodeError:
        return {"error": "windsurfAuthStatus 数据解析失败", "db_path": str(db_path)}

    api_key = (data.get("apiKey") or "").strip()
    if not api_key:
        return {"error": "apiKey 字段为空", "db_path": str(db_path)}

    return {"api_key": api_key, "db_path": str(db_path), "source_type": "sqlite"}


def extract_key(db_path: str | Path | None = None) -> dict:
    """
    从 Devin CLI credentials.toml 或 Windsurf/Devin state.vscdb 提取 API Key。

    Returns:
        {"api_key": "...", "db_path": "/path/to/source", "source_type": "..."}
        或 {"error": "..."}
    """
    if db_path is not None:
        db_path = Path(db_path).expanduser()
        if db_path.suffix == ".toml":
            return extract_key_from_toml(db_path)
        return extract_key_from_db(db_path)

    sources = get_credential_sources()
    tried_paths: list[str] = []
    first_existing_error: dict | None = None

    for source in sources:
        source_path = Path(source["path"])
        tried_paths.append(str(source_path))
        if not source_path.exists():
            continue

        result = extract_key_from_toml(source_path) if source["type"] == "toml" else extract_key_from_db(source_path)
        if "api_key" in result:
            return result
        if first_existing_error is None:
            first_existing_error = result

    if first_existing_error is not None:
        return {**first_existing_error, "tried_paths": tried_paths}

    return {
        "error": "未找到 Windsurf/Devin 凭据来源",
        "hint": "请确保 Devin 或 Windsurf 已安装并登录。",
        "db_path": tried_paths[0] if tried_paths else "",
        "tried_paths": tried_paths,
    }

def _parse_db_path(argv: list[str]) -> Path | None:
    if "--db-path" not in argv:
        return None
    idx = argv.index("--db-path")
    try:
        value = argv[idx + 1]
    except IndexError:
        raise SystemExit("Missing value for --db-path")
    return Path(value).expanduser()


def main() -> int:
    json_mode = "--json" in sys.argv
    db_path = _parse_db_path(sys.argv[1:])

    result = extract_key(db_path)

    if "error" in result:
        if json_mode:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[!] {result['error']}")
            if "hint" in result:
                print(f"    {result['hint']}")
            print(f"    数据库路径: {result.get('db_path', 'N/A')}")
        return 1

    api_key = result["api_key"]

    if json_mode:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("[OK] Windsurf/Devin API Key 提取成功")
    print()
    fmt = api_key.split("$", 1)[0] if "$" in api_key else "api-key"
    print(f"  Format: {fmt}")
    print(f"  Key: {api_key}")
    print(f"  Length: {len(api_key)} 字符")
    print(f"  Source: {result['db_path']}")
    if result.get("source_type"):
        print(f"  Source type: {result['source_type']}")
    print()

    system = platform.system()
    if system == "Darwin" or system == "Linux":
        print("配置方法:")
        print()
        print("  1. 环境变量:")
        print(f'     export WINDSURF_API_KEY="{api_key}"')
        print()
        print("  2. 添加到 shell 配置 (~/.zshrc 或 ~/.bashrc):")
        print(f'     echo \'export WINDSURF_API_KEY="{api_key}"\' >> ~/.zshrc')
        print()
        print("  3. 直接调用:")
        print('     python src/fast_context_cli.py search --query "where is auth handled" --project .')
    elif system == "Windows":
        print("配置方法:")
        print()
        print("  1. 环境变量:")
        print(f'     set WINDSURF_API_KEY={api_key}')
        print()
        print("  2. 永久设置:")
        print(f'     setx WINDSURF_API_KEY "{api_key}"')
        print()
        print("  3. 直接调用:")
        print('     python src/fast_context_cli.py search --query "where is auth handled" --project .')

    return 0


if __name__ == "__main__":
    sys.exit(main())
