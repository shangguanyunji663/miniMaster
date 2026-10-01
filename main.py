"""薄壳入口：完整 CLI 在 minimaster.cli（console_script 与本文件共用同一入口）。"""
import sys

from minimaster.cli import main

if __name__ == "__main__":
    sys.exit(main())
