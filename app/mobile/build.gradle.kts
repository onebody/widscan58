import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// The signing key lives outside the repository. `keystore.properties` at the project root is
// git-ignored; without it the release build still compiles, it just comes out unsigned.
val keystoreProperties = Properties().apply {
    val properties = rootProject.file("keystore.properties")
    if (properties.exists()) properties.inputStream().use { load(it) }
}
val keystoreFile = keystoreProperties.getProperty("storeFile")

android {
    namespace = "com.onebody.widscan58"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.onebody.widscan58"
        minSdk = 26
        targetSdk = 36
        versionCode = 8
        versionName = "0.8"
    }

    signingConfigs {
        if (keystoreFile != null) {
            create("release") {
                storeFile = file(keystoreFile)
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (keystoreFile != null) signingConfig = signingConfigs.getByName("release")
        }
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(project(":core"))
    testImplementation("junit:junit:4.13.2")
}
