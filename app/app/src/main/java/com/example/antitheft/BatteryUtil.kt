package com.example.antitheft

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import android.provider.Settings

object BatteryUtil {
    fun level(ctx: Context): Int {
        val bm = ctx.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        return bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
    }

    /** 是否已被系统加入电池优化白名单（Doze 不冻结、后台不限制） */
    fun isIgnoringBatteryOptimizations(ctx: Context): Boolean {
        val pm = ctx.getSystemService(Context.POWER_SERVICE) as PowerManager
        return pm.isIgnoringBatteryOptimizations(ctx.packageName)
    }

    /** 跳到系统弹窗，请求把本应用设为「不优化」 */
    fun requestIgnoreIntent(ctx: Context): Intent {
        return Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
            data = Uri.fromParts("package", ctx.packageName, null)
        }
    }

    /** 是否为小米 / 红米 / POCO（HyperOS / MIUI 上标准电池优化弹窗无效，需手动开启） */
    fun isXiaomi(): Boolean {
        val b = Build.BRAND.lowercase()
        return b.contains("xiaomi") || b.contains("redmi") || b.contains("poco")
    }

    /**
     * 跳到小米「应用智能省电 / 省电策略」总列表（MIUI / HyperOS）。
     * 在该列表里可以找到「系统同步服务」并手动设为「无限制」。
     */
    fun openMiuiPowerSaveList(ctx: Context): Boolean {
        val candidates = listOf(
            "com.miui.powercenter/.ui.MainActivity",
            "com.miui.powercenter/.ui.BatteryActivity",
            "com.miui.powercenter/.ui.PowerSaveActivity",
            "com.miui.securitycenter/com.miui.powercenter.activity.PowerCenterMainActivity",
            "com.miui.securitycenter/com.miui.powercenter.PowerCenterActivity"
        )
        return launchComponent(ctx, candidates)
    }

    /**
     * 跳到小米「自启动管理」设置页（MIUI 深链，依赖 securitycenter 版本）。
     * 能解析到可启动的 Activity 就直接打开并返回 true，否则返回 false 由调用方回退。
     */
    fun openMiuiAutoStart(ctx: Context): Boolean {
        val candidates = listOf(
            "com.miui.securitycenter/com.miui.permcenter.autosettings.AutoSetPolicyActivity",
            "com.miui.securitycenter/com.miui.powercenter.activity.PowerSettingsActivity",
            "com.miui.securitycenter/com.miui.powercenter.PowerSettings",
            "com.miui.securitycenter/com.miui.permcenter.activity.PermCenterActivity"
        )
        return launchComponent(ctx, candidates)
    }

    private fun launchComponent(ctx: Context, candidates: List<String>): Boolean {
        val pm = ctx.packageManager
        for (c in candidates) {
            try {
                val sep = c.indexOf('/')
                val pkg = c.substring(0, sep)
                val cls = c.substring(sep + 1)
                val intent = Intent().setComponent(ComponentName(pkg, cls))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                if (pm.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY) != null) {
                    ctx.startActivity(intent)
                    return true
                }
            } catch (_: Exception) {
                // 该候选不可用，尝试下一个
            }
        }
        return false
    }
}
