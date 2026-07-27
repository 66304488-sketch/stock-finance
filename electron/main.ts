/**
 * Electron 主进程 - 行业热力图 macOS Desktop App
 *
 * 职责:
 * 1. 管理 Python FastAPI 后端进程
 * 2. 创建 BrowserWindow 加载热力图
 * 3. 托盘图标 + Dock 菜单
 * 4. 定时数据更新调度
 */

import { app, BrowserWindow, Menu, Tray, nativeImage, Notification, dialog, shell, ipcMain } from "electron";
import * as path from "path";
import * as http from "http";
import { PythonManager } from "./python-manager";
import { Scheduler } from "./scheduler";

const PYTHON_PORT = 8001;
const BASE_URL = `http://localhost:${PYTHON_PORT}`;

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let pythonManager: PythonManager;
let scheduler: Scheduler;

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: "行业热力图",
    backgroundColor: "#1a1d23",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadURL(`${BASE_URL}/app.html`).catch((err: Error) => {
    console.error("Failed to load app:", err.message);
  });

  // 外部链接（同花顺等）用系统默认浏览器打开
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://") || url.startsWith("https://")) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });

  mainWindow.once("ready-to-show", () => {
    mainWindow?.show();
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function createMenu(): void {
  const template: Electron.MenuItemConstructorOptions[] = [
    {
      label: "行业热力图",
      submenu: [
        { role: "about" },
        { type: "separator" },
        { label: "刷新数据", click: () => runDataPipeline(1, true) },
        { type: "separator" },
        { role: "quit" },
      ],
    },
    {
      label: "编辑",
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "selectAll" },
      ],
    },
    {
      label: "视图",
      submenu: [
        { role: "reload" },
        { role: "toggleDevTools" },
        { type: "separator" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { role: "resetZoom" },
      ],
    },
  ];

  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);
}

function createTray(): void {
  const icon = nativeImage.createFromPath(path.join(__dirname, "..", "..", "resources", "icon.png"));
  tray = new Tray(icon.resize({ width: 16, height: 16 }));
  tray.setToolTip("行业热力图");

  const showOrCreate = () => {
    if (mainWindow) {
      mainWindow.show();
    } else {
      createWindow();
    }
  };
  const contextMenu = Menu.buildFromTemplate([
    { label: "显示窗口", click: showOrCreate },
    { label: "刷新数据", click: () => runDataPipeline(1, true) },
    { type: "separator" },
    { label: "退出", click: () => app.quit() },
  ]);
  tray.setContextMenu(contextMenu);
  tray.on("click", showOrCreate);
}

function postRefresh(days = 1): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.request(
      {
        hostname: "localhost",
        port: PYTHON_PORT,
        path: "/api/refresh-data",
        method: "POST",
        timeout: 5000,
        headers: { "Content-Type": "application/json" },
      },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      }
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.end(JSON.stringify({ days, mode: "auto" }));
  });
}

function postIntradayScan(): Promise<void> {
  return new Promise((resolve) => {
    const req = http.request(
      {
        hostname: "localhost",
        port: PYTHON_PORT,
        path: "/api/intraday-scan",
        method: "POST",
        timeout: 5000,
        headers: { "Content-Type": "application/json" },
      },
      (res) => {
        res.resume();
        resolve();
      },
    );
    req.on("error", () => resolve());
    req.on("timeout", () => {
      req.destroy();
      resolve();
    });
    req.end(JSON.stringify({ window: "all", scheme: "all" }));
  });
}

function getBackendJson(pathname: string): Promise<any | null> {
  return new Promise((resolve) => {
    const req = http.get(`${BASE_URL}${pathname}`, { timeout: 5000 }, (res) => {
      let data = "";
      res.on("data", (chunk) => { data += chunk; });
      res.on("end", () => {
        try {
          resolve(JSON.parse(data));
        } catch {
          resolve(null);
        }
      });
    });
    req.on("error", () => resolve(null));
    req.on("timeout", () => { req.destroy(); resolve(null); });
  });
}

async function needsCatchUp(): Promise<boolean> {
  const session = await getBackendJson("/api/market-session");
  const awaitingTodayClose = session
    && ["trading", "lunch", "awaiting_close"].includes(session.phase)
    && !session.close_confirmed;
  if (awaitingTodayClose) return false;

  const result = await getBackendJson("/api/refresh-data/check");
  if (!result) return false;
  const datasets = result?.datasets || {};
  return ["highs", "lows", "capital_flow"].some(
    (name) => datasets[name]?.status !== "up_to_date"
  );
}

interface RefreshStatus {
  running: boolean;
  success?: boolean;
  error?: string;
  current_step?: string;
  pollTimedOut?: boolean;
}

function fetchRefreshStatus(): Promise<RefreshStatus | null> {
  return new Promise((resolve) => {
    const req = http.get(`${BASE_URL}/api/refresh-data/status`, { timeout: 5000 }, (res) => {
      let data = "";
      res.on("data", (chunk) => { data += chunk; });
      res.on("end", () => {
        try {
          resolve(JSON.parse(data) as RefreshStatus);
        } catch {
          resolve(null);
        }
      });
    });
    req.on("error", () => resolve(null));
    req.on("timeout", () => {
      req.destroy();
      resolve(null);
    });
  });
}

