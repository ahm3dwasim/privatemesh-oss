#!/bin/sh
# Fetch openly licensed font assets for local serving; never used at app runtime.
set -eu
cd "$(dirname "$0")/.."
mkdir -p public/fonts
curl --fail --location --retry 2 'https://raw.githubusercontent.com/google/fonts/main/ofl/sourcesans3/SourceSans3%5Bwght%5D.ttf' -o public/fonts/source-sans-3-latin.ttf
curl --fail --location --retry 2 'https://raw.githubusercontent.com/google/fonts/main/ofl/sourcesans3/OFL.txt' -o public/fonts/SourceSans3-OFL.txt
