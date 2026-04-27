#!/bin/bash
# Copyright (C) 2026 OpenMV, LLC.
#
# This software is licensed under terms that can be found in the
# LICENSE file in the root directory of this software component.
#
# Packages resources into tar.xz archives for upload to R2.
# Generates sha256 checksums and a manifest.json with both
# stable and development channels.
#
# Usage: ./scripts/package-resources.sh
#
# Requires: git, tar, xz, python3, pip (for stubs), gh (for firmware)

set -euo pipefail

# --- Configuration -----------------------------------------------------------

SDK_VERSION="1.4.0"
SDK_BASE_URL="https://download.openmv.io/sdk"
STUDIO_BASE_URL="https://download.openmv.io/studio"
BOARDS_REPO="https://github.com/openmv/openmv-boards.git"
OPENMV_REPO="https://github.com/openmv/openmv.git"
OPENMV_DOC_REPO="https://github.com/openmv/openmv-doc.git"
FIRMWARE_GH_REPO="openmv/openmv"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT_DIR="${PROJECT_DIR}/dist/resources"

SDK_PLATFORMS=("linux-x86_64" "darwin-arm64" "windows-x86_64")

# Stable firmware tag/version (resolved once).
STABLE_FW_TAG=""
STABLE_FW_VERSION=""

# Development version (from git describe on openmv repo).
DEV_FW_VERSION=""

# --- Helpers -----------------------------------------------------------------

TMPDIR=""
trap 'rm -rf "$TMPDIR"' EXIT

