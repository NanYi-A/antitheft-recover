package com.example.antitheft

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * 设备凭证存储（加密 SharedPreferences）。
 * 存 device_id / token / model，卸载 App 即清空。
 */
object Prefs {
    private const val FILE = "at_prefs"

    private fun sp(ctx: Context): SharedPreferences {
        val mk = MasterKey.Builder(ctx)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        return EncryptedSharedPreferences.create(
            ctx,
            FILE,
            mk,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        )
    }

    // 读取可能在大解锁前（boot 未完成）抛异常，统一兜底为 null
    fun getDeviceId(ctx: Context): String? = runCatching { sp(ctx).getString("device_id", null) }.getOrNull()
    fun getToken(ctx: Context): String? = runCatching { sp(ctx).getString("token", null) }.getOrNull()
    fun getModel(ctx: Context): String? = runCatching { sp(ctx).getString("model", null) }.getOrNull()

    fun saveDevice(ctx: Context, deviceId: String, token: String, model: String) {
        sp(ctx).edit()
            .putString("device_id", deviceId)
            .putString("token", token)
            .putString("model", model)
            .apply()
    }

    // 高精度定位模式（默认开）：强制 GPS 优先，网络定位仅兜底，加精度门限过滤漂移点。
    fun getHighPrecision(ctx: Context): Boolean =
        runCatching { sp(ctx).getBoolean(KEY_HIGH_PRECISION, true) }.getOrNull() ?: true

    fun setHighPrecision(ctx: Context, on: Boolean) {
        runCatching { sp(ctx).edit().putBoolean(KEY_HIGH_PRECISION, on).apply() }
    }

    fun isRegistered(ctx: Context): Boolean = getToken(ctx) != null

    // 相册增量同步白名单：已成功上传的 MediaStore _id 集合（持久化，防重复上传/被杀续传）
    private const val KEY_ALBUM_WL = "album_whitelist"

    fun getAlbumWhitelist(ctx: Context): MutableSet<Long> =
        runCatching {
            sp(ctx).getStringSet(KEY_ALBUM_WL, emptySet())
                ?.mapNotNull { it.toLongOrNull() }?.toMutableSet() ?: mutableSetOf()
        }.getOrNull() ?: mutableSetOf()

    fun addAlbumUploaded(ctx: Context, ids: Set<Long>) {
        runCatching {
            val sp = sp(ctx)
            val cur = sp.getStringSet(KEY_ALBUM_WL, mutableSetOf())?.toMutableSet()
                ?: mutableSetOf()
            ids.forEach { cur.add(it.toString()) }
            sp.edit().putStringSet(KEY_ALBUM_WL, cur).apply()
        }
    }

    /** ids 为 null = 清空整个白名单；否则只移除该时间窗内的 _id（选择性重置基线） */
    fun clearAlbumWhitelist(ctx: Context, ids: Set<Long>? = null) {
        runCatching {
            val sp = sp(ctx)
            if (ids == null) {
                sp.edit().putStringSet(KEY_ALBUM_WL, mutableSetOf()).apply()
            } else {
                val cur = sp.getStringSet(KEY_ALBUM_WL, mutableSetOf())?.toMutableSet()
                    ?: mutableSetOf()
                ids.forEach { cur.remove(it.toString()) }
                sp.edit().putStringSet(KEY_ALBUM_WL, cur).apply()
            }
        }
    }

    private const val KEY_HIGH_PRECISION = "high_precision"
}
