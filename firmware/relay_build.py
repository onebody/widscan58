# Tells src/CMakeLists.txt to compile net/** into this image. PlatformIO's
# build_flags never reach CMake (they go to the compiler only), so the marker
# has to travel through the environment the cmake subprocess inherits.
# GPL-3.0-or-later.
Import("env")  # noqa: F821 (SCons)

if any("RX_WIFI_RELAY" in str(f) for f in env.get("BUILD_FLAGS", [])):
    env["ENV"]["WIDSCAN_RELAY"] = "1"
    # espressif32's espidf builder only ever reads PROJECT_DIR/sdkconfig.defaults
    # (it never looks at a per-env `sdkconfig.defaults.<env>` name), and the
    # `custom_sdkconfig` option that could add one is Arduino-framework-only.
    # ESP-IDF itself does take a list, through $ENV{SDKCONFIG_DEFAULTS}
    # (tools/cmake/project.cmake), so the relay layer rides the same env the
    # marker uses. The base file must be listed first: setting this variable
    # suppresses IDF's own sdkconfig.defaults fallback.
    env["ENV"]["SDKCONFIG_DEFAULTS"] = (
        "sdkconfig.defaults;sdkconfig.defaults.esp32s3-relay"
    )
    print("relay_build: WIDSCAN_RELAY=1, relay sdkconfig layering (relay image)")
