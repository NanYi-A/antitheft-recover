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
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 实时通道：按需手动开启的 WebSocket 长连接前台服务。
 *  - 开启：建立 /ws/anti-theft 长连接，服务端命令秒级 push，且可一次多条。
 *  - 关闭：断开连接并停止服务，回潜伏（仅 15 分钟轮询兜底，零额外耗电/痕迹）。
 *
 * 容错设计（本次修复核心）：
 *  - WS 连不上 / 断开时，自动启动「轮询兜底」：周期性调用 RelayApi.checkCommands 拉取命令并分发，
 *    复用与 PollWorker 完全相同的逻辑，但不启动 KeepAliveService（避免双通知）。
 *    这样即使 WS 因蜂窝路由抖动彻底连不上，命令仍能在 ≤30s 内经 poll 到达并执行（关闭/定位/相册都能用）。
 *  - WS 重连成功(onOpen)后自动停掉轮询兜底，回到「零额外耗电」的纯 WS 状态。
 *  - 重连风暴修复：用 reconnectScheduled 标志保证同一时刻只挂一个重连任务，指数退避 1s→2s→…→30s(上限)。
 *
 * 断线自动重连，受 SharedPreferences 的「开启开关」控制——手动关闭后不再重连。
 */
class RealtimeService : Service() {

    private val handler = Handler(Looper.getMainLooper())
    private var ws: WebSocket? = null

    // 单一共享 OkHttpClient（复用连接池，绝不每次 connect 新建，
    // 否则旧 socket 泄漏 → 服务器侧堆积大量 connected 却收不到 disconnected）。
    private val client = OkHttpClient.Builder()
        .pingInterval(25, TimeUnit.SECONDS)  // 25s 心跳：压过绝大多数 NAT/代理空闲超时，避免被静默掐断后疯狂重连
        .build()
    private var reconnectAttempts = 0
    @Volatile private var shouldRun = true

    // 重连风暴修复：保证同一时刻只有一个待执行的重连任务在排队
    @Volatile private var reconnectScheduled = false

    // 运行态守卫：服务已 running 时，重复 startForegroundService 不再重复 connect() 建新 WS
    @Volatile private var running = false

    // 轮询兜底：WS 不可用时周期性拉命令（不依赖 KeepAliveService，避免双通知）
    @Volatile private var pollFallbackActive = false
    private val POLL_FALLBACK_INTERVAL = 30_000L // 与 RelayApi.checkCommands 的 30s 冷却对齐，确保每次都真正拉取
    private val pollFallbackRunnable = Runnable { runPollFallback() }

    companion object {
        private const val PREFS = "at_realtime"
        private const val KEY_ON = "on"
        private const val NOTIF_ID = 2

        fun isOn(ctx: Context) =
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY_ON, false)

        fun start(ctx: Context) {
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean(KEY_ON, true).apply()
            ctx.startForegroundService(Intent(ctx, RealtimeService::class.java))
        }

        fun stop(ctx: Context) {
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean(KEY_ON, false).apply()
            ctx.startForegroundService(Intent(ctx, RealtimeService::class.java).setAction("stop"))
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        ensureForeground()
        if (intent?.action == "stop") {
            shouldRun = false
            cancelReconnect()
            stopPollFallback()
            closeWs()
            stopSelf()
            return START_NOT_STICKY
        }
        if (!isOn(this)) {
            // 开关已关闭但服务还在（竞态），立刻收尾
            shouldRun = false
            cancelReconnect()
            stopPollFallback()
            closeWs()
            stopSelf()
            return START_NOT_STICKY
        }
        if (running) {
            // 已运行（系统回收重建 / 被重复拉起）：只保活前台，不重复 connect 建新 WS
            Log.d("RealtimeService", "onStartCommand: already running, skip reconnect")
            return START_STICKY
        }
        running = true
        connect()
        return START_STICKY
    }

    private fun ensureForeground() {
        C.ensureChannel(this)
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        else 0
        try {
            startForeground(NOTIF_ID, buildNotification(), type)
        } catch (_: Exception) {
            startForeground(NOTIF_ID, buildNotification())
        }
    }

