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
        { label: "刷新数据", click: () => runDataPipeline() },
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
    { label: "刷新数据", click: () => runDataPipeline() },
    { type: "separator" },
    { label: "退出", click: () => app.quit() },
  ]);
  tray.setContextMenu(contextMenu);
  tray.on("click", showOrCreate);
}

function postRefresh(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.request(
      {
        hostname: "localhost",
        port: PYTHON_PORT,
        path: "/api/refresh-data",
        method: "POST",
        timeout: 5000,
      },
      (res) => {
        resolve(res.statusCode === 200);
      }
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.end();
  });
}

function pollRefreshStatus(maxMs = 1200000): Promise<{ running: boolean; success?: boolean; error?: string; current_step?: string }> {
  return new Promise((resolve) => {
    const start = Date.now();
    const check = () => {
      if (Date.now() - start > maxMs) {
        resolve({ running: false, success: false, error: "数据更新轮询超时" });
        return;
      }
      const req = http
        .get(`${BASE_URL}/api/refresh-data/status`, { timeout: 3000 }, (res) => {
          let data = "";
          res.on("data", (chunk) => {
            data += chunk;
          });
          res.on("end", () => {
            try {
              const status = JSON.parse(data);
              if (!status.running) {
                resolve(status);
              } else {
                notify("数据更新", status.current_step || "更新中...");
                setTimeout(check, 1000);
              }
            } catch {
              setTimeout(check, 1000);
            }
          });
        })
        .on("error", () => setTimeout(check, 1000))
        .on("timeout", () => {
          req.destroy();
          setTimeout(check, 1000);
        });
    };
    check();
  });
}

/**
 * 执行完整数据更新流水线（通过后端 API，避免在 Electron 主进程重复实现合并逻辑）
 */
let pipelineRunning = false;

async function runDataPipeline(): Promise<void> {
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
    const started = await postRefresh();
    if (!started) {
      notify("错误", "无法启动数据刷新，请检查后端服务");
      return;
    }
    const status = await pollRefreshStatus(1200000);
    if (status.success) {
      notify("数据更新", "✅ 数据更新完成");
      mainWindow?.webContents.reload();
    } else {
      const err = status.error || "未知错误";
      notify("更新失败", err);
      dialog.showErrorBox("数据更新失败", err);
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

  // 打包后用 Resources 目录（Python 脚本在 asar 外），开发模式用项目根目录
  const projectRoot = app.isPackaged
    ? process.resourcesPath
    : path.join(__dirname, "..", "..");
  console.log("Project root:", projectRoot, "isPackaged:", app.isPackaged);
  pythonManager = new PythonManager(projectRoot);
  const startResult = await pythonManager.start(PYTHON_PORT);
  if (!startResult.success) {
    const msg = startResult.error || (app.isPackaged
      ? "Python 后端启动失败。请确认已安装 Python3，然后运行：\npip3 install fastapi uvicorn akshare pandas requests httpx openpyxl baostock\n\n或双击 DMG 中的 setup.sh 一键安装"
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
  scheduler = new Scheduler(() => runDataPipeline());
  scheduler.start();

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
