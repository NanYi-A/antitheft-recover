# ProGuard / R8 规则
# 目标：release APK 体积最小化，同时保证运行时反射调用的组件不被裁剪。

# ---- 腾讯定位 SDK：原生库通过 JNI 调用，必须保留 ----
-keep class com.tencent.map.** { *; }
-keep class com.tencent.lbs.** { *; }
-dontwarn com.tencent.map.**
-dontwarn com.tencent.lbs.**

# ---- 加密存储（androidx.security-crypto）-------
-keep class androidx.security.crypto.** { *; }

# ---- OkHttp / Okio：部分版本含可选依赖 ----
-dontwarn okhttp3.**
-dontwarn okio.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**

# ---- WorkManager：后台任务通过反射实例化 Worker ----
-keep class androidx.work.impl.** { *; }
-dontwarn androidx.work.**

# ---- 保留注解与行号，便于线上崩溃定位 ----
-keepattributes *Annotation*, InnerClasses, Signature, Exceptions, SourceFile, LineNumberTable
