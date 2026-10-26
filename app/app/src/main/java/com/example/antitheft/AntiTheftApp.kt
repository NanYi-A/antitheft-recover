package com.example.antitheft

import android.app.Application
import com.tencent.map.geolocation.TencentLocationManager

/**
 * Application：进程启动时，若已注册则恢复 WorkManager 轮询。
 * 腾讯定位 SDK 隐私合规声明（7.4.6+ 强制，必须在 requestLocationUpdates 前调用）。
 */
class AntiTheftApp : Application() {
    override fun onCreate() {
        super.onCreate()
        // 隐私合规：必须在任何定位请求前调用，否则腾讯 SDK 不返回定位
        runCatching { TencentLocationManager.setUserAgreePrivacy(true) }
        runCatching {
            if (Prefs.isRegistered(this)) {
                PollWorker.schedule(this)
                // 进程被拉起（含 WorkManager 触发）即恢复常驻守护，保证不丢连接
                KeepAliveService.start(this)
                // 充电自动复活链
                ChargeResumeWorker.schedule(this)
            }
        }
    }
}
