#!/bin/bash
set -e

PACKAGE_NAME="spotify-downloader-gui"
VERSION="1.0"
BUILD_DIR="build/${PACKAGE_NAME}_${VERSION}_all"

echo "Building ${PACKAGE_NAME} version ${VERSION}..."

# Clean previous build
rm -rf build

# Create directory structure
mkdir -p "${BUILD_DIR}/DEBIAN"
mkdir -p "${BUILD_DIR}/usr/bin"
mkdir -p "${BUILD_DIR}/usr/share/${PACKAGE_NAME}"
mkdir -p "${BUILD_DIR}/usr/share/applications"

# Copy debian control file
cp deb_resources/control "${BUILD_DIR}/DEBIAN/"

# Copy desktop entry
cp deb_resources/spotify-downloader.desktop "${BUILD_DIR}/usr/share/applications/"

# Copy python sources
cp gui.py "${BUILD_DIR}/usr/share/${PACKAGE_NAME}/"
cp main.py "${BUILD_DIR}/usr/share/${PACKAGE_NAME}/"

# Create launcher script
LAUNCHER="${BUILD_DIR}/usr/bin/${PACKAGE_NAME}"
cat > "${LAUNCHER}" <<EOF
#!/bin/bash
exec python3 /usr/share/${PACKAGE_NAME}/gui.py "\$@"
EOF

chmod +x "${LAUNCHER}"

# Set permissions
chmod 755 "${BUILD_DIR}/DEBIAN"
chmod 755 "${BUILD_DIR}/DEBIAN/control"
chmod -R 755 "${BUILD_DIR}/usr"

# Build package
dpkg-deb --build "${BUILD_DIR}"

echo "SUCCESS! Package created at: build/${PACKAGE_NAME}_${VERSION}_all.deb"
