#!/usr/bin/env python3
"""Web UI 入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from agent_runtime.ui_server import run_ui_server


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 learn-claude-code 的 Web UI。")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    args = parser.parse_args()

    run_ui_server(repo_root=Path(__file__).resolve().parent, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
