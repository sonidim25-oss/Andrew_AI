"""Plan trusted skill documents for the controller installer, without importing code."""
import ast
import hashlib
import os
from pathlib import Path
import re


SKILL_NAMES = frozenset({'voice-communication', 'image-creation', 'research-workflow'})
MAX_SKILL_BYTES = 12_000


def document_hashes(module):
    tree = ast.parse(Path(module).read_bytes())
    matches = [node for node in tree.body if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == 'SKILL_DOCUMENT_HASHES' for target in node.targets)]
    if len(matches) != 1:
        raise ValueError('Skill loader must declare one exact document hash map')
    hashes = ast.literal_eval(matches[0].value)
    if not isinstance(hashes, dict) or set(hashes) != SKILL_NAMES or any(
            not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value) for value in hashes.values()):
        raise ValueError('Unexpected skill document manifest')
    return hashes


def check_paths(root, path):
    # Includes nonexistent destination directories; do not follow symlink roots.
    if any(item.is_symlink() for item in (root, path.parent, path)):
        raise ValueError('Symlink in trusted skill assets; installation refused')


def read_document(path):
    with path.open('rb') as source:
        data = source.read(MAX_SKILL_BYTES + 1)
    if len(data) > MAX_SKILL_BYTES:
        raise ValueError('Skill document exceeds the bounded loader limit')
    data.decode('utf-8')
    return data


def plan_skill_assets(source_root, target_root):
    source_root, target_root = Path(source_root), Path(target_root)
    source_hashes = document_hashes(source_root / 'prototype/andrewbot/skills.py')
    installed_module = target_root.parent / 'prototype/andrewbot/skills.py'
    if installed_module.is_symlink():
        raise ValueError('Symlink in installed skill loader')
    previous_hashes = document_hashes(installed_module) if installed_module.exists() else {}
    plan = []
    for name in sorted(SKILL_NAMES):
        source = source_root / 'skills' / name / 'SKILL.md'
        target = target_root / name / 'SKILL.md'
        check_paths(source_root / 'skills', source)
        check_paths(target_root, target)
        tested = read_document(source)
        if hashlib.sha256(tested).hexdigest() != source_hashes[name]:
            raise ValueError('Skill source changed since its loader was tested: ' + name)
        original = read_document(target) if target.exists() else None
        if original is not None and hashlib.sha256(original).hexdigest() not in {source_hashes[name], previous_hashes.get(name)}:
            raise ValueError('Installed skill has an unrecognized local edit; installation refused: ' + name)
        plan.append({'name': name, 'source': source, 'root': target_root, 'path': target,
                     'tested': tested, 'original': original, 'backup': 'skill-' + name + '.md'})
    return plan


def install_skill_assets(plan, gid, replace):
    for item in plan:
        check_paths(item['root'], item['path'])
        for directory in (item['root'], item['path'].parent):
            if not directory.exists():
                directory.mkdir(mode=0o750)
                os.chown(directory, 0, gid)
        replace(item['path'], item['tested'], gid)
        if item['path'].read_bytes() != item['tested']:
            raise RuntimeError('Installed skill bytes differ from the tested source')


def restore_skill_assets(plan, gid, replace):
    for item in plan:
        if item['original'] is None:
            item['path'].unlink(missing_ok=True)
        else:
            replace(item['path'], item['original'], gid)
