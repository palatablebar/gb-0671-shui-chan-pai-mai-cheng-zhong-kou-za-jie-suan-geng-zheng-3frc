"""批次日结：同一批次号下多个源包版本的比对与唯一封存。

上游在同一批次号下补发称重成交文件时，不直接覆盖已采用的结果，也不另起
一个可付账的批次；而是把补发包登记为同批次的候选源包，确认内容后与当前
采用版逐票比对。只有所有票都能在版本间按票号对应、且候选包自身可归档时，
才允许把采用指针切到新包。原始包、候选包和每次比对证据都保留。

比对口径：
- 两版都有此票且都能结算：按影响结算的字段比对，值不同记“变化票”。
- 两版都有此票但采用版能结算、补发包不能结算：记“无法对应”，阻断切换和封存。
- 采用版被隔离、补发包已能结算：记“修复票”，属于合法变化。
- 只有一版有该票：记“无法对应”，阻断切换和封存。
- 两版都不能结算：记“未结算票”，两边都没有应付，不阻断。
"""
from decimal import Decimal, InvalidOperation

from app.coerce import money

# 影响结算结论的字段；原始行号变化不算票值变化。
FINGERPRINT_FIELDS = ('version', 'fish', 'gross_kg', 'tare_kg', 'net_kg',
                      'billable_kg', 'price', 'rate', 'paid', 'amount', 'due', 'replaces')


def _ticket_of(entry):
    raw = entry.get('raw') or {}
    return str(raw.get('ticket') or entry.get('ticket') or '').strip()


def ticket_states(state):
    """把一次运行整理成票号 -> 状态。状态区分生效、版本冲突和隔离。"""
    states = {}
    duplicates = set()
    for row in state.get('final', []):
        ticket = str(row.get('ticket', '')).strip()
        if not ticket:
            continue
        if ticket in states:
            duplicates.add(ticket)
            continue
        states[ticket] = {'status': 'ok', 'row': row}
    for conflict in state.get('version_conflicts', []):
        ticket = str(conflict.get('ticket', '')).strip()
        if ticket and ticket not in states:
            states[ticket] = {'status': 'conflict',
                              'code': conflict.get('code', 'VERSION_CONFLICT'),
                              'detail': conflict.get('detail', '版本链无法闭环')}
    for entry in state.get('quarantine', []):
        ticket = _ticket_of(entry)
        if not ticket or ticket in states:
            continue
        states[ticket] = {'status': 'quarantine',
                          'code': entry.get('code', 'QUARANTINED'),
                          'detail': entry.get('detail', '行被隔离')}
    for ticket in duplicates:
        states[ticket] = {'status': 'ambiguous', 'code': 'TICKET_AMBIGUOUS',
                          'detail': '同一票出现多条生效记录'}
    return states


def _value(row):
    return {field: row.get(field, '') for field in FINGERPRINT_FIELDS}


def _amounts(state):
    total = paid = due = Decimal('0')
    final = state.get('final', [])
    for row in final:
        for key, which in (('amount', 'total'), ('paid', 'paid'), ('due', 'due')):
            try:
                value = Decimal(str(row.get(key, '0')))
            except InvalidOperation:
                value = Decimal('0')
            if which == 'total':
                total += value
            elif which == 'paid':
                paid += value
            else:
                due += value
    return {'tickets': len(final), 'amount': money(total), 'paid': money(paid), 'due': money(due)}


def _status_note(item):
    if item is None:
        return 'missing'
    return item['status']


def compare(base, candidate):
    """逐票比对当前采用版 base 与候选补发包 candidate。

    base 为 None 表示该批次的首个源包，没有可比对象。
    held 非空表示存在无法对应的票，不得切换或封存。
    """
    changed, repaired, unsettled = [], [], []
    held, added, removed = [], [], []
    unchanged = 0

    if base is not None:
        base_states = ticket_states(base)
        candidate_states = ticket_states(candidate)
        for ticket in sorted(set(base_states) | set(candidate_states)):
            left = base_states.get(ticket)
            right = candidate_states.get(ticket)
            if left is None:
                added.append(ticket)
                held.append({'ticket': ticket, 'code': 'TICKET_UNMATCHED',
                             'detail': '补发包新增票，无法在采用版中对应',
                             'base': 'missing', 'candidate': _status_note(right)})
                continue
            if right is None:
                removed.append(ticket)
                held.append({'ticket': ticket, 'code': 'TICKET_UNMATCHED',
                             'detail': '补发包缺少采用版中的票，无法对应',
                             'base': _status_note(left), 'candidate': 'missing'})
                continue
            base_ok, candidate_ok = left['status'] == 'ok', right['status'] == 'ok'
            if base_ok and candidate_ok:
                before, after = _value(left['row']), _value(right['row'])
                if before != after:
                    changed.append({'ticket': ticket, 'before': before, 'after': after,
                                    'amount_before': before.get('amount', ''),
                                    'amount_after': after.get('amount', '')})
                else:
                    unchanged += 1
            elif base_ok and not candidate_ok:
                held.append({'ticket': ticket, 'code': 'TICKET_LOST',
                             'detail': '采用版可结算，补发包无法结算',
                             'base': 'ok', 'candidate': right['status'],
                             'candidate_detail': right.get('detail', ''),
                             'amount_before': left['row'].get('amount', '')})
            elif not base_ok and candidate_ok:
                after = _value(right['row'])
                changed.append({'ticket': ticket, 'before': None, 'after': after,
                                'repaired': True,
                                'base_status': left['status'],
                                'base_detail': left.get('detail', ''),
                                'amount_before': '', 'amount_after': after.get('amount', '')})
                repaired.append(ticket)
            else:
                unsettled.append({'ticket': ticket, 'base': left['status'],
                                  'candidate': right['status']})

    archive = candidate.get('archive', {})
    balance = candidate.get('balance', {})
    reasons = []
    if held:
        reasons.append('存在无法在版本间对应的票，暂缓封存')
    if archive.get('status') != '可归档':
        reasons.append('候选包尚未通过归档校验')
    if not balance.get('row_balanced', False):
        reasons.append('候选包行不守恒')
    if not balance.get('money_balanced', False):
        reasons.append('候选包金额不守恒')
    if balance.get('blocked'):
        reasons.append('候选包存在版本冲突')

    return {
        'changed': changed, 'repaired': repaired, 'unsettled': unsettled,
        'held': held, 'added': added, 'removed': removed,
        'unchanged': unchanged, 'reasons': reasons, 'ready': not reasons,
        'base': _amounts(base) if base is not None else None,
        'candidate': _amounts(candidate),
        'base_ready': base.get('archive', {}).get('status') == '可归档' if base is not None else None,
        'candidate_ready': archive.get('status') == '可归档',
    }
