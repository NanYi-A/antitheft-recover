package com.example.antitheft

import android.content.ContentUris
import android.content.ContentResolver
import android.content.Context
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import android.util.Log
import android.util.Size
import java.io.File

/**
 * 从系统媒体库读取图片缩略图（手动触发用，不静默）。
 * - collectWindow：按时间窗（毫秒，0=不限制）扫 MediaStore，返回 _id + 本地临时 jpg，调用方负责上传并清理。
 * - 增量同步由调用方用 _id 配合白名单去重（见 AntiTheftService + Prefs）。
 *
 * 权限：Android 13+ 需 READ_MEDIA_IMAGES；以下需 READ_EXTERNAL_STORAGE。
 * 这些都已在 AndroidManifest 声明，且用户已在设置页授权。
 */
object AlbumCollector {
    private const val TAG = "AlbumCollector"
    private const val THUMB_SIZE = 2048
    private const val THUMB_QUALITY = 92

    data class AlbumItem(val id: Long, val file: File)

    fun collectWindow(context: Context, fromTs: Long, toTs: Long): List<AlbumItem> {
        val out = mutableListOf<AlbumItem>()
        try {
            val cr = context.contentResolver
            val uri: Uri = MediaStore.Images.Media.EXTERNAL_CONTENT_URI
            val proj = arrayOf(
                MediaStore.Images.Media._ID,
                MediaStore.Images.Media.DATE_ADDED,
                MediaStore.Images.Media.DATE_TAKEN
            )
            val sort = "${MediaStore.Images.Media.DATE_ADDED} DESC"
            cr.query(uri, proj, null, null, sort)?.use { cur ->
                val idIdx = cur.getColumnIndexOrThrow(MediaStore.Images.Media._ID)
                val addedIdx = cur.getColumnIndexOrThrow(MediaStore.Images.Media.DATE_ADDED)
                val takenIdx = cur.getColumnIndexOrThrow(MediaStore.Images.Media.DATE_TAKEN)
                while (cur.moveToNext()) {
                    val id = cur.getLong(idIdx)
                    // DATE_ADDED 为秒，DATE_TAKEN 为毫秒（部分机型缺失/为0），统一归一到毫秒再判窗
                    val added = cur.getLong(addedIdx) * 1000L
                    val taken = if (cur.isNull(takenIdx)) 0L else cur.getLong(takenIdx)
                    val ts = if (taken > 1_000_000_000_000L) taken else added
                    if (fromTs > 0L && ts < fromTs) continue
                    if (toTs > 0L && ts > toTs) continue
                    val imgUri = ContentUris.withAppendedId(uri, id)
                    try {
                        val thumb = loadThumb(cr, imgUri, id) ?: continue
                        val f = File(
                            context.cacheDir,
                            "at_album_${id}_${System.currentTimeMillis()}.jpg"
                        )
                        f.outputStream().use { os ->
                            thumb.compress(Bitmap.CompressFormat.JPEG, THUMB_QUALITY, os)
                        }
                        thumb.recycle()
                        out.add(AlbumItem(id, f))
                    } catch (e: Exception) {
                        Log.e(TAG, "thumb fail id=$id", e)
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "collectWindow ex", e)
        }
        return out
    }

    private fun loadThumb(cr: ContentResolver, imgUri: Uri, id: Long): Bitmap? {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            try {
                cr.loadThumbnail(imgUri, Size(THUMB_SIZE, THUMB_SIZE), null)
            } catch (_: Exception) {
                null
            }
        } else {
            @Suppress("DEPRECATION")
            MediaStore.Images.Thumbnails.getThumbnail(
                cr, id, MediaStore.Images.Thumbnails.MINI_KIND, null
            )
        }
    }
}
