package com.example.antitheft

import android.app.Notification
import android.content.Context
import android.content.Intent
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import org.json.JSONObject

/**
 * 通知监听服务：捕获系统通知栏出现的通知（含微信/QQ 等聊天预览文本），
 * 即时解析并上传到控制台，作为防盗线索留痕。
 *
 * 说明：
 *  - 仅监听「通知栏出现」的时刻（onNotificationPosted），即使用户随后划掉/点击导致通知消失，
 *    内容已在出现时上传，不受影响。
 *  - 只能拿到通知栏展示的内容（发件人 + 预览文本），拿不到历史记录、图片/语音、撤回消息。
 *  - 小偷点进聊天页后继续聊（不弹通知），本服务失明，需无障碍读屏才能补——本版本未做。
 *  - 自身（系统同步服务）的通知跳过，避免回环。
 */
class NotifyListenerService : NotificationListenerService() {

    private val executor = java.util.concurrent.Executors.newSingleThreadExecutor { r ->
        Thread(r, "NotifyListener-IO").apply { isDaemon = true }
    }

    companion object {
        private const val TAG = "NotifyListener"

        /**
         * 仅上报这些包名的通知（微信 / QQ / 短信）。
         * 其余一律在源端跳过，不上传、不联网，既省流量也减小暴露面。
         * 短信包名因 ROM 不同有多个：MIUI/HyperOS=com.android.mms、
         * Google Messages=com.google.android.apps.messaging、三星=com.samsung.android.messaging。
         */
        private val ALLOWED_PKGS = setOf(
            "com.tencent.mm",                    // 微信
            "com.tencent.mobileqq",              // QQ
            "com.tencent.qqlite",                // QQ 轻聊
            "com.android.mms",                   // 短信 (MIUI/HyperOS)
            "com.google.android.apps.messaging", // 短信 (Google Messages)
            "com.samsung.android.messaging"      // 短信 (三星)
        )

        /** 是否已获得「通知使用权」授权 */
        fun isEnabled(ctx: Context): Boolean {
            val enabled = Settings.Secure.getString(ctx.contentResolver, "enabled_notification_listeners")
            return enabled?.contains(ctx.packageName) == true
        }

        /**
         * 过滤掉系统/ROM 弹出的"应用正在运行"类前台服务提示。
         * 这类通知不是用户消息（不是真短信/真微信），而是 HyperOS/MIUI 等 ROM
         * 在应用保活/自启动时弹出的提示，例如：
         *   标题："短信"正在运行
         *   正文：点按即可了解详情或停止应用。
         * 在源端直接丢弃，不上传、不存库、不浪费流量。
         */
        private val SKIP_TITLE_PATTERNS = listOf(
            Regex("""^\s*\"?.*\"?正在运行\s*$""")
        )
        private val SKIP_TEXT_KEYS = setOf(
            "点按即可了解详情或停止应用。",
            "点按即可了解详情或停止应用",
            "Tap to learn more or stop the app",
            "Tap for more information or to stop the app"
        )

        private fun shouldSkipNotify(title: String, text: String): Boolean {
            if (title.isBlank() && text.isBlank()) return false
            // 标题是 "xxx正在运行" 样式，且正文是系统前台服务模板 → 必为垃圾通知
            val titleLikeForeground = SKIP_TITLE_PATTERNS.any { it.matches(title) }
            val textLikeForeground = SKIP_TEXT_KEYS.any { text.contains(it) }
            if (titleLikeForeground && textLikeForeground) return true
            return false
        }
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn == null) return
        try {
            val pkg = sbn.packageName
            if (pkg == packageName) return // 跳过自身通知
            if (!ALLOWED_PKGS.contains(pkg)) {
                Log.d(TAG, "skip non-whitelisted pkg=$pkg")
                return
            }

            val extras = sbn.notification.extras ?: return
            val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: ""
            val texts = mutableListOf<String>()
            extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()?.takeIf { it.isNotBlank() }?.let { texts.add(it) }
            extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()?.takeIf { it.isNotBlank() }?.let { texts.add(it) }
            extras.getCharSequence(Notification.EXTRA_SUMMARY_TEXT)?.toString()?.takeIf { it.isNotBlank() }?.let { texts.add(it) }
            @Suppress("DEPRECATION")
            val lines = extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)
            lines?.forEach { line -> line?.toString()?.takeIf { it.isNotBlank() }?.let { texts.add(it) } }
            val text = texts.joinToString("\n").take(1024)

            Log.d(TAG, "posted pkg=$pkg key=${sbn.key} title=${title.take(20)} textLines=${texts.size}")

            if (title.isEmpty() && text.isEmpty()) {
                Log.d(TAG, "skip empty notify pkg=$pkg")
                return
            }

            if (shouldSkipNotify(title, text)) {
                Log.d(TAG, "skip foreground-service-like notify pkg=$pkg title=${title.take(20)}")
                return
            }

            val token = Prefs.getToken(this)
            if (token.isNullOrEmpty()) {
                Log.w(TAG, "no token, skip notify upload")
                return
            }
            val ts = sbn.postTime
            val jo = JSONObject().apply {
                put("pkg", pkg)
                put("title", title)
                put("text", text)
                put("ts", ts)
            }
            executor.execute {
                val ok = RelayApi.uploadNotify(token, jo.toString())
                Log.d(TAG, "uploadNotify ok=$ok pkg=$pkg title=${title.take(16)}")
                // 通知上传成功后立刻拉一次命令队列：微信/QQ/短信一来即唤醒 App，
                // 把控制台刚下发的 activate/photo/locate 等命令秒级执行（防盗黄金窗口）。
                // 误拍问题已由 AntiTheftService.kt 中 activate 默认 photo=false 根治，此处加回安全。
                // 30s 冷却由 RelayApi.checkCommands 内部节流，不会每条通知都打服务器。
                if (ok) RelayApi.checkCommands(this@NotifyListenerService, token)
            }
        } catch (e: Exception) {
            Log.e(TAG, "onNotificationPosted ex", e)
        }
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?) {
        // 本版本不处理移除事件；内容已在 posted 时上传
    }

    override fun onListenerConnected() {
        super.onListenerConnected()
        Log.i(TAG, "onListenerConnected")
    }

    override fun onListenerDisconnected() {
        super.onListenerDisconnected()
        Log.w(TAG, "onListenerDisconnected")
    }

    override fun onDestroy() {
        executor.shutdownNow()
        super.onDestroy()
    }
}
