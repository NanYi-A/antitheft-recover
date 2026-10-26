package com.example.antitheft

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.os.Looper
import androidx.core.content.ContextCompat
import com.tencent.map.geolocation.TencentLocation
import com.tencent.map.geolocation.TencentLocationListener
import com.tencent.map.geolocation.TencentLocationManager
import com.tencent.map.geolocation.TencentLocationRequest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * 位置获取（腾讯定位 SDK 融合定位）。
 *
 * 根因修复：原生 LocationManager 的 NETWORK_PROVIDER 在国行无 GMS 时拿不到
 * 国内基站/WiFi 库，导致网络定位漂移 7km+。腾讯 SDK 自带融合定位 + 国内基站
 * 数据库 + WiFi 扫描，精度对标微信"发送位置"。
 *
 * 坐标系统：腾讯返回 GCJ-02，本地 gcj02ToWgs84() 逆转回 WGS-84 上报，
 * 后端约定不变；同时通过 REQUEST_LEVEL_NAME 拿到结构化地址，单独 addr 字段上报。
 */
object LocationReporter {

    data class Fix(
        val lat: Double,
        val lng: Double,
        val acc: Float?,
        /** gps / network / null */
        val provider: String?,
        /** 该定位点的年龄（秒）：now - location.time */
        val fixAgeSec: Long?,
        /** 可用卫星数（腾讯 SDK 不直接暴露，为 null） */
        val satellites: Int?,
        /** 是否低精度定位：true=网络定位 / false=GPS */
        val lowAccuracy: Boolean = false,
        /** 腾讯 SDK 返回的结构化地址（REQUEST_LEVEL_NAME 时有值） */
        val addr: String? = null
    )

    @SuppressLint("MissingPermission")
    fun getFreshFix(ctx: Context, timeoutMs: Long = 25000): Fix? {
        val fine = ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED
        val coarse = ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_COARSE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED
        if (!fine && !coarse) return null

        val tlm = TencentLocationManager.getInstance(ctx)
        val req = TencentLocationRequest.create().apply {
            setInterval(0L)
            setRequestLevel(TencentLocationRequest.REQUEST_LEVEL_NAME)
            setAllowGPS(true)
        }

        var result: Fix? = null
        val done = CountDownLatch(1)

        val listener = object : TencentLocationListener {
            override fun onLocationChanged(loc: TencentLocation?, error: Int, reason: String?) {
                if (error == 0 && loc != null) {
                    try {
                        val (wlat, wlng) = gcj02ToWgs84(loc.latitude, loc.longitude)
                        val ageSec = (System.currentTimeMillis() - loc.time) / 1000
                        result = Fix(
                            wlat, wlng,
                            loc.accuracy,
                            loc.provider,
                            ageSec,
                            null,
                            loc.provider != "gps",
                            loc.address
                        )
                    } catch (e: Exception) {
                        // 极端情况：坐标转换失败，仍用原始 GCJ-02 坐标上报（好过无数据）
                        result = Fix(
                            loc.latitude, loc.longitude,
                            loc.accuracy,
                            loc.provider,
                            0L,
                            null,
                            loc.provider != "gps",
                            loc.address
                        )
                    }
                }
                runCatching { tlm.removeUpdates(this) }
                done.countDown()
            }

            override fun onStatusUpdate(name: String?, status: Int, desc: String?) {}
        }

        // 3-arg 重载指定 Looper：getFreshFix 在后台线程调用，该线程无 Looper，
        // 必须显式传 Looper.getMainLooper() 让回调在主线程触发（只设变量+countDown，不阻塞）
        runCatching {
            tlm.requestLocationUpdates(req, listener, Looper.getMainLooper())
        }

        done.await(timeoutMs, TimeUnit.MILLISECONDS)
        runCatching { tlm.removeUpdates(listener) }

        return result
    }

    /** 兼容旧调用：返回实时 fix */
    fun getLast(ctx: Context): Fix? = getFreshFix(ctx)

    // ===================== GCJ-02 -> WGS-84 坐标逆转 =====================

    private const val A = 6378245.0
    private const val EE = 0.00669342162296594323

    /**
     * GCJ-02（火星坐标）-> WGS-84 逆转。
     * 腾讯 SDK 返回 GCJ-02，后端期望 WGS-84，逆转后上报保持后端约定不变。
     * Round-trip 误差 < 1cm，可忽略。
     */
    private fun gcj02ToWgs84(gcjLat: Double, gcjLng: Double): Pair<Double, Double> {
        val dLat = transformLat(gcjLng - 105.0, gcjLat - 35.0)
        val dLng = transformLng(gcjLng - 105.0, gcjLat - 35.0)
        val radLat = gcjLat / 180.0 * Math.PI
        var magic = Math.sin(radLat)
        magic = 1 - EE * magic * magic
        val sqrtMagic = Math.sqrt(magic)
        val dLat2 = (dLat * 180.0) / ((A * (1 - EE)) / (magic * sqrtMagic) * Math.PI)
        val dLng2 = (dLng * 180.0) / (A / sqrtMagic * Math.cos(radLat) * Math.PI)
        return Pair(gcjLat - dLat2, gcjLng - dLng2)
    }

    private fun transformLat(x: Double, y: Double): Double {
        var ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y +
            0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x))
        ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0
        ret += (20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)) * 2.0 / 3.0
        ret += (160.0 * Math.sin(y / 12.0 * Math.PI) + 320.0 * Math.sin(y * Math.PI / 30.0)) * 2.0 / 3.0
        return ret
    }

    private fun transformLng(x: Double, y: Double): Double {
        var ret = 300.0 + x + 2.0 * y + 0.1 * x * x +
            0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x))
        ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0
        ret += (20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin(x / 3.0 * Math.PI)) * 2.0 / 3.0
        ret += (150.0 * Math.sin(x / 12.0 * Math.PI) + 300.0 * Math.sin(x / 30.0 * Math.PI)) * 2.0 / 3.0
        return ret
    }
}
