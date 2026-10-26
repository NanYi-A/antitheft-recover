package com.example.antitheft

import android.app.AppOpsManager
import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.os.Build
import android.os.Process
import android.provider.Settings
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.util.Calendar
import java.util.concurrent.TimeUnit

/**
 * 应用使用统计采集（UsageStatsManager）。
 * 需要特殊权限 PACKAGE_USAGE_STATS，代码无法自动授予，需引导用户到系统设置手动开启。
 */
object UsageCollector {

    private const val TAG = "UsageCollector"

    /** 是否已获得「使用情况访问」权限 */
    fun hasUsagePermission(ctx: Context): Boolean {
        val appOps = ctx.getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            appOps.unsafeCheckOpNoThrow(
                AppOpsManager.OPSTR_GET_USAGE_STATS,
                Process.myUid(), ctx.packageName
            )
        } else {
            @Suppress("DEPRECATION")
            appOps.checkOpNoThrow(
                AppOpsManager.OPSTR_GET_USAGE_STATS,
                Process.myUid(), ctx.packageName
            )
        }
        return mode == AppOpsManager.MODE_ALLOWED
    }

    /** 跳转到「使用情况访问」授权页（手动授权） */
    fun openUsageSettings(ctx: Context) {
        try {
            ctx.startActivity(
                Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        } catch (_: Exception) {
        }
    }

    /**
     * 采集最近 days 天的按天分桶使用统计（仅保留有前台时长的条目）。
     * 每项：{day, pkg, total_ms, last_used, first_used, launches}
     */
    fun collectUsage(ctx: Context, days: Int = 60): JSONArray {
        val usm = ctx.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
        val end = System.currentTimeMillis()
        val start = end - TimeUnit.DAYS.toMillis(days.toLong())
        val stats = runCatching {
            usm.queryUsageStats(UsageStatsManager.INTERVAL_DAILY, start, end)
        }.getOrNull() ?: emptyList()
        Log.d(TAG, "collectUsage: raw stats=${stats.size} days=$days")

        val arr = JSONArray()
        var skippedSystem = 0
        var kept = 0
        for (s in stats) {
            val total = s.totalTimeInForeground
            if (total <= 0) continue
            val last = s.lastTimeUsed
            if (last <= 0) continue
            // 过滤系统应用（含 HyperOS 未打 FLAG_SYSTEM 的预装组件）
            if (isSystemAppByPkg(ctx, s.packageName)) {
                skippedSystem++
                continue
            }
            val cal = Calendar.getInstance().apply { timeInMillis = last }
            val day = "%04d-%02d-%02d".format(
                cal.get(Calendar.YEAR), cal.get(Calendar.MONTH) + 1, cal.get(Calendar.DAY_OF_MONTH)
            )
            val launches = runCatching {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    val m = s.javaClass.getMethod("getAppLaunchCount")
                    (m.invoke(s) as? Int) ?: 0
                } else 0
            }.getOrDefault(0)
            val o = JSONObject()
            o.put("day", day)
            o.put("pkg", s.packageName)
            o.put("label", appLabel(ctx, s.packageName))
            o.put("total_ms", total)
            o.put("last_used", last)
            o.put("first_used", s.firstTimeStamp)
            o.put("launches", launches)
            arr.put(o)
            kept++
        }
        Log.d(TAG, "collectUsage: kept=$kept skippedSystem=$skippedSystem")
        return arr
    }

    /**
     * 采集最近 days 天的应用前台会话明细。
     * 通过 UsageEvents 的前台/后台事件配对，得到每次「打开 → 切后台」的会话：
     * {pkg, start, end, duration_ms}。
     * 同时兼容旧常量（MOVE_TO_FOREGROUND/BACKGROUND）和 Android 10+ 新常量
     *（ACTIVITY_RESUMED/PAUSED），避免某些系统（如 HyperOS）只记录新类型导致配对失败。
     * 未闭合的会话（应用仍在前台）不返回，避免显示到当前时刻的不稳定数据。
     */
    fun collectSessions(ctx: Context, days: Int = 30): JSONArray {
        val usm = ctx.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
        val end = System.currentTimeMillis()
        val start = end - TimeUnit.DAYS.toMillis(days.toLong())
        val events = runCatching { usm.queryEvents(start, end) }.getOrNull() ?: return JSONArray()

        val list = mutableListOf<JSONObject>()
        // pkg -> 最近一次前台事件的时间戳
        val openMap = mutableMapOf<String, Long>()
        val ev = UsageEvents.Event()
        var eventCnt = 0
        var fgCnt = 0
        var bgCnt = 0

        while (events.hasNextEvent()) {
            events.getNextEvent(ev)
            eventCnt++
            val pkg = ev.packageName ?: continue
            if (isSystemAppByPkg(ctx, pkg)) continue
            val ts = ev.timeStamp
            when (ev.eventType) {
                UsageEvents.Event.MOVE_TO_FOREGROUND,
                UsageEvents.Event.ACTIVITY_RESUMED -> {
                    fgCnt++
                    // 同一应用已有未闭合会话：先闭合旧会话（异常/遗漏场景）
                    openMap[pkg]?.let { oldStart ->
                        if (ts > oldStart) list.add(makeSession(pkg, oldStart, ts))
                    }
                    openMap[pkg] = ts
                }
                UsageEvents.Event.MOVE_TO_BACKGROUND,
                UsageEvents.Event.ACTIVITY_PAUSED -> {
                    bgCnt++
                    openMap[pkg]?.let { openTs ->
                        if (ts > openTs) list.add(makeSession(pkg, openTs, ts))
                        openMap.remove(pkg)
                    }
                }
            }
        }
        // 故意不闭合仍处前台的会话，避免"使用到当前"这种不精确数据。
        Log.d(TAG, "collectSessions: rawEvents=$eventCnt fg=$fgCnt bg=$bgCnt sessions=${list.size} openLeft=${openMap.size}")
        list.sortByDescending { it.getLong("start") }
        return JSONArray(list)
    }

    private fun makeSession(pkg: String, start: Long, end: Long): JSONObject {
        val o = JSONObject()
        o.put("pkg", pkg)
        o.put("start", start)
        o.put("end", end)
        o.put("duration_ms", end - start)
        return o
    }

    /** 当前前台 App：取最近 2 分钟内最后一次前台事件对应的包名 */
    fun currentForeground(ctx: Context): JSONObject? {
        val usm = ctx.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
        val end = System.currentTimeMillis()
        val start = end - TimeUnit.MINUTES.toMillis(2)
        val events = runCatching { usm.queryEvents(start, end) }.getOrNull()
        if (events == null) {
            Log.d(TAG, "currentForeground: queryEvents returned null")
            return null
        }
        var lastPkg: String? = null
        var lastTime = 0L
        val ev = UsageEvents.Event()
        val fgTypes = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            setOf(UsageEvents.Event.MOVE_TO_FOREGROUND, UsageEvents.Event.ACTIVITY_RESUMED)
        } else {
            setOf(UsageEvents.Event.MOVE_TO_FOREGROUND)
        }
        var eventCnt = 0
        while (events.hasNextEvent()) {
            events.getNextEvent(ev)
            eventCnt++
            if (ev.eventType in fgTypes && ev.timeStamp >= lastTime) {
                lastTime = ev.timeStamp
                lastPkg = ev.packageName
            }
        }
        Log.d(TAG, "currentForeground: events=$eventCnt lastPkg=$lastPkg")
        if (lastPkg == null) return null
        val o = JSONObject()
        o.put("pkg", lastPkg)
        o.put("label", appLabel(ctx, lastPkg))
        o.put("ts", lastTime)
        return o
    }

    /** 常见系统/厂商包名前缀黑名单（HyperOS/MIUI/高通/原生等未打 FLAG_SYSTEM 的预装组件） */
    private val SYSTEM_PKG_PREFIXES = listOf(
        "android",
        "com.android.",
        "com.google.android.",
        "com.qualcomm.",
        "com.qti.",
        "com.miui.",
        "com.mi.",
        "com.xiaomi.",
        "com.bsp.",
        "com.mobiletools.",
        "com.huawei.",
        "com.samsung.",
        "com.lge.",
        "com.oneplus.",
        "com.coloros.",
        "com.oppo.",
        "com.vivo.",
        "com.realme.",
        "com.heytap.",
        "com.meizu.",
        "com.smartisan.",
        "com.lenovo.",
        "com.zui.",
        "com.motorola.",
        "com.sony.",
        "com.asus.",
        "com.mediatek.",
        "com.sec.",
        "com.sonyericsson.",
        "com.amazon."
    )

    /**
     * 综合判断是否为系统应用（含预装/厂商组件）。
     * 红米/HyperOS 上很多系统组件没有 FLAG_SYSTEM，必须辅以 sourceDir 路径和包名黑名单。
     */
    private fun isSystemApp(ctx: Context, ai: ApplicationInfo): Boolean {
        // 排除本应用自己
        if (ai.packageName == ctx.packageName) return true
        // 1. Android 标准系统标志
        if ((ai.flags and ApplicationInfo.FLAG_SYSTEM) != 0) return true
        // 2. sourceDir 落在系统分区
        val src = ai.sourceDir ?: ai.publicSourceDir
        if (src != null && (
                    src.startsWith("/system/") ||
                    src.startsWith("/vendor/") ||
                    src.startsWith("/product/") ||
                    src.startsWith("/system_ext/") ||
                    src.startsWith("/oem/") ||
                    src.startsWith("/priv-app/")
                )) return true
        // 3. 包名命中常见系统/厂商前缀
        val pkg = ai.packageName ?: ""
        for (p in SYSTEM_PKG_PREFIXES) {
            if (pkg == p || pkg.startsWith(p)) return true
        }
        return false
    }

    /** 按包名判断是否为系统应用（取不到 ApplicationInfo 时保守视为系统）。 */
    private fun isSystemAppByPkg(ctx: Context, pkg: String): Boolean {
        val ai = runCatching {
            ctx.packageManager.getApplicationInfo(pkg, 0)
        }.getOrNull() ?: return true
        return isSystemApp(ctx, ai)
    }

    /**
     * 已装 App 列表：只返回用户安装的第三方应用。
     * 过滤规则：标准系统标志 / sourceDir 在系统分区 / 常见系统包名前缀 / 排除自身。
     *
     * Android 11+ 默认包可见性受限，必须声明 QUERY_ALL_PACKAGES 权限；
     * 同时用 MATCH_ALL 标志绕过额外过滤，提高在 HyperOS 等厂商系统上的枚举完整性。
     */
    fun collectInstalled(ctx: Context): JSONArray {
        val pm = ctx.packageManager
        val arr = JSONArray()
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            PackageManager.MATCH_ALL
        } else {
            PackageManager.GET_META_DATA
        }
        val apps = runCatching {
            pm.getInstalledApplications(flags)
        }.getOrNull() ?: runCatching {
            @Suppress("DEPRECATION")
            pm.getInstalledPackages(0).mapNotNull {
                runCatching { pm.getApplicationInfo(it.packageName ?: "", 0) }.getOrNull()
            }
        }.getOrNull() ?: emptyList()

        Log.d(TAG, "collectInstalled: raw count=${apps.size}, flags=$flags")
        var systemCnt = 0
        var userCnt = 0
        for (ai in apps) {
            val pkg = ai.packageName ?: continue
            if (isSystemApp(ctx, ai)) {
                systemCnt++
                continue
            }
            val label = runCatching { ai.loadLabel(pm)?.toString() }.getOrNull() ?: pkg
            val o = JSONObject()
            o.put("pkg", pkg)
            o.put("label", label)
            o.put("system", false)
            arr.put(o)
            userCnt++
        }
        Log.d(TAG, "collectInstalled: user=$userCnt system=$systemCnt final=${arr.length()}")
        if (arr.length() == 0) {
            // 某些 HyperOS 版本即便声明 QUERY_ALL_PACKAGES 也返回空，给前端一个可识别的提示项
            val hint = JSONObject()
            hint.put("pkg", "antitheft.empty")
            hint.put("label", "未能读取已装应用（请确认是最新 APK 并已授权）")
            hint.put("system", false)
            arr.put(hint)
        }
        return arr
    }

    /** 组装完整上传 payload */
    fun buildPayload(ctx: Context, days: Int = 60): JSONObject {
        Log.d(TAG, "buildPayload start")
        val jo = JSONObject()
        jo.put("usage", collectUsage(ctx, days))
        jo.put("sessions", collectSessions(ctx, 30))   // 30 天会话明细
        jo.put("foreground", currentForeground(ctx) ?: JSONObject.NULL)
        jo.put("installed", collectInstalled(ctx))
        jo.put("collected_at", System.currentTimeMillis())
        Log.d(TAG, "buildPayload end usage=${jo.optJSONArray("usage")?.length() ?: 0} sessions=${jo.optJSONArray("sessions")?.length() ?: 0} installed=${jo.optJSONArray("installed")?.length() ?: 0}")
        return jo
    }

    private fun appLabel(ctx: Context, pkg: String): String {
        return try {
            val pm = ctx.packageManager
            val ai = pm.getApplicationInfo(pkg, 0)
            ai.loadLabel(pm)?.toString() ?: pkg
        } catch (_: Exception) {
            pkg
        }
    }
}
