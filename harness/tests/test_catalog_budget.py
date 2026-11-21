import json

import harness_sync


def make_source(root, name, description):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / 'SKILL.md').write_text(
        '---\nname: ' + name + '\ndescription: ' + json.dumps(description) + '\n---\n\nFull original instructions.\n'
    )
    return directory


def test_catalog_budget_covers_every_enabled_skill_without_changing_sources(tmp_path):
    want = {f'skill-{i}': make_source(tmp_path, f'skill-{i}', 'Длинное описание с контекстом. ' * 100)
            for i in range(339)}
    originals = {name: (path / 'SKILL.md').read_bytes() for name, path in want.items()}
    descriptions = harness_sync.catalog_descriptions(want, {'skill-0': 'Manually curated summary'})
    assert set(descriptions) == set(want)
    assert sum(len(value.encode('utf-8')) for value in descriptions.values()) <= 32000
    assert descriptions['skill-0'] == 'Manually curated summary'
    assert all(value.strip() and '\n' not in value for value in descriptions.values())
    assert all((want[name] / 'SKILL.md').read_bytes() == value for name, value in originals.items())


def test_new_skill_and_invalid_frontmatter_still_get_descriptions(tmp_path):
    valid = make_source(tmp_path, 'new-personal-skill', 'Я' * 500)
    invalid = tmp_path / 'legacy-skill'
    invalid.mkdir()
    (invalid / 'SKILL.md').write_text('---\nname: legacy-skill\ndescription: Invalid: YAML\n---\nBody')
    want = {'new-personal-skill': valid, 'legacy-skill': invalid}
    descriptions = harness_sync.catalog_descriptions(want, {'stale-disabled-skill': 'Skip'})
    assert set(descriptions) == set(want)
    assert all(value and len(value.encode()) <= 300 for value in descriptions.values())
    assert harness_sync.catalog_descriptions({}, {}) == {}


def test_default_sync_uses_budgeted_cards_and_preserves_full_instructions(tmp_path):
    src, gem = tmp_path / 'skills', tmp_path / 'gemini'
    active = make_source(src, 'active', 'Описание. ' * 100)
    make_source(src, 'off', 'Disabled')
    cfg = tmp_path / 'config.toml'
    cfg.write_text('[[skills.config]]\npath = "' + str(src / 'off/SKILL.md') + '"\nenabled = false\n')
    base = ['--codex-config', str(cfg), '--skills-src', str(src), '--gemini-config', str(gem),
            '--compact', str(tmp_path / 'none.json'), '--main', str(tmp_path / 'none-main.json'),
            '--codex-hooks', str(tmp_path / 'none-hooks.json'), '--claude-skills', str(tmp_path / 'claude')]
    assert harness_sync.main(base + ['--check']) == 1
    assert not gem.exists()
    assert harness_sync.main(base) == 0
    card = gem / 'skills/active'
    assert not card.is_symlink()
    assert str(active / 'SKILL.md') in (card / 'SKILL.md').read_text()
    assert 'Прочитай её целиком' in (card / 'SKILL.md').read_text()
    assert not (gem / 'skills/off').exists()
    assert harness_sync.main(base + ['--check']) == 0


def test_budgeted_catalog_never_replaces_foreign_skill_directory(tmp_path):
    src, gem = tmp_path / 'source', tmp_path / 'target'
    make_source(src, 'mine', 'A long description ' * 100)
    foreign = make_source(gem, 'mine', 'User-owned content')
    original = (foreign / 'SKILL.md').read_bytes()
    compact = harness_sync.catalog_descriptions({'mine': src / 'mine'}, {})
    changes = harness_sync.sync_skills(src, gem, {}, True, compact)
    assert changes == ['skill =mine (kept: not managed by harness_sync)']
    assert (foreign / 'SKILL.md').read_bytes() == original
