/**
 * 定时任务调度器
 *
 * 北京时间 17:30 周一至周五执行数据更新
 * 失败后 18:30、20:00 重试
 */

import * as cron from "node-cron";

export class Scheduler {
  private job: cron.ScheduledTask | null = null;
  private intradayJob: cron.ScheduledTask | null = null;
  private intradayRunning = false;
  private retryTimers: NodeJS.Timeout[] = [];

  constructor(
    private task: () => Promise<void>,
    private intradayTask?: () => Promise<void>,
  ) {}

  /** 获取当前北京时间的小时和分钟 */
  private beijingNow(): { hour: number; minute: number } {
    const fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Shanghai",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    const [h, m] = fmt.format(new Date()).split(":").map(Number);
    return { hour: h, minute: m };
  }

  start(): void {
    this.job = cron.schedule("30 17 * * 1-5", async () => {
      console.log("[Scheduler] Starting daily data pipeline...");
      try {
        await this.task();
        console.log("[Scheduler] Pipeline completed successfully");
      } catch (err) {
        console.error("[Scheduler] Pipeline failed:", err);
        this.scheduleRetry();
      }
    }, {
      timezone: "Asia/Shanghai",
    });

    console.log("[Scheduler] Daily update scheduled at 17:30 Mon-Fri (Asia/Shanghai)");

    if (this.intradayTask) {
      this.intradayJob = cron.schedule("* 9-15 * * 1-5", async () => {
        const { hour, minute } = this.beijingNow();
        const morning = (hour === 9 && minute >= 25) || hour === 10 || (hour === 11 && minute <= 30);
        const afternoon = hour >= 13 && (hour < 15 || (hour === 15 && minute <= 5));
        if ((!morning && !afternoon) || this.intradayRunning) return;
        this.intradayRunning = true;
        try {
          await this.intradayTask?.();
        } catch (err) {
          console.error("[Scheduler] Intraday snapshot trigger failed:", err);
        } finally {
          this.intradayRunning = false;
        }
      }, { timezone: "Asia/Shanghai" });
      console.log("[Scheduler] Intraday snapshots scheduled every minute during market sessions");
    }
  }

  /** 失败后依次安排 18:30、20:00 重试 */
  private scheduleRetry(): void {
    const retryTimes = ["18:30", "20:00"];
    const { hour: currentHour, minute: currentMinute } = this.beijingNow();

    for (const time of retryTimes) {
      const [h, m] = time.split(":").map(Number);
      if (h > currentHour || (h === currentHour && m > currentMinute)) {
        const delayMs = ((h - currentHour) * 60 + (m - currentMinute)) * 60 * 1000;
        console.log(`[Scheduler] Scheduling retry at ${time} CST, delay ${Math.round(delayMs / 60000)} min`);
        const timer = setTimeout(() => {
          // 清除已触发的定时器
          this.retryTimers = this.retryTimers.filter((t) => t !== timer);
          this.runRetry();
        }, delayMs);
        this.retryTimers.push(timer);
        return; // 只安排最近一次
      }
    }
  }

  private async runRetry(): Promise<void> {
    console.log("[Scheduler] Running retry...");
    try {
      await this.task();
      console.log("[Scheduler] Retry completed successfully");
    } catch (err) {
      console.error("[Scheduler] Retry failed:", err);
      // 尝试下一次重试
      this.scheduleRetry();
    }
  }

  stop(): void {
    this.job?.stop();
    this.intradayJob?.stop();
    this.retryTimers.forEach((t) => clearTimeout(t));
    this.retryTimers = [];
    console.log("[Scheduler] Stopped");
  }
}
