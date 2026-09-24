#!/bin/bash
# Build the it87 module for the running TrueNAS kernel inside a Debian container.
# Run on the NAS as root after every TrueNAS update.
set -euo pipefail

IT87_REPO=${IT87_REPO:-https://github.com/frankcrawford/it87.git}
IT87_COMMIT=${IT87_COMMIT:-bc06d3488439e5fcd725c1bdcfcac994d6d95cac}

repo=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
release=$(uname -r)
headers=$(readlink -f "/lib/modules/$release/build")
gcc_major=$(sed -n 's/.*gcc (Debian \([0-9]\+\)\..*/\1/p' /proc/version)

case $gcc_major in
    12) image=debian:bookworm ;;
    14) image=debian:trixie ;;
    *) echo "Unsupported kernel compiler 'gcc $gcc_major' in /proc/version" >&2; exit 1 ;;
esac

[[ -d $headers ]] || { echo "Kernel headers not found at $headers" >&2; exit 1; }
[[ $headers == /usr/src/* ]] || { echo "Expected headers under /usr/src, got $headers" >&2; exit 1; }

out="$repo/modules/$release"
mkdir -p "$out"
echo "Building it87 $IT87_COMMIT for $release with gcc-$gcc_major in $image"

docker run --rm \
    -v /usr/src:/usr/src:ro \
    -v "$out":/out \
    -e GCC="$gcc_major" -e HEADERS="$headers" -e RELEASE="$release" \
    -e IT87_REPO="$IT87_REPO" -e IT87_COMMIT="$IT87_COMMIT" \
    "$image" bash -euo pipefail -c '
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends \
            "gcc-$GCC" make git ca-certificates libelf-dev bc kmod >/dev/null
        ln -sf "/usr/bin/gcc-$GCC" /usr/bin/gcc
        git clone -q "$IT87_REPO" /src
        git -C /src checkout -q "$IT87_COMMIT"
        make -C "$HEADERS" M=/src CC="gcc-$GCC" modules
        vermagic=$(modinfo -F vermagic /src/it87.ko)
        [[ $vermagic == "$RELEASE "* ]] || { echo "vermagic mismatch: $vermagic" >&2; exit 1; }
        cp /src/it87.ko /out/
    '

echo "Built $out/it87.ko"
