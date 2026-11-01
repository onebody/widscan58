# Tells src/CMakeLists.txt to compile net/** into this image. PlatformIO's
# build_flags never reach CMake (they go to the compiler only), so the marker
# has to travel through the environment the cmake subprocess inherits.
# GPL-3.0-or-later.
Import("env")  # noqa: F821 (SCons)

if any("RX_WIFI_RELAY" in str(f) for f in env.get("BUILD_FLAGS", [])):
    env["ENV"]["WIDSCAN_RELAY"] = "1"
    print("relay_build: WIDSCAN_RELAY=1 (relay image)")