    private fun connect() {
        val deviceId = Prefs.getDeviceId(this)
        val token = Prefs.getToken(this)
        if (deviceId == null || token == null) {
            Log.e("RealtimeService", "no device_id/token, skip reconnect")
            scheduleReconnect()
            return
        }
        // 关键修复（重连风暴根因）：开新连接前先取消并丢弃旧 socket，
        // 否则旧连接一直挂在服务器侧（收不到 disconnected），叠加系统反复拉起服务 → connected 越堆越多。
        // cancel() 会异步触发旧 socket 的 onFailure，但那时 ws 已指向新连接，回调里 webSocket!==ws 会被忽略。
        val old = ws
        ws = null
        try { old?.cancel() } catch (_: Exception) { }

        val wsBase = C.RELAY_BASE.replace("https://", "wss://").replace("http://", "ws://")
        val url = "$wsBase/ws/anti-theft?device_id=$deviceId&token=$token"
        val req = Request.Builder().url(url).build()
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (webSocket !== ws) return  // 过期连接的回调，忽略
                Log.i("RealtimeService", "ws open")
                reconnectAttempts = 0
                reconnectScheduled = false
                // WS 恢复：停掉轮询兜底，回到纯 WS（零额外耗电）
                stopPollFallback()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (webSocket !== ws) return
                try {
                    val jo = JSONObject(text)
                    if (jo.optString("type") == "command") {
                        val cmd = jo.optString("cmd")
                        val params = jo.optJSONObject("params")
                        Log.i("RealtimeService", "ws cmd=$cmd")
                        CommandDispatcher.dispatch(this@RealtimeService, cmd, params)
                    }
                } catch (e: Exception) {
                    Log.e("RealtimeService", "ws msg ex", e)
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (webSocket !== ws) return  // 过期连接的回调，忽略
                Log.e("RealtimeService", "ws failure", t)
                closeWs()
                onWsDown()
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (webSocket !== ws) return  // 过期连接的回调，忽略
                Log.i("RealtimeService", "ws closed $code $reason")
                closeWs()
                onWsDown()
            }
        })
    }

    /** WS 不可用（连不上/断开）：启动轮询兜底 + 安排重连 */
    private fun onWsDown() {
        startPollFallback()
        scheduleReconnect()
    }

    private fun scheduleReconnect() {
        if (!shouldRun || !isOn(this)) return
        if (reconnectScheduled) return // 已有待执行的重连，避免重连风暴
        reconnectScheduled = true
        reconnectAttempts++
        // 指数退避：3s→6s→12s→24s→48s→60s(上限)。最小 3s 避免"一断就每秒重连"的可见风暴
        val delay = minOf(60_000L, 3000L * (1L shl minOf(reconnectAttempts - 1, 4)))
        Log.i("RealtimeService", "scheduleReconnect in ${delay}ms (attempt=$reconnectAttempts)")
        handler.postDelayed({
            reconnectScheduled = false
            if (shouldRun && isOn(this)) connect()
        }, delay)
    }

    private fun cancelReconnect() {
        reconnectScheduled = false
    }

    // ---- 轮询兜底：WS 不可用时周期性拉命令 ----
    private fun startPollFallback() {
        if (!shouldRun || !isOn(this)) return
        if (pollFallbackActive) return
        pollFallbackActive = true
        Log.i("RealtimeService", "poll fallback START")
        handler.post(pollFallbackRunnable)
    }

    private fun runPollFallback() {
        if (!pollFallbackActive || !shouldRun || !isOn(this)) {
            pollFallbackActive = false
            return
        }
        val token = Prefs.getToken(applicationContext)
        if (token != null) {
            // 后台线程执行，避免在主线程做同步网络请求导致 ANR
            Thread { RelayApi.checkCommands(applicationContext, token) }.start()
        }
        // 继续周期性轮询，直到 WS 恢复(onOpen 会停掉)或服务关闭
        handler.postDelayed(pollFallbackRunnable, POLL_FALLBACK_INTERVAL)
    }

    private fun stopPollFallback() {
        if (pollFallbackActive) Log.i("RealtimeService", "poll fallback STOP")
        pollFallbackActive = false
        handler.removeCallbacks(pollFallbackRunnable)
    }

    private fun closeWs() {
        try { ws?.close(1000, "bye") } catch (_: Exception) { }
        ws = null
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
        super.onDestroy()
        running = false
        shouldRun = false
        cancelReconnect()
        stopPollFallback()
        closeWs()
    }
}
