plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.example.antitheft"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.example.antitheft"
        minSdk = 24
        targetSdk = 33
        versionCode = 1
        versionName = "1.0.0"
        // 只保留 arm64-v8a：腾讯定位 SDK 含 .so 原生库，
        // 单架构过滤可将增量从 ~3.9MB 压到 ~1.5-2MB
        ndk { abiFilters += "arm64-v8a" }

        // 腾讯定位 SDK Key：公开仓库中不内置真实 Key。
        // 使用时请在 local.properties 中配置：TencentMapKey=<your-key>
        // （local.properties 已被 .gitignore 忽略，不会误提交）
        manifestPlaceholders["TENCENT_MAP_KEY"] =
            (project.findProperty("TencentMapKey") as String?) ?: "YOUR_TENCENT_MAP_KEY"
    }

    signingConfigs {
        create("publicRelease") {
            // 公开仓库的 release 产物使用 debug keystore 签名，保证他人 clone 后
            // 无需任何私钥即可构建出「能直接安装」的 APK。
            // 正式发布请自行创建 keystore 并在 local.properties 中配置签名信息。
            storeFile = file(System.getProperty("user.home") + "/.android/debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            signingConfig = signingConfigs.getByName("publicRelease")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")

    // 后台周期任务（轮询拉指令）
    implementation("androidx.work:work-runtime-ktx:2.9.0")

    // HTTP 客户端
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // 加密存储设备凭证（1.1.0 才含 MasterKey 类；1.0.0 只有旧的 MasterKeys）
    implementation("androidx.security:security-crypto:1.1.0")

    // 生命周期（Application / Service 用）
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")

    // 腾讯定位 SDK（融合定位 + 自带逆地理，解决国行无 GMS 导致 NETWORK_PROVIDER 失效）
    implementation("com.tencent.map.geolocation:TencentLocationSdk-openplatform:7.6.1.8")
}
