#!/usr/bin/env bash
# Ze sestavené aplikace (dist/repo-doctor, viz packaging/build.sh) vyrobí soubory vydání
# do dist/release/:
#   repo-doctor_amd64.deb, repo-doctor.x86_64.rpm, repo-doctor-VERZE-1-x86_64.pkg.tar.zst (Arch),
#   repo-doctor-linux-x86_64.tar.gz,
#   PKGBUILD (AUR repo-doctor-bin) a SHA256SUMS.
#
# Volání: packaging/package.sh [VERZE]     (výchozí verze z pyproject.toml; potřebuje nfpm v PATH)
# RPM se podepíše, když NFPM_RPM_KEY_FILE ukazuje na soukromý klíč; SHA256SUMS podepisuje
# až packaging/sign.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=${1:-$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)}
export VERSION NFPM_RPM_KEY_FILE=${NFPM_RPM_KEY_FILE:-}
app=dist/repo-doctor
out=dist/release
[ -x "$app/repo-doctor" ] || { echo "Chybí $app – nejdřív spusť packaging/build.sh" >&2; exit 1; }
built=$("$app/repo-doctor" --version | cut -d' ' -f2)
[ "$built" = "$VERSION" ] || { echo "Sestavená verze $built ≠ $VERSION" >&2; exit 1; }
rm -rf "$out" && mkdir -p "$out"

# .deb a .rpm (podpis RPM jen s klíčem)
config=packaging/nfpm.yaml
if [ -z "$NFPM_RPM_KEY_FILE" ]; then
    config=$(mktemp --suffix=.yaml)
    trap 'rm -f "$config"' EXIT
    sed '/^  signature:/,$d' packaging/nfpm.yaml > "$config"
fi
nfpm package --config "$config" --packager deb --target "$out/repo-doctor_amd64.deb"
nfpm package --config "$config" --packager rpm --target "$out/repo-doctor.x86_64.rpm"
nfpm package --config "$config" --packager archlinux --target "$out/"   # repo-doctor-VERZE-1-x86_64.pkg.tar.zst

# tarball: aplikace + značka instalace + instalační skript
stage=$(mktemp -d)
cp -a "$app" "$stage/repo-doctor"
echo tarball > "$stage/repo-doctor/install-kind"
echo "$VERSION" > "$stage/repo-doctor/VERSION"   # samoaktualizace podle ní odmítne starší vydání
cp packaging/files/install.sh LICENSE "$stage/repo-doctor/"
tar -C "$stage" --owner=0 --group=0 --numeric-owner -czf "$out/repo-doctor-linux-x86_64.tar.gz" repo-doctor
rm -rf "$stage"

# PKGBUILD pro AUR (repo-doctor-bin)
sha=$(sha256sum "$out/repo-doctor-linux-x86_64.tar.gz" | cut -d' ' -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SHA256@/$sha/" packaging/aur/PKGBUILD.in > "$out/PKGBUILD"

(cd "$out" && sha256sum repo-doctor_amd64.deb repo-doctor.x86_64.rpm repo-doctor-*-x86_64.pkg.tar.zst \
    repo-doctor-linux-x86_64.tar.gz PKGBUILD > SHA256SUMS)
ls -la "$out"
