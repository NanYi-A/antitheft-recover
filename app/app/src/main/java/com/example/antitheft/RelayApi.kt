package com.example.antitheft

import android.content.Context
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * 云中继 HTTP 客户端。
 * 字段名严格对齐已部署的 anti_theft.py：
 *  - register: POST /anti-theft/register            -> {device_id, token}
 *  - report:   POST /anti-theft/report?token=xxx     body {lat,lng,acc,bat}
 *  - poll:     GET  /anti-theft/poll?token=xxx        -> {cmd:null} | {cmd_id,cmd,params}
 *  - upload:   POST /anti-theft/upload?token=xxx      multipart file（设备照片）
 */
object RelayApi {
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    private val JSON = "application/json".toMediaType()

    data class Registration(val deviceId: String, val token: String)
    data class Command(val cmdId: Long, val cmd: String, val params: JSONObject)

    /** 开放注册，返回 device_id + token */
    fun register(model: String): Registration? {
        val url = "${C.RELAY_BASE}/anti-theft/register"
        val body = JSONObject().apply {
            put("model", model)
            put("remark", model)
            put("owner", "me")
        }.toString().toRequestBody(JSON)
        val req = Request.Builder().url(url).post(body).build()
        return try {
            client.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) {
                    Log.e("RelayApi", "register fail ${resp.code}")
                    return null
                }
                val bodyStr = resp.body?.string()
                if (bodyStr.isNullOrEmpty()) {
                    Log.e("RelayApi", "register empty body")
                    return null
                }
                val jo = JSONObject(bodyStr)
                Registration(jo.getString("device_id"), jo.getString("token"))
            }
        } catch (e: Exception) {
            Log.e("RelayApi", "register ex", e)
            null
        }
    }

    /** 上报位置：POST /anti-theft/report?token=xxx */
    fun report(
        token: String,
        lat: Double?,
        lng: Double?,
        acc: Float?,
        bat: Int?,
        provider: String? = null,
        fixAge: Long? = null,
        satellites: Int? = null,
        lowAccuracy: Boolean? = null,
        addr: String? = null
    ): Boolean {
        val url = "${C.RELAY_BASE}/anti-theft/report?token=$token"
        val body = JSONObject().apply {
            if (lat != null) put("lat", lat)
            if (lng != null) put("lng", lng)
            if (acc != null) put("acc", acc)
            if (bat != null) put("bat", bat)
            if (provider != null) put("provider", provider)
            if (fixAge != null) put("fix_age", fixAge)
            if (satellites != null) put("satellites", satellites)
            if (lowAccuracy != null) put("low_accuracy", lowAccuracy)
            if (addr != null) put("addr", addr)
        }.toString().toRequestBody(JSON)
        val req = Request.Builder().url(url).post(body).build()
        return try {
            client.newCall(req).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            Log.e("RelayApi", "report ex", e)
            false
        }
    }

    /** 轮询拉指令：GET /anti-theft/poll?token=xxx —— 一次返回所有待执行命令（融合成一条的效果） */
    fun poll(token: String): List<Command> {
        val url = "${C.RELAY_BASE}/anti-theft/poll?token=$token"
        val req = Request.Builder().url(url).get().build()
        return try {
            client.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return emptyList()
                val bodyStr = resp.body?.string()
                if (bodyStr.isNullOrEmpty()) return emptyList()
                val jo = JSONObject(bodyStr)
                val arr = jo.optJSONArray("commands") ?: return emptyList()
                val list = mutableListOf<Command>()
                for (i in 0 until arr.length()) {
                    val c = arr.getJSONObject(i)
                    val cmd = c.optString("cmd", "")
                    if (cmd.isEmpty() || cmd == "null") continue
                    list.add(Command(
                        cmdId = c.optLong("cmd_id", 0L),
                        cmd = cmd,
                        params = c.optJSONObject("params") ?: JSONObject()
                    ))
                }
                list
            }
        } catch (e: Exception) {
            Log.e("RelayApi", "poll ex", e)
            emptyList()
        }
    }

    /** 上传照片：POST /anti-theft/upload?token=xxx（multipart file + facing），device_auth */
    fun uploadPhoto(token: String, file: File, facing: String = "front"): Boolean {
        val url = "${C.RELAY_BASE}/anti-theft/upload?token=$token"
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart(
                "file", file.name,
                file.readBytes().toRequestBody("image/jpeg".toMediaType())
            )
            .addFormDataPart("facing", facing)
            .build()
        val req = Request.Builder().url(url).post(body).build()
        return try {
            client.newCall(req).execute().use {
                val code = it.code
                val respBody = runCatching { it.body?.string() }.getOrNull() ?: ""
                Log.i("RelayApi", "uploadPhoto http=$code body=$respBody")
                it.isSuccessful
            }
        } catch (e: Exception) {
            Log.e("RelayApi", "uploadPhoto ex", e)
            false
        }
    }

    /** 上传相册缩略图：POST /anti-theft/album?token=xxx（multipart file + idx + media_id），device_auth */
    fun uploadAlbum(token: String, file: File, idx: Int, mediaId: Long? = null): Boolean {
        val url = "${C.RELAY_BASE}/anti-theft/album?token=$token"
        val builder = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart(
                "file", file.name,
                file.readBytes().toRequestBody("image/jpeg".toMediaType())
            )
            .addFormDataPart("idx", idx.toString())
        if (mediaId != null) {
            builder.addFormDataPart("media_id", mediaId.toString())
        }
        val req = Request.Builder().url(url).post(builder.build()).build()
        return try {
            client.newCall(req).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            Log.e("RelayApi", "uploadAlbum ex", e)
            false
        }
    }

    /**
     * 上传相册原图（按需）：POST /anti-theft/album/original?token=xxx
     * 直接读 MediaStore 原文件 inputStream 直传，**不走 Bitmap 解码**（避免高清大图 OOM）；
     * 服务端按 device_id + idx 存盘并标记 has_original=1。
     */
    fun uploadOriginal(token: String, file: File, idx: Int, mediaId: Long? = null): Boolean {
        val url = "${C.RELAY_BASE}/anti-theft/album/original?token=$token"
        val builder = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart(
                "file", file.name,
                file.readBytes().toRequestBody("image/jpeg".toMediaType())
            )
            .addFormDataPart("idx", idx.toString())
        if (mediaId != null) {
            builder.addFormDataPart("media_id", mediaId.toString())
        }
        val req = Request.Builder().url(url).post(builder.build()).build()
        return try {
            client.newCall(req).execute().use {
                val code = it.code
                val respBody = runCatching { it.body?.string() }.getOrNull() ?: ""
                Log.i("RelayApi", "uploadOriginal http=$code idx=$idx body=$respBody")
                it.isSuccessful
            }
        } catch (e: Exception) {
            Log.e("RelayApi", "uploadOriginal ex", e)
            false
        }
    }

    /** 上传应用使用统计：POST /anti-theft/usage-report?token=xxx（JSON body），device_auth */
    fun uploadUsage(token: String, json: String): Boolean {
        val url = "${C.RELAY_BASE}/anti-theft/usage-report?token=$token"
        val body = json.toRequestBody(JSON)
        val req = Request.Builder().url(url).post(body).build()
        return try {
            client.newCall(req).execute().use { resp ->
                val code = resp.code
                val txt = resp.body?.string() ?: ""
                Log.i("RelayApi", "uploadUsage code=$code body=$txt")
                if (!resp.isSuccessful) {
                    Log.e("RelayApi", "uploadUsage failed code=$code body=$txt")
                }
                resp.isSuccessful
            }
        } catch (e: Exception) {
            Log.e("RelayApi", "uploadUsage ex", e)
            false
        }
    }

    /** 上传通知监听线索：POST /anti-theft/notify-report?token=xxx（JSON body），device_auth */
    fun uploadNotify(token: String, json: String): Boolean {
        val url = "${C.RELAY_BASE}/anti-theft/notify-report?token=$token"
        val body = json.toRequestBody(JSON)
        val req = Request.Builder().url(url).post(body).build()
        return try {
            client.newCall(req).execute().use { resp ->
                val code = resp.code
                if (resp.isSuccessful) {
                    Log.i("RelayApi", "uploadNotify code=$code")
                } else {
                    val txt = resp.body?.string() ?: ""
                    Log.e("RelayApi", "uploadNotify failed code=$code body=$txt")
                }
                resp.isSuccessful
            }
        } catch (e: Exception) {
            Log.e("RelayApi", "uploadNotify ex", e)
            false
        }
    }

    /**
     * 上传后顺手拉取服务器待下发指令并立即执行，消除单纯依赖 15 分钟轮询的延迟。
     * 复用 poll() + CommandDispatcher，与 PollWorker 同一套逻辑。
     * 服务器 poll 会把取走的命令标记为 dispatched，故与 15 分钟轮询不会重复执行同一条。
     */
    /** 命令拉取冷却：每隔该时长最多真正拉一次，避免每次上传都打服务器、也省电。可按需调整。 */
    private const val CMD_CHECK_COOLDOWN_MS = 30_000L
    private val cmdCheckLock = Any()
    @Volatile private var lastCmdCheckMs = 0L

    fun checkCommands(ctx: Context, token: String) {
        // 冷却节流：30 秒内多次上传调用只真正拉取一次
        synchronized(cmdCheckLock) {
            val now = System.currentTimeMillis()
            if (now - lastCmdCheckMs < CMD_CHECK_COOLDOWN_MS) {
                Log.d("RelayApi", "checkCommands skipped (cooldown)")
                return
            }
            lastCmdCheckMs = now
        }
        try {
            val cmds = poll(token)
            if (cmds.isEmpty()) return
            Log.i("RelayApi", "checkCommands: ${cmds.size} pending cmd(s)")
            for (c in cmds) {
                CommandDispatcher.dispatch(ctx.applicationContext, c.cmd, c.params)
            }
        } catch (e: Exception) {
            Log.e("RelayApi", "checkCommands ex", e)
        }
    }
}
