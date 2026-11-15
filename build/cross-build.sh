#!/bin/bash
# Cross-compiles CuraEngine ${ENGINE_VERSION} for armv7hf (32-bit Raspberry Pi OS) or aarch64
# inside the image built from Dockerfile.cross, and packages it with the Cura resources.
#
#   docker build -t curaengine-cross -f build/Dockerfile.cross build
#   docker run --rm -v "$PWD/build:/build:ro" -v "$PWD/out:/out" curaengine-cross \
#       bash /build/cross-build.sh armv7hf     # or: aarch64
#
# Result: /out/curaengine-<version>-linux-<arch>.tar.xz (+ .sha256)
set -euo pipefail
TARGET=${1:?usage: cross-build.sh armv7hf|aarch64}
ENGINE_VERSION=${ENGINE_VERSION:-5.11.0}
case "$TARGET" in
  armv7hf) CONAN_ARCH=armv7hf; TRIPLE=arm-linux-gnueabihf; QEMU=qemu-arm-static ;;
  aarch64) CONAN_ARCH=armv8;   TRIPLE=aarch64-linux-gnu;   QEMU=qemu-aarch64-static ;;
  *) echo "unknown target $TARGET" >&2; exit 2 ;;
esac
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=${WORK:-/tmp/curaengine-build}
PKG=curaengine-$ENGINE_VERSION-linux-$TARGET
PATCH=$HERE/patches/curaengine-$ENGINE_VERSION-portability.patch
mkdir -p "$WORK" /out && cd "$WORK"

sed -e "s/@ARCH@/$CONAN_ARCH/g" -e "s/@TRIPLE@/$TRIPLE/g" "$HERE/cross.profile.in" > /root/.conan2/profiles/cross

[ -d CuraEngine ] || git clone -q --depth 1 --branch "$ENGINE_VERSION" https://github.com/Ultimaker/CuraEngine.git
[ -d Cura ] || {
  git clone -q --depth 1 --branch "$ENGINE_VERSION" --filter=blob:none --sparse https://github.com/Ultimaker/Cura.git
  git -C Cura sparse-checkout set resources/definitions resources/extruders resources/quality resources/intent resources/variants
}
[ -d fdm_materials ] || git clone -q --depth 1 --branch "$ENGINE_VERSION" https://github.com/Ultimaker/fdm_materials.git

# Portability patch (missing includes, 32-bit size_t); see NOTICE
if git -C CuraEngine apply --check "$PATCH" 2>/dev/null; then git -C CuraEngine apply "$PATCH"; fi

CONAN_OPTS=(-pr:h=cross -pr:b=default -s build_type=Release --build=missing -c tools.build:skip_test=True
  -o "curaengine/*:enable_arcus=False" -o "curaengine/*:enable_plugins=False"
  -o "clipper/*:shared=False" -o "libpng/*:neon=False"
  -o "onetbb/*:tbbproxy=False" -o "onetbb/*:tbbmalloc=False")

# onetbb 2022.2 adds the x86-only -fcf-protection=full unless CMAKE_SYSTEM_PROCESSOR matches
# "aarch64"; neutralize that in its sources (this first attempt downloads them, and may fail).
(cd CuraEngine && conan install . "${CONAN_OPTS[@]}" >/dev/null 2>&1 || true)
for f in $(find /root/.conan2/p -path '*onetb*' -name GNU.cmake); do
  sed -i 's|if (NOT CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64" AND NOT EMSCRIPTEN)|if (FALSE)|' "$f"
done

(cd CuraEngine && conan build . "${CONAN_OPTS[@]}")

BIN=$WORK/CuraEngine/build/Release/CuraEngine
rm -rf "$PKG" && mkdir -p "$PKG/resources/materials" "$PKG/lib" "$PKG/LICENSES"
cp "$BIN" "$PKG/CuraEngine"
"$TRIPLE-strip" "$PKG/CuraEngine"
# onetbb is always built as a shared library: ship it next to the engine (a real file, no symlink)
TBB=$(find /root/.conan2/p -path '*onetb*/p/lib/libtbb.so.12.*' -type f | head -1)
cp "$TBB" "$PKG/lib/libtbb.so.12"
"$TRIPLE-strip" --strip-unneeded "$PKG/lib/libtbb.so.12"
patchelf --set-rpath '$ORIGIN/lib' "$PKG/CuraEngine"
file "$PKG/CuraEngine"
cp -r Cura/resources/* "$PKG/resources/"
cp fdm_materials/*.xml.fdm_material "$PKG/resources/materials/"
cp CuraEngine/LICENSE "$PKG/LICENSES/CuraEngine-AGPL-3.0.txt"
cp Cura/LICENSE "$PKG/LICENSES/Cura-resources-LGPL-3.0.txt"
cp fdm_materials/LICENSE "$PKG/LICENSES/fdm_materials.txt"
cp "$PATCH" "$PKG/LICENSES/"
sed "s/@VERSION@/$ENGINE_VERSION/g" "$HERE/NOTICE.in" > "$PKG/NOTICE"
echo "$ENGINE_VERSION" > "$PKG/VERSION"

# smoke test under user-mode emulation
$QEMU -L "/usr/$TRIPLE" "$PKG/CuraEngine" help 2>&1 | head -3

tar -cJf "/out/$PKG.tar.xz" "$PKG"
(cd /out && sha256sum "$PKG.tar.xz" | tee "$PKG.tar.xz.sha256")
