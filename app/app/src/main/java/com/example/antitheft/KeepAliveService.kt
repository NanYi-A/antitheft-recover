package com.example.antitheft

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

/**
 * 常驻守护前台服务（防丢连接的最后一道保险）。
 *
 * 设计要点：
 *  - 设备注册成功后即常驻，保证进程不被 vivo/小米 清后台冻死；WorkManager 轮询只是兜底。
 *  - 与 RealtimeService 互斥：实时通道开启时由 RealtimeService 顶上（避免双通知），
 *    实时通道关闭时由本服务维持常驻。任意时刻最多一个前台服务、一条通知。
 *  - 通知走与 RealtimeService 相同的「可隐藏」渠道（C.NOTIF_CHANNEL_ID），
 *    用户在设置页「隐藏通知」即可让通知栏/状态栏无痕迹（仅剩 Android 14 的“运行中”小标）。
 *  - START_STICKY：被系统回收后会自动重建；重建时若已不应运行则自行收尾。
 */
class KeepAliveService : Service() {

    companion object {
        const val NOTIF_ID = 3

        /** 是否需要本服务常驻：已注册 且 实时通道未开 */
        fun shouldRun(ctx: Context): Boolean =
            Prefs.isRegistered(ctx) && !RealtimeService.isOn(ctx)

        fun start(ctx: Context) {
            if (shouldRun(ctx)) {
                runCatching { ctx.startForegroundService(Intent(ctx, KeepAliveService::class.java)) }
            }
        }

        fun stop(ctx: Context) {
            runCatching { ctx.stopService(Intent(ctx, KeepAliveService::class.java)) }
        }
    }

    // 运行态守卫：避免 ChargeResumeWorker / BootReceiver 重复拉起时反复排轮询任务
    @Volatile private var running = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!shouldRun(this)) {
            stopSelf()
            return START_NOT_STICKY
        }
        ensureForeground()
        if (running) {
            // 已常驻：只保活前台，不重复排轮询
            return START_STICKY
        }
        running = true
        PollWorker.schedule(this)
        return START_STICKY
    }

    private fun ensureForeground() {
        C.ensureChannel(this)
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0
        try {
            startForeground(NOTIF_ID, buildNotification(), type)
        } catch (_: Exception) {
            startForeground(NOTIF_ID, buildNotification())
        }
    }

    private fun buildNotification(): Notification {
        val pi = PendingIntent.getActivity(
            this, 0,
            Intent(this, SettingsActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, C.NOTIF_CHANNEL_ID)
            .setContentTitle(C.NOTIF_TITLE)
            .setContentText(C.NOTIF_TEXT)
            .setSmallIcon(R.drawable.ic_hidden)
            .setContentIntent(pi)
            .setOngoing(true)
            .setShowWhen(false)
            .setSilent(true)
            .setVisibility(NotificationCompat.VISIBILITY_SECRET)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()
    }

    override fun onDestroy() {
        running = false
        super.onDestroy()
    }
}
