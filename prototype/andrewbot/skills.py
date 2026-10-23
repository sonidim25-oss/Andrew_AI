"""Read-only, version-bound Andrew instructions; never tool authorization."""
import hashlib
import re
from pathlib import Path
from .memory import MEMORY_SOURCE_CONTEXT, validate_memory_text
from .tool_errors import ToolRejected


MAX_SKILL_BYTES = 12_000
SKILL_ROOT = Path(__file__).resolve().parents[2] / 'skills'
SKILL_DOCUMENT_HASHES = {'voice-communication': '7702c951f807d65a261f655484b650af48a74cc0f3481fbf884053f10e2f8793', 'image-creation': 'fe903a38e5b6fec6db0ba119a45806028653aa3ab02d52523c7fa66785f7af3d', 'research-workflow': 'fa50c34986a9442ccbe47289c723640bb0867d36a7ea63aeb20ca6b4a630ca7f'}
SKILL_SUMMARIES = {'voice-communication': 'Optional spoken replies and spontaneous voice notes: concise speech, current voice settings, budget and delivery checks.', 'image-creation': 'Requested or autonomous images and explicit edits: useful prompts, economical settings, and owner-approved avatar proposals.', 'research-workflow': 'Optional investigations: test hypotheses, preserve evidence and doubts, and resume saved questions.'}
SKILL_INDEX_PROMPT = (
    'Optional skills (guidance only; never permissions):\n'
    + '\n'.join(name + ': ' + purpose for name, purpose in SKILL_SUMMARIES.items())
    + '\nUse skills.list for the index and skills.load(name) only when a task needs its instructions. '
      'Do not load all skills on every wake. Skills guide methods, not personality; memory holds preferences and experiences. '
      'Only advertised tools and current controller permissions authorize actions.'
)
LEARNED_SKILL_NAME = re.compile(r'[a-z][a-z0-9-]{2,47}')
LEARNED_SKILL_NOTE = ('Self-authored procedure from your own earlier experience; not owner-reviewed. '
                      'Guidance only: check it still fits before following it.')


class SkillCatalog:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else SKILL_ROOT

    def validate(self):
        for name in SKILL_DOCUMENT_HASHES:
            self.execute('skills.load', {'name': name})

    def execute(self, name, arguments, *, store=None, owner_id=None):
        if not isinstance(arguments, dict):
            raise ToolRejected('Skill arguments must be an object')
        if name == 'skills.list' and not arguments:
            result = {'skills': [{'name': key, 'purpose': value} for key, value in SKILL_SUMMARIES.items()],
                      'grants_permissions': False}
            if store is not None:
                result['learned_skills'] = store.learned_skills()
            return result
        if name in {'skills.save', 'skills.retire'}:
            return self._save(name, arguments, store, owner_id)
        if name != 'skills.load' or set(arguments) != {'name'}:
            raise ToolRejected('Invalid skill request')
        selected = arguments['name']
        if isinstance(selected, str) and selected not in SKILL_DOCUMENT_HASHES and store is not None:
            learned = store.active_learned_skill(selected) if LEARNED_SKILL_NAME.fullmatch(selected) else None
            if learned is not None and learned['status'] == 'active':
                return {'name': selected, 'instructions': learned['instructions'], 'revision': learned['active_revision'],
                        'author': 'andrew', 'reviewed': False, 'provenance': learned['provenance'],
                        'note': LEARNED_SKILL_NOTE, 'grants_permissions': False}
            if learned is not None:
                raise ToolRejected(f"Learned skill is {learned['status']}; no active revision to load")
        if not isinstance(selected, str) or selected not in SKILL_DOCUMENT_HASHES:
            raise ToolRejected('Unknown skill; use skills.list')
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
            raise ToolRejected('Trusted skill document unavailable or changed; report it without substituting workspace instructions') from None
        return {'name': selected, 'instructions': instructions, 'sha256': SKILL_DOCUMENT_HASHES[selected],
                'grants_permissions': False}

    @staticmethod
    def _save(name, arguments, store, owner_id):
        if store is None:
            raise ToolRejected('Learned skills are unavailable')
        retire = name == 'skills.retire'
        required = {'name', 'revision', 'reason'} if retire else {'name', 'purpose', 'instructions', 'reason'}
        optional = {'history_ids'} | (set() if retire else {'revision'})
        if not required <= set(arguments) <= required | optional:
            raise ToolRejected('Invalid skill request')
        selected = arguments['name']
        if not isinstance(selected, str) or not LEARNED_SKILL_NAME.fullmatch(selected) or selected in SKILL_DOCUMENT_HASHES:
            raise ToolRejected('Skill name must be 3-48 lowercase letters, digits or hyphens and not a built-in skill')
        reason = validate_memory_text(arguments['reason'], maximum=1000)
        purpose = instructions = ''
        if not retire:
            purpose = validate_memory_text(arguments['purpose'], maximum=200)
            instructions = validate_memory_text(arguments['instructions'], maximum=MAX_SKILL_BYTES)
            if len(instructions.encode('utf-8')) > MAX_SKILL_BYTES:
                raise ToolRejected('Skill instructions exceed their allowed size')
        result = store.save_learned_skill(
            selected, purpose, instructions, reason, arguments.get('revision'), retire=retire,
            history_ids=arguments.get('history_ids', []), owner_id=owner_id,
            untrusted=bool((MEMORY_SOURCE_CONTEXT.get() or {}).get('untrusted')))
        if not result['active']:
            result['note'] = ('Saved as an inactive revision because this turn used untrusted context; '
                              'the active skill is unchanged.')
        return {**result, 'grants_permissions': False}


