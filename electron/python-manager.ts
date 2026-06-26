/**
 * Python 进程管理器
 *
 * 职责:
 * 1. 启动 FastAPI 服务器 (python server.py)
 * 2. 运行数据脚本
 * 3. 进程健康检查 + 自动重启
 * 4. 退出时清理
 */

import { ChildProcess, spawn, execSync, execFileSync } from "child_process";
import * as path from "path";
import * as http from "http";

interface RunResult {
  success: boolean;
  stdout?: string;
  stderr?: string;
  exitCode: number | null;
}

export class PythonManager {
  private serverProcess: ChildProcess | null = null;
  private projectRoot: string;
  private pythonCmd: string;

  constructor(projectRoot: string) {
    this.projectRoot = projectRoot;
    this.pythonCmd = this.findPython();
  }

  /** 查找可用的 Python（优先用 pip 安装了依赖的） */
  private findPython(): string {
    const candidates = [
      "/opt/anaconda3/bin/python3",
      "/opt/homebrew/bin/python3",
      "/usr/local/bin/python3",
      "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
      "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
      "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
    ];
    // 先试具体路径，再试 PATH 中的
    for (const cmd of candidates) {
      try {
        execFileSync(cmd, ["--version"], { stdio: "pipe" });
        return cmd;
      } catch {
        continue;
      }
    }
    for (const cmd of ["python3", "python"]) {
      try {
        execSync(`${cmd} --version`, { stdio: "pipe" });
        return cmd;
      } catch {
        continue;
      }
    }
    return "python3";
  }

  /** 启动 FastAPI 服务器，返回 {success, error} */
  async start(port: number): Promise<{success: boolean; error?: string}> {
    console.log(`Starting Python backend on port ${port}...`);

    // 先检查端口是否已被占用 (可能之前没正常退出)
    const existingReady = await this.checkPort(port);
    if (existingReady) {
      console.log("Python backend is already running");
      return { success: true };
    }

    // 先测试 python 是否能正常导入关键模块
    try {
      execFileSync(this.pythonCmd, ["-c", "import fastapi, uvicorn, akshare, pandas, anthropic, openai, mcp"], {
        cwd: this.projectRoot,
        stdio: "pipe",
        timeout: 10000,
      });
    } catch (e: any) {
      const stderr = e.stderr?.toString() || e.message || "";
      return { success: false, error: `Python 依赖缺失:\n${stderr.slice(0, 300)}\n\n请运行: pip3 install akshare baostock pywencai httpx uvicorn fastapi pandas openpyxl pillow anthropic openai mcp` };
    }

    this.serverProcess = spawn(this.pythonCmd, ["server.py"], {
      cwd: this.projectRoot,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
    });

    // 收集 stderr 用于调试
    let startupStderr = "";
    const stderrCollector = (data: Buffer) => {
      startupStderr += data.toString();
    };
    this.serverProcess.stderr?.on("data", stderrCollector);

    // 等待服务器就绪
    const ready = await this.waitForReady(port, 15000);
    if (!ready) {
      console.error("Python backend failed to start");
      console.error("Stderr output:", startupStderr.slice(0, 500));
      this.serverProcess?.kill();
      this.serverProcess = null;
      return { success: false, error: `Python 进程退出:\n${startupStderr.slice(0, 300) || "(无输出)"}` };
    }
    // 移除收集器，后续 stderr 由正常日志处理
    this.serverProcess.stderr?.removeListener("data", stderrCollector);

    console.log("Python backend is ready");

    // 监听进程退出
    this.serverProcess.on("exit", (code) => {
      console.log(`Python backend exited with code ${code}`);
      this.serverProcess = null;
    });

    this.serverProcess.stderr?.on("data", (data) => {
      console.error(`[python] ${data.toString().trim()}`);
    });

    return { success: true };
  }

  /** 快速检查端口是否已存在运行中的服务 */
  private checkPort(port: number): Promise<boolean> {
    return new Promise((resolve) => {
      const req = http.get(`http://localhost:${port}/app.html`, { timeout: 3000 }, (res) => {
        resolve(res.statusCode === 200);
      });
      req.on("error", () => resolve(false));
      req.on("timeout", () => { req.destroy(); resolve(false); });
    });
  }

  /** 等待 HTTP 服务就绪 */
  private waitForReady(port: number, timeoutMs: number): Promise<boolean> {
    return new Promise((resolve) => {
      const start = Date.now();
      const check = () => {
        if (Date.now() - start > timeoutMs) {
          resolve(false);
          return;
        }
        const req = http.get(`http://localhost:${port}/app.html`, { timeout: 2000 }, (res) => {
          if (res.statusCode === 200) {
            resolve(true);
          } else {
            setTimeout(check, 500);
          }
        });
        req.on("error", () => setTimeout(check, 500));
        req.on("timeout", () => { req.destroy(); setTimeout(check, 500); });
      };
      check();
    });
  }

  /** 执行 Python 数据脚本 */
  runScript(script: string, args: string[] = []): Promise<RunResult> {
    return new Promise((resolve) => {
      const child = spawn(this.pythonCmd, [script, ...args], {
        cwd: this.projectRoot,
        stdio: ["pipe", "pipe", "pipe"],
        env: { ...process.env, PYTHONUNBUFFERED: "1" },
      });

      let stdout = "";
      let stderr = "";

      child.stdout?.on("data", (data) => {
        const text = data.toString();
        stdout += text;
        process.stdout.write(text);
      });

      child.stderr?.on("data", (data) => {
        const text = data.toString();
        stderr += text;
        process.stderr.write(text);
      });

      child.on("close", (code) => {
        resolve({
          success: code === 0,
          stdout,
          stderr,
          exitCode: code,
        });
      });

      child.on("error", (err) => {
        resolve({
          success: false,
          stderr: err.message,
          exitCode: null,
        });
      });
    });
  }

  /** 检查服务器是否在运行 */
  isRunning(): boolean {
    return this.serverProcess !== null && !this.serverProcess.killed;
  }

  /** 停止 Python 后端 */
  stop(): void {
    const proc = this.serverProcess;
    if (proc) {
      console.log("Stopping Python backend...");
      this.serverProcess = null;
      proc.kill("SIGTERM");
      setTimeout(() => {
        if (!proc.killed) {
          proc.kill("SIGKILL");
        }
      }, 5000);
    }
  }
}
