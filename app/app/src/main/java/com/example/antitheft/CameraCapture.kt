package com.example.antitheft

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.ImageFormat
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.ImageReader
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.util.Size
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Camera2 无预览抓拍前摄（用于远程拍照）。
 * 在前台服务(camera 类型)内调用，Android 11+ 才允许后台访问相机。
 * 同步等待 ImageReader 回调，超时 8s；成功返回 true 并把 JPEG 写入 out。
 */
object CameraCapture {
    private const val TAG = "CameraCapture"

    @SuppressLint("MissingPermission")
    fun capture(context: Context, out: File, facing: String = "front"): Boolean {
        val cm = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager

        // 拍照前自检：权限 + AppOps，定位 "disabled by policy" 的来源
        val granted = context.checkSelfPermission(android.Manifest.permission.CAMERA) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
        val opMode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            try {
                (context.getSystemService(Context.APP_OPS_SERVICE) as android.app.AppOpsManager)
                    .unsafeCheckOpNoThrow(
                        android.app.AppOpsManager.OPSTR_CAMERA,
                        android.os.Process.myUid(), context.packageName
                    )
            } catch (_: Exception) { -999 }
        } else -1
        Log.i(TAG, "precheck granted=$granted appOpsCamera=$opMode")
        if (!granted) {
            Log.e(TAG, "camera permission not granted, abort")
            return false
        }

        val wantFront = facing != "back"
        val targetId = cm.cameraIdList.firstOrNull { id ->
            val f = cm.getCameraCharacteristics(id)
                .get(android.hardware.camera2.CameraCharacteristics.LENS_FACING)
            if (wantFront) f == android.hardware.camera2.CameraCharacteristics.LENS_FACING_FRONT
            else f == android.hardware.camera2.CameraCharacteristics.LENS_FACING_BACK
        } ?: run {
            Log.e(TAG, "no ${if (wantFront) "front" else "back"} camera found")
            return false
        }

        val chars = cm.getCameraCharacteristics(targetId)
        val map = chars.get(android.hardware.camera2.CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        val choices = map?.getOutputSizes(ImageFormat.JPEG) ?: arrayOf(Size(640, 480))
        // 限宽 <= 1280 取最大，控体积；取不到则用兜底
        val picked = choices.filter { it.width <= 1280 }.maxByOrNull { it.width * it.height } ?: choices[0]

        val reader = ImageReader.newInstance(picked.width, picked.height, ImageFormat.JPEG, 1)
        val done = CountDownLatch(1)
        var ok = false
        var dev: CameraDevice? = null
        val thread = HandlerThread("at-cam").also { it.start() }
        val handler = Handler(thread.looper)

        // 拍照期间保持 CPU 唤醒，避免息屏下 MIUI 更严的后台相机策略
        val pm = context.getSystemService(Context.POWER_SERVICE) as android.os.PowerManager
        val wl = pm.newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK, "at:cam")
        try { wl.acquire(12_000) } catch (_: Exception) {}

        reader.setOnImageAvailableListener({ r ->
            try {
                val img = r.acquireLatestImage()
                if (img != null) {
                    val buf = img.planes[0].buffer
                    val bytes = ByteArray(buf.remaining())
                    buf.get(bytes)
                    img.close()
                    out.writeBytes(bytes)
                    ok = true
                }
            } catch (e: Exception) {
                Log.e(TAG, "read image fail", e)
            } finally {
                done.countDown()
            }
        }, handler)

        val stateCb = object : CameraDevice.StateCallback() {
            override fun onOpened(camera: CameraDevice) {
                dev = camera
                try {
                    val builder = camera.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE)
                    builder.addTarget(reader.surface)
                    camera.createCaptureSession(listOf(reader.surface), object : CameraCaptureSession.StateCallback() {
                        override fun onConfigured(session: CameraCaptureSession) {
                            try {
                                session.capture(builder.build(), null, handler)
                            } catch (e: Exception) {
                                Log.e(TAG, "capture fail", e)
                                done.countDown()
                            }
                        }

                        override fun onConfigureFailed(session: CameraCaptureSession) {
                            Log.e(TAG, "configure failed")
                            done.countDown()
                        }
                    }, handler)
                } catch (e: Exception) {
                    Log.e(TAG, "create session fail", e)
                    done.countDown()
                }
            }

            override fun onDisconnected(camera: CameraDevice) {
                camera.close()
                done.countDown()
            }

            override fun onError(camera: CameraDevice, error: Int) {
                Log.e(TAG, "open error $error")
                camera.close()
                done.countDown()
            }
        }

        try {
            cm.openCamera(targetId, stateCb, handler)
        } catch (e: Exception) {
            Log.e(TAG, "openCamera ex", e)
            done.countDown()
        }

        try {
            done.await(8, TimeUnit.SECONDS)
        } catch (e: InterruptedException) {
            Log.e(TAG, "await interrupted", e)
        } finally {
            try { dev?.close() } catch (_: Exception) {}
            try { reader.close() } catch (_: Exception) {}
            try { if (wl.isHeld) wl.release() } catch (_: Exception) {}
            thread.quitSafely()
        }
        return ok
    }

    /** 向后兼容别名：默认前摄。 */
    @SuppressLint("MissingPermission")
    fun captureFront(context: Context, out: File): Boolean = capture(context, out, "front")
}
