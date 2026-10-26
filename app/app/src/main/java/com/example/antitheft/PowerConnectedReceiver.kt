package com.example.antitheft

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.Intent.ACTION_POWER_CONNECTED

/**
 * 插入电源（USB/充电器）时一次性触发 ChargeResumeWorker，恢复常驻守护 / 实时通道。
 *
 * 仅做「触发」，【不做自链】——Worker 跑一次即止，由服务自身的 START_STICKY +
 * 前台常驻 + 各自调度逻辑维持运行。这样从根上消除「充电态下每 250ms 重跑一次
 * 充电 Worker → 反复 startForegroundService(RealtimeService) → 反复新建 WS」的重连风暴。
 */
class PowerConnectedReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != ACTION_POWER_CONNECTED) return
        if (!Prefs.isRegistered(ctx)) return
        ChargeResumeWorker.schedule(ctx)
    }
}
