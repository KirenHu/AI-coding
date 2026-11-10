#!/usr/bin/env python3
"""Collect read-only WBS evidence; failures are documented, never invented."""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys

PERIOD = re.compile(r'Week\d+[（(](\d{2})/(\d{2})\s*[-–]\s*(\d{2})/(\d{2})[）)]')


def label(n, start):
    return f'Week{n}（{start:%m/%d}-{start + timedelta(days=4):%m/%d}）'


def previous_wbs(config):
    paths = [p for p in (Path.home() / 'WBS').glob('*/周度计划表-*.xlsx') if p.is_file()]
    if paths:
        return str(max(paths, key=lambda p: (p.stat().st_mtime_ns, str(p))))
    baseline = config.get('baseline_wbs')
    return str(Path(baseline).expanduser()) if baseline else None


def periods(path, run_date):
    """Deduplicate by date range, not typo-prone Week numbers or repeated rows."""
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    found = {}
    has_header = False
    try:
        for sheet in book:
            # Some source files declare an incorrect A1:A225 dimension even
            # though B:H contain real cells. Do not trust cached dimensions.
            sheet.reset_dimensions()
            col = None
            for row in sheet.iter_rows(values_only=True):
                if col is None:
                    if '周期' in row and '事项' in row:
                        has_header = True
                        col = row.index('周期')
                    continue
                value = row[col] if len(row) > col else None
                if not isinstance(value, str):
                    continue
                for match in PERIOD.finditer(value):
                    sm, sd, em, ed = map(int, match.groups())
                    # A December period seen in January belongs to last year.
                    # Do not roll back the current week's Friday on a Monday run.
                    year = run_date.year - int(sm - run_date.month > 6)
                    start = date(year, sm, sd)
                    end = date(year + int((em, ed) < (sm, sd)), em, ed)
                    found.setdefault((start, end), match.group(0))
    finally:
        book.close()
    if not has_header:
        raise ValueError('未找到同时含“周期”和“事项”的表头')
    return [(a, b, text) for (a, b), text in found.items()]


def previous_plan_text(config, plan):
    """读取本人上一版计划（任意表格格式），按行原样输出，作为本周 WBS 的主线。"""
    from openpyxl import load_workbook
    # 优先：~/WBS/<YYYY>-W<ww>/ 中早于本周的最近一个周目录，取其中最新修改的文件（本人确认版通常最新）
    this_dir = plan['iso_week']
    week_dirs = sorted(d for d in (Path.home() / 'WBS').glob('[0-9][0-9][0-9][0-9]-W[0-9][0-9]') if d.is_dir() and d.name < this_dir)
    for d in reversed(week_dirs):
        files = [p for p in d.iterdir() if p.suffix == '.xlsx' and not p.name.startswith(('~', '.~'))]
        if files:
            latest = max(files, key=lambda p: p.stat().st_mtime_ns)
            return render_plan(latest)
    candidates = []
    if plan.get('prev_wbs'):
        candidates.append(Path(plan['prev_wbs']))
    for pattern in config.get('plan_sources') or []:
        pattern = str(Path(pattern).expanduser())
        root = Path(pattern.split('*', 1)[0]).parent if '*' in pattern else Path(pattern).parent
        candidates.extend(p for p in root.glob(str(Path(pattern).relative_to(root))) if p.is_file() and not p.name.startswith(('~', '.~')))
    if not candidates:
        return ['- 未配置上一版计划（baseline_wbs / plan_sources 均为空）。请在待确认里问本人本周计划。']
    return render_plan(max(candidates, key=lambda p: p.stat().st_mtime_ns))


def render_plan(latest):
    from openpyxl import load_workbook
    out = [f'来源：{latest}', '']
    book = load_workbook(latest, read_only=True, data_only=True)
    try:
        for sheet in book:
            sheet.reset_dimensions()
            out.append(f'### 工作表：{sheet.title}')
            for row in sheet.iter_rows(values_only=True):
                cells = [line(c) for c in row if c not in (None, '')]
                if cells:
                    out.append('- ' + ' ｜ '.join(cells))
    finally:
        book.close()
    return out


