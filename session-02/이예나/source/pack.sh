#!/bin/bash
# S3에 올릴 web.tar.gz, app.tar.gz를 dist/에 만든다.
set -euo pipefail
cd "$(dirname "$0")"

rm -rf dist build
mkdir -p dist build
cp -R app build/app
rm -rf build/app/node_modules
(cd build/app && npm ci --omit=dev --no-audit --no-fund)

export COPYFILE_DISABLE=1
tar --no-xattrs -czf dist/web.tar.gz web
tar --no-xattrs -czf dist/app.tar.gz -C build app
rm -rf build
ls -lh dist
