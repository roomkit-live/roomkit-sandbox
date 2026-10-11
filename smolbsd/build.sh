#!/bin/sh
#
# Build the roomkit-sandbox smolBSD image into <smolBSD>/images/.
#
# usage: smolbsd/build.sh /path/to/smolBSD [VAR=val ...]
#
# Extra arguments go to smoler.sh build, e.g. NOSUDO=1 to build without
# root once the builder image has been fetched with `bmake fetchimg`.

set -eu

here=$(cd "$(dirname "$0")" && pwd)
smolbsd=$(cd "${1:?usage: $0 /path/to/smolBSD [VAR=val ...]}" && pwd)
shift

etc="$smolbsd/service/roomkit-sandbox/etc"
mkdir -p "$etc"
cp "$here/runner.sh" "$etc/roomkit-runner"
cp "$here/workspace.sh" "$etc/roomkit-workspace"
cp "$here/rc.pre" "$etc/rc.pre"
cp "$here/rc.local" "$etc/rc.local"

cd "$smolbsd"
./smoler.sh build -y "$@" "$here/SMOLerfile"