def make_plan(config, run_date, gaps):
    monday = run_date - timedelta(days=run_date.weekday())
    last_monday = monday - timedelta(days=7)
    prev = previous_wbs(config)
    past = []
    if prev:
        try:
            past = sorted((p for p in periods(prev, run_date) if p[1] < monday), key=lambda p: (p[1], p[0]))
        except Exception as exc:
            gaps.append(f'上一版 WBS 无法读取：{prev} — {exc}')
    if prev and not any(g.startswith('上一版 WBS 无法读取') for g in gaps):
        n = len(past) + 1
    else:
        n = (monday - date.fromisoformat(config['week1_start'])).days // 7 + 1
    iso = run_date.isocalendar()
    return {
        'run_date': run_date.isoformat(),
        'iso_week': f'{iso.year}-W{iso.week:02d}',
        'this_week': {'start': monday.isoformat(), 'end': (monday + timedelta(days=4)).isoformat(), 'label': label(n, monday)},
        'last_week': {'start': last_monday.isoformat(), 'end': (last_monday + timedelta(days=4)).isoformat(), 'label': past[-1][2] if past else label(n-1, last_monday)},
        'prev_wbs': prev,
    }


def text(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def line(value):
    return re.sub(r'\s+', ' ', text(value)).strip()


def empty_files(value):
    if not value or not text(value).strip():
        return True
    try:
        return not json.loads(value) if isinstance(value, str) else not value
    except (ValueError, TypeError):
        return False


# 只保留能对应到成果或决策的记录；change/bugfix/discovery 等过程记录由会话摘要概括
KEEP_TYPES = {'feature', 'decision', 'change'}
# 面试、薪资、人事类内容不进证据
SENSITIVE = re.compile(r'面试|薪资|期望涨幅|离职|入职|offer|简历|绩效|调薪', re.I)


def mem_evidence(config, start, end):
    records, gaps, dropped = [], [], 0
    path = Path.home() / '.claude-mem/claude-mem.db'
    if not path.exists():
        return records, ['claude-mem：数据库不存在，缺 AI 工作记录，请确认 claude-mem 是否在后台运行'], dropped
    projects = config.get('mem_projects') or []
    where = ' WHERE created_at_epoch >= ? AND created_at_epoch <= ?'
    params = [int(start.timestamp()*1000), int(end.timestamp()*1000)]
    if projects:
        where += ' AND project IN (' + ','.join('?' for _ in projects) + ')'
        params.extend(projects)
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    except sqlite3.Error as exc:
        return records, [f'claude-mem：{exc}'], dropped
    connection.row_factory = sqlite3.Row
    try:
        for table, columns in [
            ('observations', 'id,project,type,title,subtitle,narrative,facts,files_modified,created_at_epoch'),
            ('session_summaries', 'id,memory_session_id,project,request,completed,next_steps,created_at_epoch'),
        ]:
            try:
                rows = connection.execute('SELECT '+columns+' FROM '+table+where+' ORDER BY created_at_epoch,id', params)
                rows = [dict(row) for row in rows]
                if table == 'session_summaries':
                    latest = {}
                    for r in rows:
                        latest[r['memory_session_id']] = r
                    dropped += len(rows) - len(latest)
                    rows = list(latest.values())
                for r in rows:
                    if table == 'observations' and r['type'] not in KEEP_TYPES:
                        dropped += 1
                        continue
                    if SENSITIVE.search(' '.join(text(v) for v in r.values() if isinstance(v, str))):
                        dropped += 1
                        continue
                    stamp = datetime.fromtimestamp(r['created_at_epoch']/1000).astimezone()
                    if table == 'observations':
                        excerpt = line(' / '.join(text(r[k]) for k in ['subtitle', 'narrative'] if r[k]))[:120]
                        content = f"{stamp:%m-%d} {line(r['type'])} {line(r['title'])} — {excerpt}"
                    else:
                        content = f"{stamp:%m-%d} summary {line(text(r['request'])+' — '+text(r['completed']))[:200]}"
                    records.append((r['project'] or '未标注项目', content, r['created_at_epoch'], r['id']))
            except sqlite3.Error as exc:
                gaps.append(f'claude-mem {table}：{exc}')
    finally:
        connection.close()
    if not records and not gaps:
        gaps.append('claude-mem：窗口内无保留记录，缺 AI 工作记录，请确认 claude-mem 是否在后台运行')
    records.sort(key=lambda r: (r[0], r[2], r[3]))
    return records, gaps, dropped


def unwrap(payload):
    if isinstance(payload, dict):
        for key in ('data', 'result'):
            if isinstance(payload.get(key), (dict, list)):
                return payload[key]
    return payload


def collection(payload, key):
    body = unwrap(payload)
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get(key), list):
        return body[key]
    raise ValueError(f'未知返回结构，缺少 {key} 数组（不能视为空结果）')


