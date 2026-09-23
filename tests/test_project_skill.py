from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


# --- Generic contract for every skill under .agents/skills/ ---
# Keeps the whole skill library on one official format (minimal frontmatter,
# references one level deep, no pre-approved shell/tools) so added methodology
# skills cannot drift into a second, cargo-culted layout.

SKILLS_DIR = ROOT / ".agents" / "skills"
_SKILL_NAME_RE = re.compile(r"[a-z0-9-]{1,63}")


def _iter_skill_dirs() -> list[Path]:
    return sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir())


def _frontmatter_fields(text: str) -> dict[str, str]:
    match = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def test_every_skill_has_minimal_valid_frontmatter() -> None:
    skill_dirs = _iter_skill_dirs()
    assert skill_dirs, "expected at least one skill directory"
    for skill_dir in skill_dirs:
        skill_md = skill_dir / "SKILL.md"
        assert skill_md.is_file(), f"{skill_dir.name} is missing SKILL.md"
        fields = _frontmatter_fields(skill_md.read_text(encoding="utf-8"))
        assert set(fields) == {"name", "description"}, skill_dir.name
        assert _SKILL_NAME_RE.fullmatch(fields["name"]), skill_dir.name
        assert fields["name"] == skill_dir.name, skill_dir.name
        assert len(fields["description"]) >= 30, skill_dir.name


def test_every_skill_references_exist_and_stay_one_level_deep() -> None:
    for skill_dir in _iter_skill_dirs():
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        for relative in re.findall(r"\]\((references/[^)]+)\)", text):
            assert (skill_dir / relative).is_file(), f"{skill_dir.name}: {relative}"
            assert len(Path(relative).parts) == 2, f"{skill_dir.name}: {relative}"


def test_no_skill_preapproves_shell_or_lists_dependencies() -> None:
    for skill_dir in _iter_skill_dirs():
        skill = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert "allowed-tools" not in skill, skill_dir.name
        meta_path = skill_dir / "agents" / "openai.yaml"
        if meta_path.is_file():
            meta = meta_path.read_text(encoding="utf-8")
            assert "dependencies:" not in meta, skill_dir.name
            assert "shell" not in meta.lower(), skill_dir.name
            assert f"${skill_dir.name}" in meta, skill_dir.name
