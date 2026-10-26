package com.example.antitheft

import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters

/**
 * 充电自动复活：插入电源即拉起常驻守护（实时通道开着则拉起 WS）。
 *
 * 用 WorkManager「充电约束」一次性任务实现——系统级托管，即使 App 进程已被杀，
 * 插入 USB/充电时系统也会唤醒本 Worker，从而把进程叫醒并恢复前台服务。
 *
 * 关键修复（WS 重连风暴根因）：本 Worker 只做「一次性触发」，执行后【不再自链】。
 * 旧代码在 doWork 末尾自链下一个相同任务，叠加 RequiresCharging 约束——
 * 充电态下约束持续满足 → WorkManager 每 ~250ms 重跑一次 → 反复
 * startForegroundService(RealtimeService) → 反复新建 WS → 风暴。
 * 触发改为事件驱动：① 开机（BootReceiver）② 插入电源广播（PowerConnectedReceiver），均为一次性。
 */
class ChargeResumeWorker(ctx: Context, params: WorkerParameters) : Worker(ctx, params) {

    override fun doWork(): Result {
        if (!Prefs.isRegistered(applicationContext)) {
            // 未注册：不再续链，等下次注册时再排
            return Result.success()
        }
        Log.i("ChargeResumeWorker", "charging -> resume keepalive")
        runCatching {
            if (RealtimeService.isOn(applicationContext)) {
                applicationContext.startForegroundService(
                    Intent(applicationContext, RealtimeService::class.java)
                )
            } else {
                KeepAliveService.start(applicationContext)
            }
        }
        // 不再自链：触发改由 PowerConnectedReceiver（插入电源）/ BootReceiver（开机）一次性调度，
        // 否则 RequiresCharging 约束在充电态下持续满足 → 每 ~250ms 重跑 → 反复拉起前台服务 → WS 重连风暴。
        return Result.success()
    }

    companion object {
        private const val NAME = "at_charge_resume"

        fun schedule(ctx: Context) {
            val req = OneTimeWorkRequestBuilder<ChargeResumeWorker>()
                .setConstraints(
                    Constraints.Builder()
                        .setRequiresCharging(true)
                        .build()
                )
                .build()
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                NAME, ExistingWorkPolicy.REPLACE, req
            )
        }
    }
}
