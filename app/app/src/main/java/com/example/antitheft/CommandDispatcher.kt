package com.example.antitheft

import android.content.Context
import org.json.JSONObject

/**
 * 指令分发中心：PollWorker（15 分钟轮询兜底）与 RealtimeService（WS 实时通道）共用同一份分发逻辑。
 * 无论命令来自哪条通道，手机端执行语义完全一致。
 */
object CommandDispatcher {
    fun dispatch(ctx: Context, cmd: String, params: JSONObject?) {
        when (cmd) {
            "activate" -> AntiTheftService.start(ctx, params)
            "locate" -> AntiTheftService.requestLocate(ctx)
            "photo" -> AntiTheftService.requestPhoto(ctx, params)
            "album" -> AntiTheftService.requestAlbum(ctx, params)
            "album_original" -> AntiTheftService.requestAlbumOriginal(ctx, params)
            "usage" -> AntiTheftService.requestUsage(ctx)
            "lock" -> AntiTheftService.requestLock(ctx)
            "wipe" -> AntiTheftService.requestWipe(ctx)
            "stop" -> AntiTheftService.stop(ctx)
            "realtime" -> handleRealtime(ctx, params)
        }
    }

    private fun handleRealtime(ctx: Context, params: JSONObject?) {
        val on = params?.optBoolean("on", false) ?: false
        if (on) {
            // 实时通道顶上，关掉常驻守护避免双通知
            KeepAliveService.stop(ctx)
            RealtimeService.start(ctx)
        } else {
            RealtimeService.stop(ctx)
            // 实时关了，改由常驻守护维持进程
            KeepAliveService.start(ctx)
        }
    }
}
