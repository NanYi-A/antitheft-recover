package com.example.antitheft

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context

/**
 * 全局常量。改这里即可调整中继地址、暗码、轮询间隔。
 */
object C {
    /** 云中继基址（HTTPS，标准 443 端口，经 Nginx 反代到本地 8001）。
     *  用域名不用 IP，换服务器/迁移轻量时只改 DNS 解析指向新 IP，无需重装 APK。
     *  WS 地址由 RealtimeService 自动派生为 wss://（见 RealtimeService.kt）。 */
    const val RELAY_BASE = "https://relay.example.com"

    /** 暗码：拨号盘输入 *#*#<自定义数字>#*#* 唤起设置页（需与 AndroidManifest 中的 host 保持一致）
     *  公开仓库中已替换为占位符，使用时请改回你自己的暗码。 */
    const val SECRET_CODE = "12345678"

    /** WorkManager 轮询最小间隔（Android 强制 15 分钟） */
    const val POLL_INTERVAL_MIN = 15L

    /** 通知伪装 */
    const val NOTIF_CHANNEL_ID = "sys_sync"
    const val NOTIF_CHANNEL_NAME = "系统同步服务"
    const val NOTIF_TITLE = "系统服务"
    const val NOTIF_TEXT = "正在同步数据…"

    /** 创建隐蔽通知渠道（三处 Service 共用，避免重复代码）。
     *  IMPORTANCE_MIN：不弹横幅、不响、不亮屏；前台服务仍可运行。 */
    fun ensureChannel(ctx: Context) {
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (nm.getNotificationChannel(NOTIF_CHANNEL_ID) == null) {
            val ch = NotificationChannel(
                NOTIF_CHANNEL_ID, NOTIF_CHANNEL_NAME, NotificationManager.IMPORTANCE_MIN
            )
            ch.setShowBadge(false)
            ch.enableLights(false)
            ch.enableVibration(false)
            ch.setSound(null, null)
            ch.lockscreenVisibility = Notification.VISIBILITY_SECRET
            nm.createNotificationChannel(ch)
        }
    }
}