sha256() {
    if command -v sha256sum &>/dev/null; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

filesize() {
    wc -c < "$1" | tr -d ' '
}

# Create a tar.xz archive from a directory.
make_archive() {
    local name="$1"
    local src_dir="$2"
    local archive="${OUT_DIR}/${name}.tar.xz"

    echo ""
    echo "Creating ${name}.tar.xz..."
    tar cJf "$archive" -C "$(dirname "$src_dir")" "$(basename "$src_dir")"
    echo "sha256: $(sha256 "$archive")"
    echo "size:   $(filesize "$archive")"
}

# Get dev version from git describe on a shallow clone.
# Fetches tags and enough history for describe to count commits.
# Excludes non-release tags like "development".
# v4.8.1-479-gd726d833 -> v4.8.1-479
git_describe_version() {
    local dir="$1"
    git -C "$dir" fetch --tags --quiet
    git -C "$dir" fetch --deepen=1000 --quiet
    git -C "$dir" describe --tags --exclude=development | sed 's/-g[0-9a-f]*$//'
}

# --- Resolve tags/versions ---------------------------------------------------

# Find latest stable firmware release tag.
resolve_stable_fw_tag() {
    if [ -n "$STABLE_FW_TAG" ]; then
        return
    fi

    echo "Resolving latest stable firmware release..."
    STABLE_FW_TAG=$(gh release list --repo "$FIRMWARE_GH_REPO" --limit 20 \
        --json tagName \
        --jq '[.[] | select(.tagName | startswith("v"))][0].tagName')

    if [ -z "$STABLE_FW_TAG" ] || [ "$STABLE_FW_TAG" = "null" ]; then
        echo "ERROR: No stable release found in $FIRMWARE_GH_REPO" >&2
        exit 1
    fi

    STABLE_FW_VERSION="${STABLE_FW_TAG}"
    echo "Stable firmware: ${STABLE_FW_TAG}"
}

# Clone openmv repo (shallow - git_describe_version deepens as needed).
clone_openmv() {
    if [ -d "$TMPDIR/openmv" ]; then
        return
    fi
    echo "Cloning openmv..."
    git clone --depth 1 --quiet "$OPENMV_REPO" "$TMPDIR/openmv"
}

# Clone openmv-doc repo (shallow - git_describe_version deepens as needed).
clone_openmv_doc() {
    if [ -d "$TMPDIR/openmv-doc" ]; then
        return
    fi
    echo "Cloning openmv-doc..."
    git clone --depth 1 --quiet "$OPENMV_DOC_REPO" "$TMPDIR/openmv-doc"
}

# --- Package functions -------------------------------------------------------

package_boards() {
    echo "=== Packaging boards ==="
    local src="$TMPDIR/boards"

    git clone --depth 1 --quiet "$BOARDS_REPO" "$src"
    git -C "$src" fetch --tags --quiet

    # Stable: checkout latest tag
    local stable_version
    stable_version=$(git -C "$src" tag --sort=-v:refname | head -1) || true
    if [ -z "$stable_version" ]; then
        echo "ERROR: No tags found in $BOARDS_REPO" >&2
        exit 1
    fi
    echo "Boards stable: ${stable_version}"

    # Package HEAD as dev first (before checking out the tag)
    local dev_version
    dev_version=$(git_describe_version "$src")
    echo "Boards development: ${dev_version}"
    local dev_name="boards-${dev_version}"
    cp -r "$src" "$TMPDIR/${dev_name}"
    rm -rf "$TMPDIR/${dev_name}/.git" "$TMPDIR/${dev_name}/.gitignore"
    make_archive "$dev_name" "$TMPDIR/${dev_name}"

    # Now checkout stable tag and package
    git -C "$src" fetch --depth 1 origin tag "$stable_version" --quiet
    git -C "$src" checkout "$stable_version" --quiet
    rm -rf "$src/.git" "$src/.gitignore"
    local stable_name="boards-${stable_version}"
    mv "$src" "$TMPDIR/${stable_name}"
    make_archive "$stable_name" "$TMPDIR/${stable_name}"
}

package_examples() {
    echo ""
    echo "=== Packaging examples ==="
    clone_openmv
    resolve_stable_fw_tag

    # Development: package from HEAD first (git_describe_version deepens as needed)
    DEV_FW_VERSION=$(git_describe_version "$TMPDIR/openmv")
    echo "Development firmware version: ${DEV_FW_VERSION}"
    local dev_name="examples-${DEV_FW_VERSION}"
    cp -r "$TMPDIR/openmv/scripts/examples" "$TMPDIR/${dev_name}"
    make_archive "$dev_name" "$TMPDIR/${dev_name}"
    rm -rf "$TMPDIR/${dev_name}"

    # Stable: fetch and checkout stable tag
    git -C "$TMPDIR/openmv" fetch --depth 1 origin tag "$STABLE_FW_TAG" --quiet
    git -C "$TMPDIR/openmv" checkout "$STABLE_FW_TAG" --quiet
    local stable_name="examples-${STABLE_FW_VERSION}"
    cp -r "$TMPDIR/openmv/scripts/examples" "$TMPDIR/${stable_name}"
    make_archive "$stable_name" "$TMPDIR/${stable_name}"
    rm -rf "$TMPDIR/${stable_name}"
}

package_stubs() {
    echo ""
    echo "=== Packaging stubs ==="
    clone_openmv_doc
    resolve_stable_fw_tag

    pip install --quiet sphinx &>/dev/null

    # Development: package from HEAD first (git_describe_version deepens as needed)
    local dev_version
    dev_version=$(git_describe_version "$TMPDIR/openmv-doc")
    echo "Stubs development: ${dev_version}"
    local dev_name="stubs-${dev_version}"
    mkdir -p "$TMPDIR/${dev_name}"
    python3 "$TMPDIR/openmv-doc/genpyi.py" \
        --docs-dir "$TMPDIR/openmv-doc/docs/_sources/library/" \
        --pyi-dir "$TMPDIR/${dev_name}"
    make_archive "$dev_name" "$TMPDIR/${dev_name}"
    rm -rf "$TMPDIR/${dev_name}"

    # Stable: fetch and checkout stable tag
    git -C "$TMPDIR/openmv-doc" fetch --depth 1 origin tag "$STABLE_FW_TAG" --quiet
    git -C "$TMPDIR/openmv-doc" checkout "$STABLE_FW_TAG" --quiet
    local stable_name="stubs-${STABLE_FW_VERSION}"
    mkdir -p "$TMPDIR/${stable_name}"
    python3 "$TMPDIR/openmv-doc/genpyi.py" \
        --docs-dir "$TMPDIR/openmv-doc/docs/_sources/library/" \
        --pyi-dir "$TMPDIR/${stable_name}"
    make_archive "$stable_name" "$TMPDIR/${stable_name}"
    rm -rf "$TMPDIR/${stable_name}"
}

package_firmware() {
    echo ""
    echo "=== Packaging firmware ==="
    resolve_stable_fw_tag

    # Stable firmware
    local stable_name="firmware-${STABLE_FW_VERSION}"
    mkdir -p "$TMPDIR/${stable_name}"

    gh release download "$STABLE_FW_TAG" \
        --repo "$FIRMWARE_GH_REPO" \
        --dir "$TMPDIR/${stable_name}" \
        --pattern "*.zip" &>/dev/null || true

    for zip in "$TMPDIR/${stable_name}"/*.zip; do
        [ -f "$zip" ] || continue
        unzip -q -o "$zip" -d "$TMPDIR/${stable_name}"
        rm "$zip"
    done

    local count
    count=$(find "$TMPDIR/${stable_name}" -type f | wc -l | tr -d ' ')
    if [ "$count" -eq 0 ]; then
        echo "ERROR: No firmware assets found in release $STABLE_FW_TAG" >&2
        return 1
    fi
    echo "Stable: ${count} firmware files"
    make_archive "$stable_name" "$TMPDIR/${stable_name}"

    # Development firmware
    # DEV_FW_VERSION was set by package_examples (from git describe on openmv)
    local dev_name="firmware-${DEV_FW_VERSION}"
    mkdir -p "$TMPDIR/${dev_name}"

    gh release download "development" \
        --repo "$FIRMWARE_GH_REPO" \
        --dir "$TMPDIR/${dev_name}" \
        --pattern "*.zip" &>/dev/null || true

    for zip in "$TMPDIR/${dev_name}"/*.zip; do
        [ -f "$zip" ] || continue
        unzip -q -o "$zip" -d "$TMPDIR/${dev_name}"
        rm "$zip"
    done

    count=$(find "$TMPDIR/${dev_name}" -type f | wc -l | tr -d ' ')
    if [ "$count" -eq 0 ]; then
        echo "WARNING: No dev firmware assets found" >&2
    else
        echo "Development: ${count} firmware files"
    fi
    make_archive "$dev_name" "$TMPDIR/${dev_name}"
}

# --- Manifest generation -----------------------------------------------------

# Emit a JSON entry for a channeled resource (boards, examples, firmware, stubs).
# Looks for archives matching {name}-*.tar.xz in OUT_DIR.
emit_resource_entry() {
    local name="$1"

    # Find stable and dev archives
    local stable_archive=""
    local dev_archive=""

    for f in "${OUT_DIR}"/${name}-*.tar.xz; do
        [ -f "$f" ] || continue
        local base
        base=$(basename "$f")
        local ver="${base%.tar.xz}"
        ver="${ver#${name}-}"
        # Version with a dash after stripping leading v is development
        local stripped="${ver#v}"
        if [[ "$stripped" == *-* ]]; then
            dev_archive="$f"
        else
            stable_archive="$f"
        fi
    done

    local first=true

    printf '  "%s": {\n' "$name"

    if [ -n "$stable_archive" ]; then
        local sbase sver
        sbase=$(basename "$stable_archive")
        sver="${sbase%.tar.xz}"
        sver="${sver#${name}-}"
        printf '    "stable": {\n'
        printf '      "version": "%s",\n' "$sver"
        printf '      "url": "%s/%s",\n' "$STUDIO_BASE_URL" "$sbase"
        printf '      "sha256": "%s",\n' "$(sha256 "$stable_archive")"
        printf '      "size": %s\n' "$(filesize "$stable_archive")"
        printf '    }'
        first=false
    fi

    if [ -n "$dev_archive" ]; then
        if [ "$first" = false ]; then
            printf ',\n'
        fi
        local dbase dver
        dbase=$(basename "$dev_archive")
        dver="${dbase%.tar.xz}"
        dver="${dver#${name}-}"
        printf '    "development": {\n'
        printf '      "version": "%s",\n' "$dver"
        printf '      "url": "%s/%s",\n' "$STUDIO_BASE_URL" "$dbase"
        printf '      "sha256": "%s",\n' "$(sha256 "$dev_archive")"
        printf '      "size": %s\n' "$(filesize "$dev_archive")"
        printf '    }'
    fi

    printf '\n  }'
}

generate_manifest() {
    echo ""
    echo "=== Generating manifest.json ==="

    local manifest="${OUT_DIR}/manifest.json"

    {
        printf '{\n  "schema_version": 1,\n'

        emit_resource_entry "boards"
        printf ',\n'

        emit_resource_entry "examples"
        printf ',\n'

        emit_resource_entry "firmware"
        printf ',\n'

        emit_resource_entry "stubs"
        printf ',\n'

        # Tools (no channel split)
        printf '  "tools": {\n'
        printf '    "version": "%s",\n' "$SDK_VERSION"
        printf '    "platforms": {\n'

        local first=true
        for platform in "${SDK_PLATFORMS[@]}"; do
            local sdk_name="openmv-sdk-${SDK_VERSION}-${platform}"
            local sdk_url="${SDK_BASE_URL}/${sdk_name}.tar.xz"
            local sdk_sha256_url="${sdk_url}.sha256"

            echo "  Fetching checksum for ${sdk_name}..." >&2
            local sdk_sha256 sdk_size
            sdk_sha256=$(curl -fsSL "$sdk_sha256_url" | awk '{print $1}') || {
                echo "WARNING: Could not fetch checksum for ${sdk_name}" >&2
                continue
            }

            sdk_size=$(curl -fsSLI "$sdk_url" | grep -i content-length | tail -1 | awk '{print $2}' | tr -d '\r')
            if [ -z "$sdk_size" ]; then
                sdk_size=0
            fi

            if [ "$first" = true ]; then
                first=false
            else
                printf ',\n'
            fi

            printf '      "%s": {\n        "url": "%s",\n        "sha256": "%s",\n        "size": %s\n      }' \
                "$platform" "$sdk_url" "$sdk_sha256" "$sdk_size"
        done

        printf '\n    }\n  }\n}\n'
    } > "$manifest"

    echo "Manifest written to ${manifest}"
}

# --- Main --------------------------------------------------------------------

main() {
    TMPDIR=$(mktemp -d)
    mkdir -p "$OUT_DIR"

    resolve_stable_fw_tag
    package_boards
    package_examples
    package_stubs
    package_firmware
    generate_manifest

    echo ""
    echo "=== Done ==="
    echo "Archives in: ${OUT_DIR}"
    ls -lh "${OUT_DIR}"
}

main
