pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // 腾讯定位 SDK maven 仓库（jcenter 已废弃，官方云镜像）
        maven { url = uri("https://mirrors.cloud.tencent.com/repository/maven/") }
    }
}

rootProject.name = "AntiTheft"
include(":app")