function pollRefreshStatus(maxMs = 1200000): Promise<RefreshStatus> {
  return new Promise((resolve) => {
    const start = Date.now();
    let lastStep = "";
    const check = async () => {
      // Always query once after wake/resume before evaluating elapsed wall time.
      const status = await fetchRefreshStatus();
      if (status && !status.running) {
        resolve(status);
        return;
      }
      if (status?.current_step && status.current_step !== lastStep) {
        lastStep = status.current_step;
        console.log(`[Scheduler] Data update step: ${lastStep}`);
      }
      if (Date.now() - start > maxMs) {
        resolve(status
          ? { ...status, pollTimedOut: true }
          : { running: false, success: undefined, pollTimedOut: true, error: "暂时无法确认后台更新状态" });
        return;
      }
      setTimeout(check, 2000);
    };
    void check();
  });
}

/**
 * 执行完整数据更新流水线（通过后端 API，避免在 Electron 主进程重复实现合并逻辑）
 */
let pipelineRunning = false;

async function runDataPipeline(days = 1, interactive = false): Promise<void> {
  if (pipelineRunning) {
    notify("数据更新", "已有刷新任务在运行中");
    return;
  }
  if (!pythonManager.isRunning()) {
    notify("错误", "Python 后端未运行");
    return;
  }

  pipelineRunning = true;
  notify("数据更新", "启动后台刷新...");
  try {
    const started = await postRefresh(days);
    if (!started) {
      const message = "无法启动数据刷新，请检查后端服务";
      notify("错误", message);
      if (!interactive) throw new Error(message);
      return;
    }
    const status = await pollRefreshStatus(1200000);
    if (status.success) {
      notify("数据更新", "✅ 数据更新完成");
      mainWindow?.webContents.reload();
    } else if (status.pollTimedOut) {
      const message = status.running
        ? "数据仍在后台更新，完成后页面会自动读取最新结果"
        : "暂时无法确认更新状态，稍后可在设置页查看最终结果";
      notify("数据更新", message);
    } else {
      const err = status.error || "未知错误";
      notify("更新失败", err);
      if (interactive) dialog.showErrorBox("数据更新失败", err);
      else throw new Error(err);
    }
  } finally {
    pipelineRunning = false;
  }
}

function notify(title: string, body: string): void {
  if (Notification.isSupported()) {
    const n = new Notification({ title, body });
    n.show();
  }
}

// ──── App 生命周期 ────────────────────────────────────────────────

app.whenReady().then(async () => {
  createMenu();

  // IPC: 打开文件夹对话框
  ipcMain.handle("select-dir", async () => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ["openDirectory", "createDirectory"],
      title: "选择备份文件夹",
    });
    return result.canceled ? null : result.filePaths[0];
  });

  // IPC: 在 Finder 中打开路径（打包版只允许 app 自己的 userData 目录）
  ipcMain.handle("open-path", async (_e, target: string) => {
    const resolved = path.resolve(String(target || ""));
    if (app.isPackaged) {
      const base = path.resolve(app.getPath("userData"));
      if (resolved !== base && !resolved.startsWith(base + path.sep)) return "restricted";
    }
    return shell.openPath(resolved);
  });

  // 打包后用 Resources 目录（Python 脚本在 asar 外），开发模式用项目根目录
  const projectRoot = app.isPackaged
    ? process.resourcesPath
    : path.join(__dirname, "..", "..");
  const resourceStaticDir = path.join(projectRoot, "static");
  const userDataDir = app.getPath("userData");
  const dataDir = app.isPackaged ? path.join(userDataDir, "data") : resourceStaticDir;
  console.log("Project root:", projectRoot, "data dir:", dataDir, "isPackaged:", app.isPackaged);
  pythonManager = new PythonManager(projectRoot, {
    resourceStaticDir,
    dataDir,
    userDataDir: app.isPackaged ? userDataDir : path.join(process.env.HOME || userDataDir, ".stock-finance"),
  });
  const startResult = await pythonManager.start(PYTHON_PORT);
  if (!startResult.success) {
    const msg = startResult.error || (app.isPackaged
      ? "Python 后端启动失败。请退出 App，在安装镜像中双击“安装运行依赖.command”，完成后重新打开 App。"
      : "无法启动 Python 后端服务，请检查 Python 环境");
    dialog.showErrorBox("启动失败", msg);
    app.quit();
    return;
  }

  // 等 1 秒让服务器就绪
  await new Promise((r) => setTimeout(r, 1500));

  createWindow();
  createTray();

  // 启动定时任务 (北京时间 17:30, 周一至周五)
  scheduler = new Scheduler(() => runDataPipeline(), postIntradayScan);
  scheduler.start();
  if (await needsCatchUp()) {
    console.log("[Scheduler] Runtime data is stale; starting catch-up update");
    void runDataPipeline(20).catch((err) => {
      console.error("[Scheduler] Catch-up update failed:", err);
    });
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  // macOS 下不退出
});

app.on("before-quit", () => {
  scheduler?.stop();
  pythonManager?.stop();
});