def first(obj, *keys):
    if not isinstance(obj, dict):
        return None
    for key in keys:
        if obj.get(key) is not None:
            return obj[key]
    return None


def timestamp(value):
    """ISO strings; DingTalk native numeric epoch values use milliseconds."""
    if value is None or value == '' or value == 0:
        return None
    if isinstance(value, dict):
        value = first(value, 'dateTime', 'datetime', 'date', 'timestamp')
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdecimal()):
            return datetime.fromtimestamp(float(value)/1000).astimezone()
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def display_time(value):
    stamp = timestamp(value)
    return stamp.isoformat(timespec='seconds') if stamp else (line(value) or '未知')


def completeness(payload):
    body = unwrap(payload)
    nodes = [payload, body]
    pagination = payload.get('meta', {}).get('pagination', {}) if isinstance(payload, dict) else {}
    result = {}
    for node in nodes:
        if isinstance(node, dict):
            for key in ('complete', 'hasMore', 'stopReason', 'failures', 'partial'):
                if key in node:
                    result[key] = node[key]
    if pagination:
        result['pagination'] = pagination
    return result


class DWS:
    # Explicit allowlist prevents a configuration value from becoming a write command.
    ALLOWED = {('minutes', '+search'), ('minutes', '+detail'), ('minutes', '+action-items'),
               ('todo', '+get-related-tasks'), ('calendar', '+agenda'), ('chat', '+chat-messages')}

    def __init__(self):
        self.gaps = []
        self.ledger = []

    def call(self, service, leaf, *args, check_complete=True):
        if (service, leaf) not in self.ALLOWED:
            raise ValueError('只允许读取类命令')
        cmd = ['dws', service, leaf, *args, '--format', 'json']
        command = shlex.join(cmd)
        payload = None
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if result.stdout.strip():
                try:
                    payload = json.loads(result.stdout)
                except json.JSONDecodeError:
                    if result.returncode == 0:
                        raise ValueError('stdout 不是合法 JSON')
            error = payload.get('error') if isinstance(payload, dict) else None
            failed = result.returncode != 0 or bool(error) or (isinstance(payload, dict) and payload.get('ok') is False)
            if failed:
                # Keep the exact stderr first line, plus a useful structured message.
                stderr_first = result.stderr.splitlines()[0] if result.stderr.strip() else '(stderr 为空)'
                diagnostic = error.get('message') if isinstance(error, dict) else None
                if not diagnostic and result.stderr.strip():
                    try:
                        diagnostic = json.loads(result.stderr).get('error', {}).get('message')
                    except ValueError:
                        pass
                self.gaps.append(f'`{command}` — exit={result.returncode}；stderr 首行：{stderr_first}；{line(diagnostic)[:500]}')
            status = completeness(payload)
            self.ledger.append({'command': command, 'exit_code': result.returncode, **status})
            if check_complete and (status.get('complete') is False or status.get('hasMore') is True or status.get('failures') or status.get('partial')):
                self.gaps.append(f'`{command}` — 返回不完整：{json.dumps(status, ensure_ascii=False)}')
            # Partial payloads may contain successful independent records; caller validates shape.
            return payload
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            self.gaps.append(f'`{command}` — {type(exc).__name__}: {line(exc)}')
            self.ledger.append({'command': command, 'error': type(exc).__name__})
            return None

    def rows(self, payload, key, source):
        if payload is None:
            return []
        if isinstance(payload, dict) and payload.get('ok') is False and not payload.get('data'):
            return []
        try:
            return collection(payload, key)
        except ValueError as exc:
            self.gaps.append(f'{source}：{exc}')
            return []


def strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


def relevant_paragraphs(value):
    result, relevant = [], False
    for raw in strings(value):
        for paragraph in raw.splitlines():
            if re.search(r'决策|决定|行动|待办|进度|进展|完成|交付|后续|下一步|decision|action|progress|todo', paragraph, re.I):
                relevant = True
            elif re.match(r'^\s*#{1,6}\s+', paragraph):
                relevant = False
            if relevant and paragraph.strip():
                result.append(paragraph.strip())
    return '\n'.join(result)


def minutes_evidence(dws, start, end):
    payload = dws.call('minutes', '+search', '--scope', 'all', '--start', start.isoformat(), '--end', end.isoformat(), '--page-all')
    rows = dws.rows(payload, 'minutes', '钉钉听记')
    selected, seen = [], set()
    for row in rows:
        uid = first(row, 'taskUuid')
        if not uid:
            dws.gaps.append('钉钉听记：条目缺 taskUuid，无法读取摘要及行动项')
            continue
        if uid in seen:
            continue
        seen.add(uid)
        rawtime = first(row, 'createdAt', 'createTime', 'startTime', 'created_at')
        stamp = timestamp(rawtime)
        if stamp and not start <= stamp <= end:
            continue
        # Search itself applies the requested window; preserve unknown time explicitly.
        selected.append((uid, row, rawtime))

    def detail(item):
        uid, row, rawtime = item
        payload = dws.call('minutes', '+detail', '--id', str(uid), '--artifacts', 'summary,todos')
        if payload is None:
            return (line(first(row, 'title', 'name')) or str(uid)) + '；时间：' + display_time(rawtime) + '；摘要/行动项缺失'
        body = unwrap(payload)
        # Artifacts remain tied to this exact requested taskUuid; unknown shapes are gaps.
        if isinstance(body, dict):
            bundles = first(body, 'items', 'results', 'bundles')
            if isinstance(bundles, list):
                body = next((b for b in bundles if first(b, 'taskUuid', 'id') == uid), {})
            artifacts = first(body, 'artifacts') or body
        else:
            artifacts = {}
        summary = first(artifacts, 'summary')
        todos = first(artifacts, 'todos', 'actions', 'actionItems')
        if summary is None:
            dws.gaps.append(f'钉钉听记 {uid}：摘要返回结构未知或缺失')
        if todos is None:
            dws.gaps.append(f'钉钉听记 {uid}：行动项返回结构未知或缺失')
        content = relevant_paragraphs(summary)
        if todos is not None:
            content += '\n行动项：' + '；'.join(strings(todos))
        if not content.strip():
            content = '未取得与决策/行动项/进度相关段落；仅有元信息'
        url = first(row, 'url', 'link') or first(body, 'url', 'link') or '未提供 URL'
        return f"{line(first(row, 'title', 'name')) or uid}；时间：{display_time(rawtime)}；{line(content)[:600]}；{line(url)}"

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(detail, selected))


def todo_evidence(dws, start, end):
    payload = dws.call('todo', '+get-related-tasks')
    rows = dws.rows(payload, 'tasks', '钉钉待办')
    result, seen = [], set()
    for row in rows:
        uid = first(row, 'taskId', 'id')
        if uid and uid in seen:
            continue
        if uid:
            seen.add(uid)
        created = timestamp(first(row, 'createdAt', 'createTime', 'createdTime', 'gmtCreate'))
        completed = timestamp(first(row, 'completedAt', 'finishTime', 'completedTime', 'gmtFinished'))
        due_value = first(row, 'dueDate', 'dueTime', 'due', 'planFinishTime', 'planFinishDate')
        due = timestamp(due_value)
        state = first(row, 'status', 'todoStatus', 'done', 'isDone')
        done = state is True or str(state).lower() in ('true', 'done', 'completed', '已完成', 'finish', 'finished')
        # Include overdue outstanding tasks even if their deadline precedes the window.
        overdue = due is not None and due < end and (not done or (completed and completed >= start))
        within = any(t is not None and start <= t <= end for t in (created, completed))
        if within or overdue:
            result.append(f"{line(first(row, 'title', 'subject', 'name'))}；状态：{line(state) or '未知'}{'（逾期）' if overdue else ''}；截止：{display_time(due_value)}")
        elif created is None and completed is None and due is None:
            dws.gaps.append(f'钉钉待办 {uid or "未知ID"}：返回缺少可筛选时间，无法判定窗口，未输出')
    return result


