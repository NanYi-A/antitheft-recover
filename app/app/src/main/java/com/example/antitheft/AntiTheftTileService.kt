package com.example.antitheft

import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/**
 * 快捷设置瓷砖（Quick Settings Tile）：下拉通知栏的小开关。
 *
 * 作为拨号暗码 *#*#12345678#*#* 在 MIUI/HyperOS 上不发 SECRET_CODE 广播时的
 * 隐藏替代入口。label 伪装成"同步"，图标复用低调灰点，刻意不起眼。
 *
 * 用户点一下 -> 拉起设置页，可注册 / 激活设备管理员 / 授权权限。
 * （暗码依旧保留，vivo 等会广播 ROM 仍可生效，两者并存。）
 */
class AntiTheftTileService : TileService() {

    override fun onStartListening() {
        super.onStartListening()
        qsTile?.apply {
            state = Tile.STATE_ACTIVE
            label = getString(R.string.tile_label)
            updateTile()
        }
    }

    override fun onClick() {
        super.onClick()
        val intent = Intent(this, SettingsActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        // API 34 起 startActivityAndCollapse(Intent) 废弃，改用 PendingIntent 版；
        // 低版本只有 Intent 版，故按版本分流。
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            val pi = PendingIntent.getActivity(
                this, 0, intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            startActivityAndCollapse(pi)
        } else {
            @Suppress("DEPRECATION")
            startActivityAndCollapse(intent)
        }
    }
}
