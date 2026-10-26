package com.example.antitheft

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings

/**
 * 厂商 ROM 兼容：运行时识别品牌，给出保活引导文案与多个跳转 intent。
 *
 * 红米(MIUI/HyperOS) 实测踩过的坑（vivo 适配时一并规避）：
 *  1) 暗码拨号盘不发 SECRET_CODE 广播给第三方 App → 统一用 TileService 瓷砖入口（跨厂商通用）。
 *  2) WorkManager 周期任务被省电冻结 → 间歇离线、指令收不到 → 必须引导自启动+电池白名单。
 *  3) 后台定位「始终允许」批量弹窗拿不到 → 必须手动进权限页改。
 *  4) 前台服务通知强制显示 → 用 IMPORTANCE_MIN + 系统设置关渠道（已做）。
 *
 * vivo(OriginOS/Funtouch) 同样会清后台，保活引导与红米对齐；专用 Intent 失败一律回退应用详情页。
 */
object ManufacturerCompat {

    /** 单个保活步骤：文案标签 + 跳转 intent（intent=null 表示纯手动引导，无直达页） */
    data class CompatAction(val label: String, val intent: Intent?)

    data class Guidance(
        val brand: String,
        val title: String,
        val steps: String,
        val actions: List<CompatAction>
    )

    fun detect(ctx: Context): Guidance {
        val brand = Build.BRAND.lowercase()
        val manufacturer = Build.MANUFACTURER.lowercase()
        val isXiaomi = brand == "xiaomi" || brand == "redmi" || manufacturer == "xiaomi"
        val isVivo = brand == "vivo" || manufacturer == "vivo" || brand == "iqoo"

        return when {
            isXiaomi -> Guidance(
                "xiaomi",
                "小米 / 红米 (MIUI/HyperOS)",
                "红米实测坑：① WorkManager 被省电冻结→间歇离线；② 暗码失效→用瓷砖入口；③ 后台定位必须手动改成「始终允许」。\n" +
                        "请逐项打开（每项都会尝试跳到对应设置页，跳不开则回退应用详情页）：",
                listOf(
                    CompatAction("① 自启动管理 → 允许本应用", miuiAutostartIntent()),
                    CompatAction("② 电池 → 省电策略 → 无限制（忽略电池优化）", batteryOptIntent(ctx)),
                    CompatAction("③ 应用 → 权限 → 后台弹出界面 → 允许", appDetailsIntent(ctx)),
                    CompatAction("④ 最近任务锁定本应用（上滑/长按加锁，防清理）", null)
                )
            )

            isVivo -> Guidance(
                "vivo",
                "vivo / iQOO (OriginOS/Funtouch)",
                "vivo 适配（已规避红米踩过的坑）：① 暗码不稳定→统一用瓷砖入口（下拉通知栏点「同步」）；② OriginOS 清后台较狠→必须做以下保活；③ 后台定位需手动改成「始终允许」。\n" +
                        "请逐项打开（专用页跳不开会回退应用详情页）：",
                listOf(
                    CompatAction("① i管家 → 应用管理 → 自启动 → 允许本应用", vivoAutostartIntent()),
                    CompatAction("② 设置 → 电池 → 后台耗电 → 本应用允许后台高耗电（忽略电池优化）", batteryOptIntent(ctx)),
                    CompatAction("③ 设置 → 应用与权限 → 权限管理 → 后台弹出界面 → 允许", appDetailsIntent(ctx)),
                    CompatAction("④ 权限管理 → 定位 → 本应用 → 始终允许", appDetailsIntent(ctx)),
                    CompatAction("⑤ 最近任务锁定本应用（下拉加锁，防清理）", null)
                )
            )

            else -> Guidance(
                "other",
                "通用 (${Build.BRAND})",
                "请在系统设置中允许：自启动、后台运行、电池无限制（忽略电池优化），并授予定位「始终允许」/相机/通知权限。\n" +
                        "下拉通知栏点「同步」瓷砖可随时打开本设置页。",
                listOf(
                    CompatAction("① 忽略电池优化（允许后台运行）", batteryOptIntent(ctx)),
                    CompatAction("② 应用详情（改权限/自启动）", appDetailsIntent(ctx))
                )
            )
        }
    }

    private fun miuiAutostartIntent(): Intent? = runCatching {
        Intent().setComponent(
            ComponentName(
                "com.miui.securitycenter",
                "com.miui.permcenter.autoset.AutoSetActivity"
            )
        )
    }.getOrNull()

    private fun vivoAutostartIntent(): Intent? = runCatching {
        // OriginOS / Funtouch 的 i 管家自启动页，组件名随版本略有差异，失败回退应用详情页
        Intent().setComponent(
            ComponentName(
                "com.iqoo.secure",
                "com.iqoo.secure.ui.phoneoptimize.AddWhiteListActivity"
            )
        )
    }.getOrNull()

    /** 通用：打开系统「电池优化」列表，让用户把本应用设为不优化（红米/vivo/通用均适用） */
    private fun batteryOptIntent(ctx: Context): Intent? = runCatching {
        Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
    }.getOrNull() ?: runCatching {
        Intent(Settings.ACTION_BATTERY_SAVER_SETTINGS)
    }.getOrNull() ?: appDetailsIntent(ctx)

    /** 通用：应用详情页（可改权限、自启动、通知等） */
    private fun appDetailsIntent(ctx: Context): Intent = Intent(
        Settings.ACTION_APPLICATION_DETAILS_SETTINGS
    ).apply {
        data = Uri.fromParts("package", ctx.packageName, null)
    }
}
