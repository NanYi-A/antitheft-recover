package com.example.antitheft

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * 开机自启：恢复 WorkManager 轮询（仅已注册设备才会真正拉指令）。
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        // 1. 恢复 WorkManager 轮询（核心兜底，无需前台服务）
        PollWorker.schedule(ctx)
        // 2. 实时通道开启则开机续上 WS；否则拉起常驻守护，确保进程不被冻死
        if (RealtimeService.isOn(ctx)) {
            runCatching {
                ctx.startForegroundService(Intent(ctx, RealtimeService::class.java))
            }
        } else {
            KeepAliveService.start(ctx)
        }
        // 3. 充电自动复活：插入电源即时拉起（前台服务被杀后的兜底）
        ChargeResumeWorker.schedule(ctx)
    }
}
