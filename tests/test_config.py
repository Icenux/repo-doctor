from __future__ import annotations

from pathlib import Path

import pytest

from repo_doctor.config import (
    AllowEntry,
    Config,
    ConfigError,
    ConfigStore,
    ForgeConfig,
    LimitsConfig,
    RootConfig,
    effective_for_repo,
    load_repo_overrides,
    parse_config,
)
from repo_doctor.models import Severity

SAMPLE = """\
# můj komentář
[[roots]]
path = "~/projekty"  # inline komentář
depth = 3
exclude = ["**/archiv/**"]

[[forges]]
name = "github"
type = "github"
user = "nekdo"
token_source = "keyring"

[[forges]]
name = "domaci-forgejo"
type = "forgejo"
url = "https://git.example.ts.net:3000/"
token_cmd = "pass show git/forgejo"
ca_bundle = "~/certs/ca.pem"

[keys]
scan_all = "R"

[checks]
fail_on = "medium"
"""


def test_load_sample(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(SAMPLE)
    cfg = ConfigStore(f).load()
    assert cfg.roots[0].expanded == Path.home() / "projekty"
    assert cfg.forges[1].url == "https://git.example.ts.net:3000"
    assert cfg.forges[1].token_source == "cmd"
    assert cfg.forges[0].base_url == "https://api.github.com"
    assert cfg.checks.fail_on is Severity.MEDIUM
    assert cfg.forge("github") is not None
    assert cfg.forge("nope") is None


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "none.toml")
    assert not store.exists
    assert store.load() == Config()


@pytest.mark.parametrize(
    ("snippet", "needle"),
    [
        ('[[forges]]\nname="x"\ntype="github"\ntoken="abc"\n', "prostý text"),
        ('[[forges]]\nname="x"\ntype="gitea"\n', "vyžaduje url"),
        ('[[forges]]\nname="x"\ntype="github"\ntoken_source="env"\n', "token_env"),
        ('[[forges]]\nname="x"\ntype="github"\ntoken_source="cmd"\n', "token_cmd"),
        ('[[forges]]\nname="x"\ntype="github"\nurl="ftp://x"\n', "http"),
        ('[[forges]]\nname="x"\ntype="github"\nurl="https://u:p@x"\n', "přihlašovací"),
        (
            '[[forges]]\nname="x"\ntype="github"\n[[forges]]\nname="x"\ntype="gitlab"\n',
            "duplicitní",
        ),
        ('[[roots]]\npath=" "\n', "prázdná"),
        ('[[roots]]\npath="/x"\ndepth=99\n', "depth"),
        ("unknown_key = 1\n", "unknown_key"),
    ],
)
def test_invalid(tmp_path: Path, snippet: str, needle: str) -> None:
    f = tmp_path / "c.toml"
    f.write_text(snippet)
    with pytest.raises(ConfigError) as exc:
        ConfigStore(f).load()
    assert needle in str(exc.value)


def test_broken_toml(tmp_path: Path) -> None:
    f = tmp_path / "c.toml"
    f.write_text("[[roots]\n")
    with pytest.raises(ConfigError):
        ConfigStore(f).load()


def test_roundtrip_preserves_comments(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(SAMPLE)
    store = ConfigStore(f)
    store.load()
    store.upsert_root(RootConfig(path="/mnt/data/git", depth=2))
    store.upsert_root(RootConfig(path="~/projekty", depth=4, enabled=False), index=0)
    store.set_value("limits", "stash_days", 10)
    store.set_value("ui", "language", "en")
    store.set_value("ui", "editor", None)
    store.set_top("license", "ISC")
    store.set_top("templates_dir", None)
    text = f.read_text()
    assert "# můj komentář" in text
    cfg = ConfigStore(f).load()
    assert [r.path for r in cfg.roots] == ["~/projekty", "/mnt/data/git"]
    assert cfg.roots[0].depth == 4 and not cfg.roots[0].enabled
    assert cfg.limits.stash_days == 10
    assert cfg.license == "ISC"
    store.remove_root(1)
    store.remove_root(0)
    assert ConfigStore(f).load().roots == []


def test_invalid_change_not_written(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text(SAMPLE)
    store = ConfigStore(f)
    store.load()
    with pytest.raises(ConfigError):
        store.set_value("limits", "stash_days", -5)
    assert "stash_days" not in f.read_text()
    assert "stash_days" not in store.doc.as_string()


def test_forges_and_allowlist(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    store = ConfigStore(f)
    store.load()
    store.upsert_forge(ForgeConfig(name="gh", type="github", token_env="GH_TOKEN"))
    store.upsert_forge(ForgeConfig(name="gl", type="gitlab", token_source="keyring"))
    store.upsert_forge(ForgeConfig(name="gh", type="github", token_cmd="pass x"), index=0)
    cfg = ConfigStore(f).load()
    assert cfg.forges[0].token_source == "cmd" and cfg.forges[0].token_env is None
    store.remove_forge(1)
    store.remove_forge(0)
    assert ConfigStore(f).load().forges == []
    store.add_allow(AllowEntry(hash="0123456789abcdef", reason="test fixture"))
    assert ConfigStore(f).load().allowlist[0].reason == "test fixture"
    store.remove_allow("0123456789abcdef")
    assert ConfigStore(f).load().allowlist == []
    assert "token" not in f.read_text().replace("token_", "")


def test_repo_overrides(tmp_path: Path) -> None:
    cfg = parse_config({"checks": {"disabled": ["ci-missing"]}})
    assert load_repo_overrides(tmp_path) is None
    (tmp_path / ".repo-doctor.toml").write_text(
        '[checks]\ndisabled=["readme-missing"]\n[limits]\nstash_days=3\n'
        '[[allowlist]]\nhash="0123456789abcdef"\nreason="ok"\n'
        'ignore_paths=["x/**"]\nlicense="ISC"\n'.replace(
            'ignore_paths=["x/**"]\nlicense="ISC"\n', ""
        )
    )
    ov = load_repo_overrides(tmp_path)
    eff = effective_for_repo(cfg, ov)
    assert eff.checks.disabled == ["readme-missing"]
    assert eff.limits.stash_days == 3
    assert len(eff.allowlist) == 1
    assert effective_for_repo(cfg, None) is cfg
    (tmp_path / ".repo-doctor.toml").write_text('ignore_paths=["x/**"]\nlicense="ISC"\n')
    eff = effective_for_repo(cfg, load_repo_overrides(tmp_path))
    assert eff.ignore_paths == ["x/**"] and eff.license == "ISC"
    (tmp_path / ".repo-doctor.toml").write_text("bogus = 1\n")
    with pytest.raises(ConfigError):
        load_repo_overrides(tmp_path)


def test_limits_defaults() -> None:
    assert LimitsConfig().history_size == 30


def test_removed_ui_keys_are_ignored(tmp_path: Path) -> None:
    """Klíče zrušené v #89 (jediné téma, bez přepínače panelu) staré configy nerozbijí."""
    f = tmp_path / "config.toml"
    f.write_text('[ui]\ntheme = "light"\nshow_sidebar = false\nlanguage = "cs"\n')
    cfg = ConfigStore(f).load()
    assert cfg.ui.language == "cs"
    assert not hasattr(cfg.ui, "theme") and not hasattr(cfg.ui, "show_sidebar")
