#!/usr/bin/env bash
# PŘECHODNÉ (starý samostatný repozitář repo-doctoru; nové verze jdou do Icenux, viz release.yml).
# Z balíčků vydání (dist/release, viz packaging/package.sh) vyrobí podepsaný APT a RPM
# repozitář pro GitHub Pages do dist/site/:
#   apt/  – plochý repozitář (Packages, Release, InRelease, Release.gpg)
#   rpm/  – repodata/ z createrepo_c + podepsaný repomd.xml.asc
#   repo-doctor.asc – veřejný klíč vydání
#
# Volání: GNUPGHOME=<klíčenka s klíčem vydání> packaging/repo.sh [VERZE]
# Potřebuje apt-ftparchive (apt-utils), createrepo_c a gpg.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=${1:-$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)}
fpr=$(sed -n 's/^FINGERPRINT = "\(.*\)"/\1/p' src/repo_doctor/update.py)
rel=dist/release
site=dist/site
sign=(gpg --batch --yes --local-user "$fpr")
rm -rf "$site" && mkdir -p "$site/apt" "$site/rpm"
cp src/repo_doctor/data/signing-key.asc "$site/repo-doctor.asc"

# APT
cp "$rel/repo-doctor_amd64.deb" "$site/apt/repo-doctor_${VERSION}_amd64.deb"
(
    cd "$site/apt"
    apt-ftparchive packages . > Packages
    gzip -9kn Packages
    apt-ftparchive \
        -o APT::FTPArchive::Release::Origin=repo-doctor \
        -o APT::FTPArchive::Release::Label=repo-doctor \
        -o APT::FTPArchive::Release::Architectures=amd64 \
        release . > Release
    "${sign[@]}" --clearsign --output InRelease Release
    "${sign[@]}" --armor --detach-sign --output Release.gpg Release
)

# RPM
cp "$rel/repo-doctor.x86_64.rpm" "$site/rpm/repo-doctor-${VERSION}-1.x86_64.rpm"
createrepo_c --quiet "$site/rpm"
"${sign[@]}" --armor --detach-sign --output "$site/rpm/repodata/repomd.xml.asc" \
    "$site/rpm/repodata/repomd.xml"

cat > "$site/index.html" <<HTML
<!doctype html><meta charset="utf-8"><title>repo-doctor – balíčkové repozitáře</title>
<p>APT a RPM repozitář <a href="https://github.com/Luquas95/repo-doctor">repo-doctoru</a>
(verze ${VERSION}). Nainstaluj balíček z
<a href="https://github.com/Luquas95/repo-doctor/releases/latest">posledního vydání</a> –
repozitář se nastaví sám a další verze přijdou s aktualizací systému.</p>
HTML
touch "$site/.nojekyll"
find "$site" -type f | sort
