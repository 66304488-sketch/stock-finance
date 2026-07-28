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
  /** 端口被同应用实例占用时收养该后端（无进程句柄） */
  private adoptedExisting = false;
  /** 主动 stop 后置位，阻止 exit 监听器触发自动重启 */
  private intentionalStop = false;
  private restartAttempts = 0;
  private port = 8001;
  /** 主进程通知回调（放弃自动重启时告知用户） */
  onNotify?: (title: string, body: string) => void;

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
    this.port = port;
    this.intentionalStop = false;

    // 先检查端口是否已被占用 (可能之前没正常退出)
    const portState = await this.checkPort(port);
    if (portState === "ready") {
      const sameRoot = await this.checkExistingBackendRoot(port);
      if (sameRoot) {
        console.log("Python backend is already running");
        this.adoptedExisting = true;
        this.restartAttempts = 0;
        return { success: true };
      }
      return {
        success: false,
        error: `端口 ${port} 已被其他行业热力图后端占用。请先退出旧版行业热力图，或在终端运行：lsof -tiTCP:${port} -sTCP:LISTEN | xargs kill`,
      };
    }
    if (portState === "occupied") {
      return {
        success: false,
        error: `端口 ${port} 被一个无响应的进程占用（监听中但不回应请求，可能是挂死的旧后端）。请先结束占用进程：lsof -tiTCP:${port} -sTCP:LISTEN | xargs kill`,
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
    this.restartAttempts = 0;

    // 监听进程退出：非主动停止的异常退出走自动重启（带退避）
    this.serverProcess.on("exit", (code) => {
      console.log(`Python backend exited with code ${code}`);
      this.serverProcess = null;
      if (!this.intentionalStop) {
        this.scheduleRestart();
      }
    });

    this.serverProcess.stderr?.on("data", (data) => {
      console.error(`[python] ${data.toString().trim()}`);
    });

    return { success: true };
  }

  /** 快速检查端口状态：ready=服务正常响应，occupied=监听中但无响应（挂死），free=无监听 */
  private checkPort(port: number): Promise<"ready" | "occupied" | "free"> {
    return new Promise((resolve) => {
      const req = http.get(`http://localhost:${port}/app.html`, { timeout: 3000 }, (res) => {
        resolve(res.statusCode === 200 ? "ready" : "occupied");
      });
      req.on("error", (err: NodeJS.ErrnoException) => {
        // 连接被拒绝 = 端口无监听 = 空闲；其余错误按占用处理
        resolve(err.code === "ECONNREFUSED" ? "free" : "occupied");
      });
      // 连接成功但 3 秒内无响应 = 被挂死进程占用
      req.on("timeout", () => { req.destroy(); resolve("occupied"); });
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

  /** 崩溃后自动重启：最多 3 次，间隔递增 (5s/10s/15s)，超过后通知用户 */
  private scheduleRestart(): void {
    if (this.restartAttempts >= 3) {
      this.onNotify?.("Python 后端", "后端多次崩溃，已停止自动重启，请重新打开应用");
      return;
    }
    this.restartAttempts += 1;
    const delayMs = this.restartAttempts * 5000;
    console.log(`Backend crashed; restarting in ${delayMs / 1000}s (attempt ${this.restartAttempts}/3)`);
    setTimeout(() => {
      if (this.intentionalStop || this.serverProcess) return;
      void this.start(this.port).then((result) => {
        if (!result.success) {
          console.error("Backend restart failed:", result.error);
        }
      });
    }, delayMs);
  }

  /** 检查服务器是否在运行（自己拉起的进程存活，或已收养同应用实例的既有后端） */
  isRunning(): boolean {
    return this.adoptedExisting || (this.serverProcess !== null && !this.serverProcess.killed);
  }

  /** 停止 Python 后端 */
  stop(): void {
    this.intentionalStop = true;
    this.adoptedExisting = false;
    const proc = this.serverProcess;
    if (proc) {
      console.log("Stopping Python backend...");
      this.serverProcess = null;
      // proc.killed 只表示信号已发出而非进程已死，必须用 exit 事件确认
      let exited = false;
      proc.once("exit", () => { exited = true; });
      proc.kill("SIGTERM");
      setTimeout(() => {
        if (!exited) {
          proc.kill("SIGKILL");
        }
      }, 5000);
    }
  }
}
