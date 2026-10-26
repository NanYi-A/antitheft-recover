package com.example.antitheft

import android.app.admin.DeviceAdminReceiver

/**
 * 设备管理员接收器。启用后可 lockNow() / wipeData()。
 * 卸载 App 前必须先到系统设置里解除本管理员，形成防卸载障碍。
 * 无需覆写 onEnabled/onDisabled（基类默认实现即可）。
 */
class AntiTheftDeviceAdmin : DeviceAdminReceiver()
