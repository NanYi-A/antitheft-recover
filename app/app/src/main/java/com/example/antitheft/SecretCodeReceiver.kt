package com.example.antitheft

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * 暗码接收器：拨号盘输入 *#*#12345678#*#* 唤起设置页。
 * 这是隐藏 App 后唯一的本地入口（桌面无图标）。
 */
class SecretCodeReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action == "android.provider.Telephony.SECRET_CODE") {
            val code = intent.data?.host
            if (code == C.SECRET_CODE) {
                Log.i("SecretCode", "open settings")
                val i = Intent(ctx, SettingsActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                ctx.startActivity(i)
            }
        }
    }
}