def calendar_evidence(dws, start, friday):
    end = datetime.combine(friday + timedelta(days=1), time.min).astimezone()
    args = ['--start', start.isoformat(), '--end', end.isoformat()]
    result, cursors, seen = [], set(), set()
    for _ in range(100):
        payload = dws.call('calendar', '+agenda', *args, check_complete=False)
        rows = dws.rows(payload, 'events', '钉钉日程')
        for row in rows:
            uid = first(row, 'eventId', 'id')
            if uid and uid in seen:
                continue
            if uid:
                seen.add(uid)
            a, b = first(row, 'start', 'startTime'), first(row, 'end', 'endTime')
            result.append(f"{line(first(row, 'title', 'summary', 'name'))}；{display_time(a)} — {display_time(b)}")
        if not isinstance(payload, dict):
            break
        page = payload.get('meta', {}).get('pagination', {})
        body = unwrap(payload)
        cursor = page.get('next_token') or first(body, 'nextCursor')
        if not cursor:
            status = completeness(payload)
            if status.get('complete') is False or status.get('hasMore') is True or status.get('failures'):
                dws.gaps.append('钉钉日程：返回不完整且缺少续页游标 — ' + json.dumps(status, ensure_ascii=False))
            break
        if cursor in cursors:
            dws.gaps.append('钉钉日程：分页游标循环，结果不完整')
            break
        cursors.add(cursor)
        args = args[:4] + ['--cursor', str(cursor)]
    else:
        dws.gaps.append('钉钉日程：达到 100 页上限，结果不完整')
    return result


