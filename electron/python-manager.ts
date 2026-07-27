/**
 * Python 进程管理器
 *
 * 职责:
 * 1. 启动 FastAPI 服务器 (python server.py)
 * 2. 运行数据脚本
 * 3. 进程健康检查 + 自动重启
 * 4. 退出时清理
 */

import { ChildProcess, spawn, execFileSync } from "child_process";
import * as path from "path";
import * as http from "http";
import * as fs from "fs";

interface RunResult {
  success: boolean;
  stdout?: string;
  stderr?: string;
  exitCode: number | null;
}

interface PythonPaths {
  resourceStaticDir: string;
  dataDir: string;
  userDataDir: string;
}

export class PythonManager {
  private serverProcess: ChildProcess | null = null;
  private projectRoot: string;
  private paths: PythonPaths;
  private pythonCmd: string;

  constructor(projectRoot: string, paths: PythonPaths) {
    this.projectRoot = projectRoot;
    this.paths = paths;
    this.pythonCmd = this.findPython();
  }

  private getPythonEnv(): NodeJS.ProcessEnv {
    return {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      PYTHONDONTWRITEBYTECODE: "1",
      STOCK_FINANCE_RESOURCE_DIR: this.paths.resourceStaticDir,
      STOCK_FINANCE_DATA_DIR: this.paths.dataDir,
      STOCK_FINANCE_USER_DATA_DIR: this.paths.userDataDir,
    };
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
      "python3",
      "python",
    ];
    const dependencyCheck = "import fastapi, uvicorn, akshare, pandas, requests, httpx, openpyxl, baostock; from akshare.stock.stock_share_changes_cninfo import py_mini_racer";
    let firstExecutable: string | null = null;
    for (const cmd of Array.from(new Set(candidates))) {
      try {
        execFileSync(cmd, ["--version"], { stdio: "pipe" });
        firstExecutable ||= cmd;
      } catch {
        continue;
      }
      try {
        execFileSync(cmd, ["-c", dependencyCheck], {
          cwd: this.projectRoot,
          stdio: "pipe",
          timeout: 10000,
        });
        return cmd;
      } catch {
        continue;
      }
    }
    return firstExecutable || "python3";
  }

  /** 启动 FastAPI 服务器，返回 {success, error} */
  async start(port: number): Promise<{success: boolean; error?: string}> {
    console.log(`Starting Python backend on port ${port}...`);

    // 先检查端口是否已被占用 (可能之前没正常退出)
    const existingReady = await this.checkPort(port);
    if (existingReady) {
      const sameRoot = await this.checkExistingBackendRoot(port);
      if (sameRoot) {
        console.log("Python backend is already running");
        return { success: true };
      }
      return {
        success: false,
        error: `端口 ${port} 已被其他行业热力图后端占用。请先退出旧版行业热力图，或在终端运行：lsof -tiTCP:${port} -sTCP:LISTEN | xargs kill`,
      };
    }

    // 检查核心依赖（不检查 anthropic/openai/mcp，AI 功能可降级）
    try {
      execFileSync(this.pythonCmd, ["-c", "import fastapi, uvicorn, akshare, pandas, requests, httpx, openpyxl, baostock; from akshare.stock.stock_share_changes_cninfo import py_mini_racer"], {
        cwd: this.projectRoot,
        stdio: "pipe",
        timeout: 10000,
      });
    } catch (e: any) {
      const stderr = e.stderr?.toString() || e.message || "";
      // 检查是否存在面向用户的依赖安装脚本
      var setupPath = path.join(this.projectRoot, "安装运行依赖.command");
      var setupHint = "";
      try { if (fs.existsSync(setupPath)) setupHint = `\n\n或双击“安装运行依赖.command”一键安装依赖`; } catch(_) {}
      return { success: false, error: `Python 核心依赖缺失:\n${stderr.slice(0, 300)}\n\n请在终端运行:\n${this.pythonCmd} -m pip install fastapi uvicorn akshare mini-racer pandas requests httpx openpyxl baostock\n\nAI 功能（日报/问答）还需要:\n${this.pythonCmd} -m pip install anthropic openai${setupHint}` };
    }

    this.serverProcess = spawn(this.pythonCmd, ["server.py"], {
      cwd: this.projectRoot,
      stdio: ["pipe", "pipe", "pipe"],
      env: this.getPythonEnv(),
    });

    // The packaged GUI has no terminal consuming stdout. Always drain this
    // pipe; otherwise frequent intraday logs eventually fill the OS buffer
    // and block the Python event loop, making every HTTP request hang.
    this.serverProcess.stdout?.on("data", () => {});

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

  /** 检查端口上的现有服务是否来自同一份应用资源 */
  private checkExistingBackendRoot(port: number): Promise<boolean> {
    return new Promise((resolve) => {
      const req = http.get(`http://localhost:${port}/api/runtime-info`, { timeout: 3000 }, (res) => {
        let data = "";
        res.on("data", (chunk) => { data += chunk; });
        res.on("end", () => {
          try {
            const info = JSON.parse(data);
            const existingRoot = fs.realpathSync(info.project_root || "");
            const existingDataDir = fs.realpathSync(info.data_dir || "");
            const currentRoot = fs.realpathSync(this.projectRoot);
            const currentDataDir = fs.realpathSync(this.paths.dataDir);
            resolve(existingRoot === currentRoot && existingDataDir === currentDataDir);
          } catch {
            resolve(false);
          }
        });
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
        env: this.getPythonEnv(),
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
