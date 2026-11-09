"""Read-only Telegram reaction-visibility diagnostics with sanitized reports."""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from telethon import errors
from telethon.tl import functions, types
REPORT_PATH = Path(os.environ.get('REACTION_RESEARCH_REPORT_PATH', '/home/user/reaction_research/latest_report.json'))

def _target_kind(entity) -> str:
    if isinstance(entity, types.Channel):
        if getattr(entity, 'broadcast', False):
            return 'broadcast_channel'
        if getattr(entity, 'megagroup', False):
            return 'megagroup'
        return 'channel'
    if isinstance(entity, types.Chat):
        return 'small_group'
    return type(entity).__name__

def _reaction_label(reaction) -> str:
    if isinstance(reaction, types.ReactionEmoji):
        return reaction.emoticon
    if isinstance(reaction, types.ReactionCustomEmoji):
        return '自定义表情'
    if isinstance(reaction, types.ReactionPaid):
        return '付费星星'
    return type(reaction).__name__

async def inspect_post_reactions(client, entity, post_id: int) -> dict:
    """Inspect only official reaction metadata and attempt one permitted list request."""
    message = await client.get_messages(entity, ids=post_id)
    if message is None:
        raise ValueError('未找到目标帖子，或当前会话无权访问该帖子')
    reactions = getattr(message, 'reactions', None)
    report = {'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'target_type': _target_kind(entity), 'post_id': post_id, 'reactions_present': reactions is not None, 'can_see_list': bool(getattr(reactions, 'can_see_list', False)), 'total_reactions': 0, 'reaction_counts': [], 'recent_reaction_entries_returned': 0, 'paid_top_reactor_entries_returned': 0, 'list_call_attempted': False, 'list_access': 'not_attempted_no_reaction_metadata', 'list_total_count': None, 'list_reaction_rows_returned': 0, 'list_user_objects_returned': 0, 'list_chat_objects_returned': 0, 'list_has_next_page': False, 'list_error': None, 'potential_visibility_inconsistency': False, 'identities_included': False, 'write_actions_performed': False}
    if reactions is None:
        return report
    counts = []
    for row in getattr(reactions, 'results', []) or []:
        count = int(getattr(row, 'count', 0))
        counts.append({'reaction': _reaction_label(row.reaction), 'count': count})
    report['reaction_counts'] = counts
    report['total_reactions'] = sum((row['count'] for row in counts))
    report['recent_reaction_entries_returned'] = len(getattr(reactions, 'recent_reactions', None) or [])
    report['paid_top_reactor_entries_returned'] = len(getattr(reactions, 'top_reactors', None) or [])
    report['list_call_attempted'] = True
    try:
        result = await client(functions.messages.GetMessageReactionsListRequest(peer=entity, id=post_id, limit=100))
        report['list_access'] = 'available'
        report['list_total_count'] = int(getattr(result, 'count', 0))
        report['list_reaction_rows_returned'] = len(getattr(result, 'reactions', []) or [])
        report['list_user_objects_returned'] = len(getattr(result, 'users', []) or [])
        report['list_chat_objects_returned'] = len(getattr(result, 'chats', []) or [])
        report['list_has_next_page'] = bool(getattr(result, 'next_offset', None))
        report['potential_visibility_inconsistency'] = not report['can_see_list']
    except errors.RPCError as exc:
        error_name = type(exc).__name__
        report['list_error'] = error_name
        if error_name == 'BroadcastForbiddenError':
            report['list_access'] = 'broadcast_forbidden'
        else:
            report['list_access'] = 'rpc_error'
        report['potential_visibility_inconsistency'] = report['can_see_list']
    except Exception as exc:
        report['list_access'] = 'error'
        report['list_error'] = type(exc).__name__
        report['potential_visibility_inconsistency'] = report['can_see_list']
    return report

def save_reaction_report(report: dict) -> Path:
    """Persist no session, auth key, reactor identity, message text, or credentials."""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True, mode=448)
    temporary = REPORT_PATH.with_suffix('.tmp')
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 384)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(payload)
    os.replace(temporary, REPORT_PATH)
    return REPORT_PATH