def chat_evidence(dws, config, start, end):
    result = []
    for group in config.get('chat_groups') or []:
        cid = first(group, 'openConversationId')
        name = first(group, 'name') or cid
        if not cid:
            dws.gaps.append(f'群消息 {name}：配置缺 openConversationId，未查询')
            continue
        payload = dws.call('chat', '+chat-messages', '--group', cid, '--start', start.isoformat(timespec='seconds'), '--end', end.isoformat(timespec='seconds'), '--order', 'asc', '--page-all', '--no-reactions')
        count = 0
        for row in dws.rows(payload, 'messages', f'群消息 {name}'):
            kind = first(row, 'msgType', 'messageType', 'type')
            if kind is not None and str(kind).lower() not in ('text', '1'):
                continue
            content = first(row, 'text', 'content')
            if isinstance(content, dict):
                content = first(content, 'text', 'content')
            if not isinstance(content, str):
                continue
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    content = first(parsed, 'text', 'content') or ''
            except ValueError:
                pass
            if not isinstance(content, str) or len(content.strip()) < 15:
                continue
            rawtime = first(row, 'createTime', 'createdAt', 'sendTime', 'timestamp')
            stamp = timestamp(rawtime)
            if stamp and not start <= stamp <= end:
                continue
            sender = first(row, 'senderName', 'sender', 'senderId')
            result.append(f'{line(name)}｜{line(sender)} {display_time(rawtime)} {line(content)[:200]}')
            count += 1
            if count == 200:
                dws.gaps.append(f'群消息 {name}：已按配置上限保留 200 条合格文本（其余不输出）')
                break
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-only', action='store_true', help='仅输出周次 JSON，不采集来源')
    parser.add_argument('--out', help='evidence.md 输出路径')
    parser.add_argument('--config', default='~/WBS/config.json', help='配置 JSON（默认 ~/WBS/config.json）')
    parser.add_argument('--today', type=date.fromisoformat, help='覆盖用于周次计算的运行日期 YYYY-MM-DD；窗口仍截止真实运行时刻')
    args = parser.parse_args(argv)
    path = Path(args.config).expanduser()
    if not path.is_file():
        print(f'配置缺失：{path}；请先运行 prompts/setup.md 创建配置。', file=sys.stderr)
        return 2
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(config, dict):
            raise ValueError('配置必须是 JSON 对象')
        date.fromisoformat(config['week1_start'])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'配置错误：{exc}', file=sys.stderr)
        return 2
    now = datetime.now().astimezone()
    gaps = []
    plan = make_plan(config, args.today or now.date(), gaps)
    if args.plan_only:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        for gap in gaps:
            print(gap, file=sys.stderr)
        return 0
    if not args.out:
        parser.error('采集模式必须提供 --out')
    start = datetime.combine(date.fromisoformat(plan['last_week']['start']), time.min).astimezone()
    mem, mem_gaps, dropped = mem_evidence(config, start, now)
    gaps.extend(mem_gaps)
    dws = DWS()
    sections = {'钉钉听记': [], '钉钉待办': [], '钉钉日程': [], '钉钉群消息': []}
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = {
            '钉钉听记': pool.submit(minutes_evidence, dws, start, now),
            '钉钉待办': pool.submit(todo_evidence, dws, start, now),
            '钉钉日程': pool.submit(calendar_evidence, dws, start, date.fromisoformat(plan['this_week']['end'])),
            '钉钉群消息': pool.submit(chat_evidence, dws, config, start, now),
        }
        for source, task in tasks.items():
            try:
                kept = [e for e in task.result() if not SENSITIVE.search(e)]
                dropped += len(task.result()) - len(kept)
                sections[source] = kept
            except Exception as exc:
                gaps.append(f'{source}：{type(exc).__name__}: {line(exc)}')
    gaps.extend(dws.gaps)
    if not config.get('chat_groups'):
        gaps.append('钉钉群消息：chat_groups 为空，未配置采集群')
    counts = {'claude-mem': len(mem), **{k: len(v) for k, v in sections.items()}}
    if sum(counts.values()) < 3:
        gaps.append('证据不足 3 条，请重点人工补充')
    output = [f"# WBS 证据 {plan['iso_week']} · {plan['this_week']['label']}", '', f'证据窗口（本地时区）：{start.isoformat()} 至 {now.isoformat()}', f"日程窗口：{plan['last_week']['start']} 至 {plan['this_week']['end']}（含周五）", '', '## 来源缺口', '']
    output.extend(['- '+gap for gap in gaps] or ['- 无'])
    output.extend(['', '## 上一版计划（主线，逐行核对状态并延续到本周）', ''])
    output.extend(previous_plan_text(config, plan))
    output.extend(['', '## claude-mem（按 project_map 归到业务项目）', ''])
    number = 0
    groups = defaultdict(list)
    project_map = config.get('project_map') or {}
    for project, content, *_ in mem:
        groups[project_map.get(project, project)].append(content)
    for project, entries in groups.items():
        output.extend([f'### {line(project)}', ''])
        for entry in entries:
            number += 1
            output.append(f'- [E{number}] {entry}')
    if not mem:
        output.append('无保留记录。')
    for source, entries in sections.items():
        output.extend(['', '## '+source, ''])
        for entry in entries:
            number += 1
            output.append(f'- [E{number}] {entry}')
        if not entries:
            output.append('无保留记录（失败/未配置请见来源缺口）。')
    output.extend(['', '## 统计', '', '| 来源 | 条数 |', '|---|---:|'])
    output.extend(f'| {key} | {value} |' for key, value in counts.items())
    output.extend([f'| 丢弃记录（过程类、重复会话摘要、人事敏感） | {dropped} |', '', '### 读取命令完整性台账', ''])
    output.extend('- '+json.dumps(entry, ensure_ascii=False) for entry in dws.ledger)
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('w', encoding='utf-8') as stream:  # evidence 是可重建的中间产物，重跑时覆盖
        stream.write('\n'.join(output)+'\n')
    print(json.dumps({'out': str(out), 'counts': counts, 'discarded_process_records': dropped, 'gaps': gaps}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
