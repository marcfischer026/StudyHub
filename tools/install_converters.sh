#!/bin/zsh
# Installs the local PDF converters UniVerse uses for "Convert on this Mac".
# Marker and MinerU need PyTorch on Python 3.12 and pin different Pillow versions,
# so each gets an environment of its own under the data root. About 2 GB for the
# environments; the models (about 6 GB) download on the first conversion.
set -euo pipefail

root="${UNIVERSE_DATA_DIR:-$HOME/.universe_data}/converters"
mkdir -p "$root"
command -v uv >/dev/null || brew install uv

uv venv -q --allow-existing --python 3.12 "$root/marker"
VIRTUAL_ENV="$root/marker" uv pip install -q "marker-pdf>=2.1,<3"

# MinerU 2: a plain offline converter. MinerU 4 is a document-library server instead.
uv venv -q --allow-existing --python 3.12 "$root/mineru"
VIRTUAL_ENV="$root/mineru" uv pip install -q "mineru[core]>=2.5,<3"

echo "Installed in $root"
