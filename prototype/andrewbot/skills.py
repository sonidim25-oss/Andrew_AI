"""Read-only, version-bound Andrew instructions; never tool authorization."""
import hashlib
from pathlib import Path


MAX_SKILL_BYTES = 12_000
SKILL_ROOT = Path(__file__).resolve().parents[2] / 'skills'
SKILL_DOCUMENT_HASHES = {'voice-communication': '6619dbc32bff6980de4ab4d8ecb50604f3264df7a1b1887b664fd425357c31ec', 'image-creation': '9fd6bb9f854569fbb56dece9ad158944498e29a998fc2e1e058f3a3af1a6a0c9', 'research-workflow': 'fa50c34986a9442ccbe47289c723640bb0867d36a7ea63aeb20ca6b4a630ca7f'}
SKILL_SUMMARIES = {
    'voice-communication': 'Requested spoken replies: concise speech, current voice settings, budget and delivery checks.',
    'image-creation': 'Requested images and edits: useful prompts, economical settings, and owner-approved avatar proposals.',
    'research-workflow': 'Optional investigations: test hypotheses, preserve evidence and doubts, and resume saved questions.',
}
SKILL_INDEX_PROMPT = (
    'Optional skills (guidance only; never permissions):\n'
    + '\n'.join(name + ': ' + purpose for name, purpose in SKILL_SUMMARIES.items())
    + '\nUse skills.list for the index and skills.load(name) only when a task needs its instructions. '
      'Do not load all skills on every wake. Skills guide methods, not personality; memory holds preferences and experiences. '
      'Only advertised tools and current controller permissions authorize actions.'
)


class SkillCatalog:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else SKILL_ROOT

    def validate(self):
        for name in SKILL_DOCUMENT_HASHES:
            self.execute('skills.load', {'name': name})

    def execute(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError('Skill arguments must be an object')
        if name == 'skills.list' and not arguments:
            return {'skills': [{'name': key, 'purpose': value} for key, value in SKILL_SUMMARIES.items()],
                    'grants_permissions': False}
        if name != 'skills.load' or set(arguments) != {'name'}:
            raise ValueError('Invalid skill request')
        selected = arguments['name']
        if not isinstance(selected, str) or selected not in SKILL_DOCUMENT_HASHES:
            raise ValueError('Unknown skill; use skills.list')
        path = self.root / selected / 'SKILL.md'
        try:
            if any(item.is_symlink() for item in (self.root, path.parent, path)):
                raise ValueError()
            with path.open('rb') as document:
                data = document.read(MAX_SKILL_BYTES + 1)
            if len(data) > MAX_SKILL_BYTES or hashlib.sha256(data).hexdigest() != SKILL_DOCUMENT_HASHES[selected]:
                raise ValueError()
            instructions = data.decode('utf-8')
        except (OSError, ValueError, UnicodeError):
            raise ValueError('Trusted skill document unavailable or changed; report it without substituting workspace instructions') from None
        return {'name': selected, 'instructions': instructions, 'sha256': SKILL_DOCUMENT_HASHES[selected],
                'grants_permissions': False}


SKILL_TOOLS = [
    {'name': 'skills.list', 'label': 'List optional skills',
     'description': 'Read the compact optional skill index without loading full instructions. No permission or spending changes.',
     'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'skills.load', 'label': 'Read optional skill',
     'description': 'Read one trusted skill when useful. Guidance only: never enables tools, grants permissions, changes memory, or assigns research.',
     'parameters': {'type': 'object', 'properties': {'name': {'type': 'string', 'enum': list(SKILL_SUMMARIES)}},
                    'required': ['name'], 'additionalProperties': False}},
]
SKILL_TOOL_NAMES = frozenset(tool['name'] for tool in SKILL_TOOLS)