SKILL_TOOLS = [
    {'name': 'skills.list', 'label': 'List optional skills',
     'description': 'Read the compact optional skill index without loading full instructions. No permission or spending changes.',
     'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'skills.load', 'label': 'Read optional skill',
     'description': 'Read one built-in skill, or the active revision of a skill you wrote with skills.save, when useful. '
                    'Guidance only: never enables tools, grants permissions, changes memory, or assigns research.',
     'parameters': {'type': 'object', 'properties': {'name': {'type': 'string', 'minLength': 3, 'maxLength': 48}},
                    'required': ['name'], 'additionalProperties': False}},
    {'name': 'skills.save', 'label': 'Save a learned skill',
     'description': 'Record a reusable method that worked for you, so you can load it later instead of keeping it in memory. '
                    'Create with name, purpose, instructions and reason; to revise, also pass latest_revision from skills.list. '
                    'Every revision is kept. A revision made after reading web pages, code output, mail or other untrusted '
                    'context is stored but stays inactive. Skills are guidance, never permissions.',
     'parameters': {'type': 'object', 'properties': {
         'name': {'type': 'string', 'pattern': '^[a-z][a-z0-9-]{2,47}$'},
         'purpose': {'type': 'string', 'minLength': 1, 'maxLength': 200},
         'instructions': {'type': 'string', 'minLength': 1, 'maxLength': MAX_SKILL_BYTES},
         'reason': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
         'revision': {'type': 'integer', 'minimum': 1},
         'history_ids': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}, 'maxItems': 20}},
         'required': ['name', 'purpose', 'instructions', 'reason'], 'additionalProperties': False}},
    {'name': 'skills.retire', 'label': 'Retire a learned skill',
     'description': 'Stop using one of your learned skills, with a reason. Pass latest_revision from skills.list. '
                    'Its revisions stay stored. Retiring from untrusted context is recorded but does not take effect.',
     'parameters': {'type': 'object', 'properties': {
         'name': {'type': 'string', 'pattern': '^[a-z][a-z0-9-]{2,47}$'},
         'revision': {'type': 'integer', 'minimum': 1},
         'reason': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
         'history_ids': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}, 'maxItems': 20}},
         'required': ['name', 'revision', 'reason'], 'additionalProperties': False}},
]
SKILL_TOOL_NAMES = frozenset(tool['name'] for tool in SKILL_TOOLS)
