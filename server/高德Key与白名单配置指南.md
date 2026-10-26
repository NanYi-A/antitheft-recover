# 高德开放平台 Key 与白名单配置指南

> 本项目用到高德开放平台的**两套独立凭证**，很多人会在这里踩坑。这份文档把两者的区别、申请步骤、白名单怎么填、部署时怎么注入一次讲清楚。

---

## 一、先搞清楚：你要申请两个 Key，不是一个

| | **Web 服务 Key** | **Web 端(JS API) Key** |
|---|---|---|
| 控制台「服务平台」选 | Web服务 | Web端(JS API) |
| 本项目用在哪 | `restapi.amap.com/v3/geocode/regeo` —— 服务端逆地理，把经纬度转成可读地址 | `webapi.amap.com/maps?v=2.0` —— 控制台网页里加载地图底图 |
| 谁发起请求 | 你的服务器（后端） | 用户的浏览器（前端） |
| 安全限制 | **IP 白名单**（填服务器公网 IP） | **域名白名单**（填站点域名）+ **安全密钥 securityJsCode** |
| 泄露后果 | 别人拿不到（IP 不匹配直接报 `10005 INVALID_USER_IP`） | 别人可能拿去刷你的配额（除非配了域名白名单） |
| 配额（个人认证） | 5000 次/日 | 5000 次/日（搜索类只有 100 次/日） |

**为什么 JS Key 必然在前端可见**：JS API 的脚本标签就是 `<script src="...?key=xxx">`，浏览器必须拿到这个 key 才能加载地图。所以「JS Key 出现在网页源码里」是设计如此，不是漏洞 —— 高德正是靠**域名白名单**来解决这个问题的。

---

## 二、Web 端(JS API) Key —— 你要配的那个

### 第 1 步：创建 Key

