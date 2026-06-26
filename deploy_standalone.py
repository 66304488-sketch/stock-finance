"""
自动部署脚本: 将 standalone HTML 推送到 GitHub Pages
- 在数据更新 + standalone 生成后运行
- git add + commit + push

使用: python deploy_standalone.py [--message "update data"]
"""

import subprocess
import sys
import os
from datetime import datetime


def run(cmd: list[str]) -> tuple[int, str, str]:
    """运行 shell 命令并返回 (exit_code, stdout, stderr)"""
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(__file__))
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def main():
    msg = " ".join(sys.argv[2:]) if len(sys.argv) > 2 and sys.argv[1] == "--message" else ""
    if not msg:
        today = datetime.now().strftime("%Y-%m-%d")
        msg = f"更新数据至{today}"

    standalone = os.path.join(os.path.dirname(__file__), "static", "industry-heatmap-standalone.html")

    if not os.path.exists(standalone):
        print(f"错误: {standalone} 不存在，请先运行 generate_standalone.py")
        sys.exit(1)

    print(f"部署: {msg}")

    # 1. git add
    code, out, err = run(["git", "add", "static/industry-heatmap-standalone.html"])
    if code != 0:
        print(f"  git add 失败: {err}")
        sys.exit(1)
    print("  ✅ git add")

    # 2. git commit
    code, out, err = run(["git", "commit", "-m", msg])
    if code != 0:
        if "nothing to commit" in err:
            print("  ⚠️ 没有变更需要提交")
            return
        print(f"  git commit 失败: {err}")
        sys.exit(1)
    print(f"  ✅ git commit: {msg}")

    # 3. git push
    code, out, err = run(["git", "push", "origin", "master"])
    if code != 0:
        print(f"  git push 失败: {err}")
        print("  可能需要手动推送，或检查网络/git配置")
        sys.exit(1)
    print("  ✅ git push")

    print(f"\n部署完成！手机访问: https://github.com/你的用户名/stock-finance/blob/master/static/industry-heatmap-standalone.html")


if __name__ == "__main__":
    main()
