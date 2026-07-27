/**
 * Preload 脚本 - 在渲染进程和主进程之间建立安全的 IPC 桥接
 */

import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("electronAPI", {
  /** 获取后端 base URL */
  getBaseUrl: () => "http://localhost:8001",

  /** 监听数据更新完成事件 */
  onDataUpdated: (callback: () => void) => {
    ipcRenderer.on("data-updated", callback);
    return () => ipcRenderer.removeListener("data-updated", callback);
  },

  /** 获取 app 版本 */
  getVersion: () => ipcRenderer.invoke("get-version"),

  /** 打开文件夹选择对话框 */
  selectDirectory: () => ipcRenderer.invoke("select-dir"),

  /** 在 Finder 中打开指定路径（打包版仅限应用数据目录） */
  openPath: (target: string) => ipcRenderer.invoke("open-path", target),
});