1. 登录 [高德开放平台控制台](https://console.amap.com/)
2. 左侧 **应用管理 → 我的应用**
3. 找到你的应用（没有就点右上角「创建新应用」）
4. 点应用右侧的 **「添加Key」**
5. 填写：
   - **Key 名称**：随便起，比如 `防盗控制台JS`
   - **服务平台**：选 **Web端(JS API)** ← 千万别选成 Android 或 Web服务
   - **域名白名单**：填你的站点域名，见下

### 第 2 步：域名白名单怎么填（最容易错的地方）

**格式：只写域名，不带协议、不带路径、不带端口。**

```
✅ 正确：relay.xienanyi.xyz
✅ 正确：www.example.com
✅ 正确：localhost
✅ 正确：127.0.0.1

❌ 错误：https://relay.xienanyi.xyz      （带协议）
❌ 错误：relay.xienanyi.xyz/console      （带路径）
❌ 错误：relay.xienanyi.xyz:443          （带端口）
❌ 错误：*.xienanyi.xyz                  （高德新版不支持通配符）
```

多个域名**换行分开填**，一行一个。

> ⚠️ **版本差异**：高德控制台不同版本对格式要求不完全一致 —— 有的版本要写完整 Origin（`https://relay.xienanyi.xyz`），有的只写域名。**最稳的做法**：先按「只写域名」提交，然后打开控制台看地图是否正常加载；如果浏览器控制台报 `INVALID_USER_DOMAIN`，就改成带协议的形式再试一次。

### 第 3 步：拿「安全密钥」（必做，别跳过）

**2021 年 12 月 2 日之后创建的 Key，必须配合安全密钥使用**，否则地图接口报 `INVALID_USER_SCODE` 或干脆不响应。

在应用列表里，Key 那一列下面会显示一行 **「安全密钥：xxxxxxxxxxxx」**（一串 32 位字符）。点「添加Key」/「设置」时弹窗里也能看到。

它和安全密钥的关系：
- **Key** = 告诉高德「我是谁」
- **安全密钥** = 告诉高德「这个请求确实是我发的，不是别人盗用我的 Key」

### 第 4 步：控制台页面里怎么用（本项目已内置）

本项目的控制台 HTML 由服务端渲染，两个值都从**环境变量**注入，不硬编码：

```bash
export AMAP_JS_KEY=你的JS端Key
export AMAP_JS_CODE=你的安全密钥
```

服务端会把它们替换进 HTML：

```html
<script>
  window._AMapSecurityConfig = { securityJsCode: "{AMAP_JS_CODE}" };
</script>
<script src="https://webapi.amap.com/maps?v=2.0&key={AMAP_JS_KEY}"></script>
```

> 注意加载顺序：`window._AMapSecurityConfig` **必须在** 加载地图 JS 脚本**之前**定义，否则无效。

### 更安全的做法：用 Nginx 代理转发（不把安全密钥暴露给前端）

如果你不想让安全密钥出现在浏览器里，可以让 Nginx 转发：

```nginx
location /_AMapService/ {
    set $args "$args&jscode=你的安全密钥";
    proxy_pass https://restapi.amap.com/;
}
```

前端改成：

```html
<script>
  window._AMapSecurityConfig = {
    serviceHost: "https://relay.xienanyi.xyz/_AMapService",
  };
</script>
```

这样安全密钥只存在于服务器，浏览器永远看不到。本项目默认用**明文方式**（简单够用），因为控制台本身也需要登录 Token 才能操作。

---

## 三、Web 服务 Key（服务端逆地理用）

### 申请

同样在「应用管理」→ 点「添加Key」→ 服务平台选 **Web服务**。

### IP 白名单

**不是域名白名单，是 IP 白名单。** 在 Key 的设置弹窗里找到「IP白名单」那一栏，填你服务器的**公网 IP**（`121.40.94.194` 这种）。

配了之后，只有来自这些 IP 的请求会被高德正常响应，其他一律返回 `10005 INVALID_USER_IP`。**这就是为什么 Web 服务 Key 泄露了也不危险** —— 攻击者从自己服务器发请求，IP 不匹配，直接被拒。

### 部署时的注入

```bash
export AMAP_WEB_KEY=你的Web服务Key
```

服务端在 `anti_theft.py` 里的用法：

```python
AMAP_WEB_KEY = os.environ.get("AMAP_WEB_KEY", "")
...
url = ("https://restapi.amap.com/v3/geocode/regeo"
       f"?location={glng:.6f},{glat:.6f}&key={AMAP_WEB_KEY}"
       "&radius=1000&extensions=all&roadlevel=0&language=zh_CN")
```

**没配会怎样**：会打印一行警告，然后回退到 Nominatim（OpenStreetMap 的公开服务）。国内访问 Nominatim 慢且经常失败，逆地理基本用不了。

---

## 四、supervisor 完整配置示例

```ini
[program:antitheft]
directory=/www/wwwroot/antitheft-relay
command=/www/wwwroot/antitheft-relay/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8001 --workers 1
autostart=true
autorestart=true
environment=CTRL_TOKEN="你的token",AMAP_WEB_KEY="你的webservicekey",AMAP_JS_KEY="你的jskey",AMAP_JS_CODE="你的安全密钥"
```

改完 `supervisorctl reread && supervisorctl update && supervisorctl restart antitheft`。

---

## 五、自查清单

| 检查项 | 怎么验 | 出错表现 |
|---|---|---|
| JS Key 平台选对了 | 控制台「服务平台」列显示 `Web端` | 选错平台 → 地图完全加载不出来，报 `INVALID_USER_KEY` |
| 域名白名单格式 | 浏览器 F12 → Network → 看 `webapi.amap.com` 请求的 Referer | 报 `INVALID_USER_DOMAIN` |
| 白名单生效 | 改完等 3–5 分钟再测 | 刚改完立刻测会失败（缓存没刷新） |
| 安全密钥已注入 | 看控制台网页源码里 `securityJsCode` 是否有值 | 报 `INVALID_USER_SCODE`，或地图加载了但搜索/定位无响应 |
| Web 服务 Key 的 IP 白名单 | 从**另一台机器**调一次逆地理 | 报 `10005 INVALID_USER_IP` |
| 逆地理能出中文地址 | 控制台点某台设备看位置 | 慢或失败 → 多半是 `AMAP_WEB_KEY` 没配或没加 IP 白名单 |

---

## 六、万一 Key 泄露了怎么办

1. **立刻去控制台禁用或删除该 Key** —— 比任何清理仓库的动作都快
2. 高德控制台 → 应用管理 → 找到 Key → 删除
3. 重新申请一个，配好白名单
4. 更新服务器环境变量并重启服务

**判断泄露影响**：
- Web 服务 Key → 配了 IP 白名单的话，别人即使拿到也调不动，**不急**（但还是建议换）
- JS Key → 别人能刷你的配额（搜索类只有 100 次/日，恶意刷的话你的地图就没法用了），**要换**

> **预防永远比补救便宜**：域名白名单 3 分钟配好，能省掉所有后续麻烦。
