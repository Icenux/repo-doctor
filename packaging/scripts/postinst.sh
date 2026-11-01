#!/bin/sh
# Po instalaci i aktualizaci balíčku (deb postinst, rpm %post): nastaví zdroj balíčků Icenux.
# Soubory zdroje nepatří balíčku (jinak by se u dalších aplikací Icenux v dpkg tloukly o
# jednu cestu): zapíše je tento skript, a to jen když chybí. Klíč se obnoví vždy.
# Zároveň odstraní starý samostatný repozitář repo-doctoru (luquas95.github.io/repo-doctor),
# který balíček dřív přidával; jeho klíč zmizí s aktualizací, takže by apt hlásil chybu.
set -e
lib=/usr/lib/repo-doctor

if [ -d /etc/apt/sources.list.d ] && [ -f "$lib/icenux.asc" ]; then
    install -d /usr/share/keyrings
    install -m 644 "$lib/icenux.asc" /usr/share/keyrings/icenux.asc
    [ -e /etc/apt/sources.list.d/icenux.list ] ||
        install -m 644 "$lib/icenux.list" /etc/apt/sources.list.d/icenux.list
    old=/etc/apt/sources.list.d/repo-doctor.list
    if [ -f "$old" ] && grep -q 'luquas95\.github\.io/repo-doctor/' "$old"; then
        rm -f "$old"
    fi
fi

if [ -d /etc/yum.repos.d ] && [ -f "$lib/icenux-rpm.asc" ]; then
    install -d /etc/pki/rpm-gpg
    install -m 644 "$lib/icenux-rpm.asc" /etc/pki/rpm-gpg/RPM-GPG-KEY-icenux
    [ -e /etc/yum.repos.d/icenux.repo ] ||
        install -m 644 "$lib/icenux.repo" /etc/yum.repos.d/icenux.repo
    old=/etc/yum.repos.d/repo-doctor.repo
    if [ -f "$old" ] && grep -q 'luquas95\.github\.io/repo-doctor/' "$old"; then
        rm -f "$old"
    fi
fi
exit 0
