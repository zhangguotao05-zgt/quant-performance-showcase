"""Read-only arithmetic and integrity checks for the anonymous public bundle."""
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
ALIASES = list('ABCDEF')
EXPECTED = {
    '.gitignore', 'README.md', 'SHA256SUMS', 'docs/methodology.md', 'docs/updating.md',
    'reports/backtest.md', 'reports/paper-monthly.md', 'scripts/verify.py',
    'data/backtest.json', 'data/backtest-portfolio-daily.json',
    'data/paper-monthly.json', 'data/paper-equity-daily.json',
    'data/paper-regimes.json', 'reports/parameter-changes.md',
    'assets/backtest.png', 'assets/paper-monthly.png',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def close(actual, expected):
    require(math.isfinite(actual) and math.isfinite(expected), 'Non-finite number')
    require(math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-8),
            f'Numeric mismatch: {actual} != {expected}')


def parse(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def load(name):
    return json.loads((ROOT / 'data' / name).read_text(encoding='utf-8'))


def drawdown(values):
    peak = values[0]
    worst = 0.0
    for value in values:
        require(value > 0, 'Non-positive equity')
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def check_files():
    actual = {path.relative_to(ROOT).as_posix() for path in ROOT.rglob('*')
              if path.is_file() and '.git' not in path.parts and '__pycache__' not in path.parts}
    require(actual == EXPECTED, 'Unexpected or missing public files: ' + str(actual ^ EXPECTED))
    manifest = {}
    for line in (ROOT / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        digest, name = line.split('  ', 1)
        require(name in EXPECTED and name not in manifest and name != 'SHA256SUMS', 'Invalid hash entry')
        require(re.fullmatch('[0-9a-f]{64}', digest) is not None, 'Invalid digest')
        manifest[name] = digest
        require(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, 'Hash mismatch: ' + name)
    require(set(manifest) == EXPECTED - {'SHA256SUMS'}, 'Incomplete manifest')
    for name in EXPECTED:
        if not name.endswith(('.md', '.json', '.py')):
            continue
        text = (ROOT / name).read_text(encoding='utf-8')
        require(re.search(r'^-{5}BEGIN [A-Z ]*PRIVATE KEY-{5}$', text, flags=re.M) is None,
                'Private key material found')
        require(re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text) is None, 'Unexpected network address')
        if name.endswith('.md'):
            for target in re.findall(r'\]\(([^)]+)\)', text):
                if not target.startswith(('https://', '#')):
                    require((ROOT / name).parent.joinpath(target).is_file(), 'Broken link: ' + target)


def check_backtest():
    data = load('backtest.json')
    primary = data['primary']
    curve = load('backtest-portfolio-daily.json')
    require(data['type'] == 'historical_backtest' and data['initial_capital_usdt'] == 12000,
            'Unexpected historical capital or type')
    require(data['leverage'] == 5, 'Unexpected historical leverage')
    require(set(data['strategy_summary']) == set(ALIASES + ['portfolio']), 'Invalid historical aliases')
    require(len(data['annual']) == 49, 'Unexpected historical annual rows')
    daily = {parse(row['timestamp']): row['nav'] for row in curve['daily_nav']}
    require(len(daily) == len(curve['daily_nav']), 'Duplicate historical daily timestamp')
    close(primary['start_nav'], daily[parse(primary['start'])])
    close(primary['end_nav'], daily[parse(primary['end'])])
    close(primary['final_equity_usdt'], primary['end_nav'] * 12000)
    close(primary['max_drawdown'], min(row['drawdown'] for row in curve['daily_worst_30m_drawdown']))
    for alias in ALIASES + ['portfolio']:
        summary = data['strategy_summary'][alias]
        close(summary['return_total'], summary['end_nav'] / summary['start_nav'] - 1)
        days = (parse(summary['end']) - parse(summary['start'])).total_seconds() / 86400
        close(summary['days'], days)
        close(summary['cagr'], (summary['end_nav'] / summary['start_nav']) ** (365.25 / days) - 1)
        annual = sorted([row for row in data['annual'] if row['strategy'] == alias], key=lambda row: row['year'])
        require([row['year'] for row in annual] == list(range(2020, 2027)), 'Missing historical year')
        for index, row in enumerate(annual):
            close(row['return_total'], row['end_nav'] / row['start_nav'] - 1)
            require(-1 < row['max_drawdown'] <= 0, 'Invalid historical drawdown')
            if index:
                close(annual[index-1]['end_nav'], row['start_nav'])
                require(annual[index-1]['end'] == row['start'], 'Non-contiguous annual periods')
            if alias == 'portfolio':
                close(row['start_nav'], daily[parse(row['start'])])
                close(row['end_nav'], daily[parse(row['end'])])
        close(math.prod(1+row['return_total'] for row in annual), summary['end_nav']/summary['start_nav'])


def check_period(row, values):
    start, end = parse(row['start_snapshot_utc']), parse(row['end_snapshot_utc'])
    require(start < end and start in values and end in values, 'Missing period boundary')
    close(row['start_equity_usdt'], values[start])
    close(row['end_equity_usdt'], values[end])
    close(row['profit_usdt'], values[end] - values[start])
    close(row['return'], values[end] / values[start] - 1)
    close(row['max_daily_drawdown'], drawdown([value for day, value in sorted(values.items()) if start <= day <= end]))


def check_months(months, values):
    require(len({row['month'] for row in months}) == len(months), 'Duplicate month')
    for index, row in enumerate(months):
        check_period(row, values)
        start, end = parse(row['start_snapshot_utc']), parse(row['end_snapshot_utc'])
        boundary = (start.replace(day=1, hour=0, minute=0, second=0, microsecond=0) +
                    timedelta(days=32)).replace(day=1)
        require(row['month'] == start.strftime('%Y-%m'), 'Incorrect month label')
        require(row['in_progress'] == (end < boundary), 'Incorrect provisional status')
        require(row['partial_start'] == (start.day != 1 or any((start.hour, start.minute, start.second, start.microsecond))),
                'Incorrect partial-start status')
        require(end == min(boundary, max(values)), 'Incorrect month cutoff')
        if index:
            require(months[index-1]['end_snapshot_utc'] == row['start_snapshot_utc'], 'Non-contiguous months')
    close(math.prod(1+row['return'] for row in months), values[max(values)] / values[min(values)])


def check_paper():
    data = load('paper-monthly.json')
    rows = load('paper-equity-daily.json')['rows']
    require(data['type'] == 'paper_trading_snapshot_performance', 'Wrong paper data type')
    require(set(data['accounts']) == set(ALIASES), 'Invalid paper aliases')
    require(len({row['timestamp'] for row in rows}) == len(rows), 'Duplicate paper day')
    account_days = 0
    for alias in ALIASES:
        account = data['accounts'][alias]
        require(account['strategy'] == alias, 'Inconsistent strategy alias')
        values = {parse(row['timestamp']): row[alias] for row in rows if row[alias] is not None}
        require(len(values) == (max(values)-min(values)).days + 1, 'Missing paper daily values')
        account_days += len(values)
        values[parse(account['started_at_utc'])] = account['initial_capital_usdt']
        check_months(account['months'], values)
        check_period(account['since_inception'], values)
    portfolio = {parse(row['timestamp']): row['portfolio'] for row in rows if row['portfolio'] is not None}
    for row in rows:
        if row['portfolio'] is not None:
            require(all(row[alias] is not None for alias in ALIASES), 'Incomplete portfolio cohort')
            close(row['portfolio'], sum(row[alias] for alias in ALIASES))
    check_months(data['portfolio']['months'], portfolio)
    check_period(data['portfolio']['since_common_start'], portfolio)
    close(data['portfolio']['start_equity_usdt'], portfolio[min(portfolio)])
    require(data['portfolio']['common_start_snapshot_utc'] == min(portfolio).isoformat().replace('+00:00', 'Z'),
            'Incorrect portfolio common start')
    close(data['cash_ledger_reconciliation']['account_days_checked'], account_days)
    require(parse(data['snapshot_as_of_utc']) == max(portfolio), 'Incorrect data cutoff')
    print(f'PASS: {account_days} account-days, 6 anonymous accounts, {len(data["portfolio"]["months"])} portfolio months')


def check_regimes(data=None):
    data = load('paper-regimes.json') if data is None else data
    require(data['type'] == 'paper_trading_parameter_transition' and data['paper_only'] is True,
            'Wrong parameter-transition scope')
    require(data['target_new_entry_leverage'] == 5, 'Unexpected entry leverage')
    require(data['performance_history_rebased'] is False, 'Historical performance must not be rebased')
    require(data['margin_rule'] == 'preserve_original_margin_allocation', 'Unexpected margin policy')
    require(data['legacy_position_policy'] == 'keep_recorded_leverage_size_and_exit_logic',
            'Unexpected legacy-position policy')
    accounts = data['accounts']
    require(set(accounts) == set(ALIASES), 'Invalid transition aliases')
    start = parse(data['rollout_started_at_utc'])
    finish = parse(data['all_new_entries_verified_at_utc'])
    checked = parse(data['validation_as_of_utc'])
    require(start < finish <= checked, 'Invalid transition timeline')
    changed = []
    paper = load('paper-monthly.json')
    for alias, account in accounts.items():
        require(account['new_entry_leverage'] == 5, 'Unexpected account entry leverage')
        require(account['previous_new_entry_leverage'] in paper['accounts'][alias]['observed_trade_leverage'],
                'Previous leverage disagrees with published trade history')
        close(data['initial_capital_per_account_usdt'], paper['accounts'][alias]['initial_capital_usdt'])
        if account['changed']:
            require(account['previous_new_entry_leverage'] != 5, 'Invalid changed flag')
            began, verified = parse(account['process_started_at_utc']), parse(account['verified_at_utc'])
            require(start <= began < verified <= finish, 'Invalid account transition timeline')
            require(account['legacy_positions_at_cutover'] >= 0, 'Invalid legacy-position count')
            changed.append(account)
        else:
            require(account['previous_new_entry_leverage'] == 5 and account['process_started_at_utc'] is None,
                    'Unchanged account should already use target leverage')
            require(account['legacy_positions_at_cutover'] is None, 'Unchanged account has no cutover')
            require(parse(account['verified_at_utc']) <= checked, 'Invalid unchanged-account check time')
    require(min(parse(row['process_started_at_utc']) for row in changed) == start and
            max(parse(row['verified_at_utc']) for row in changed) == finish, 'Incorrect aggregate transition time')
    verification = data['verification']
    close(verification['changed_accounts'], len(changed))
    close(verification['healthy_paper_accounts'], len(ALIASES))
    close(verification['legacy_positions_preserved_in_changed_accounts'],
          sum(row['legacy_positions_at_cutover'] for row in changed))
    for count in verification.values():
        require(type(count) is int and count >= 0, 'Invalid verification count')
    print('PASS: anonymous parameter-transition timeline and account continuity metadata')


def main():
    check_files()
    check_backtest()
    check_paper()
    check_regimes()
    print('PASS: file allowlist, hashes, links, historical aggregates and paper-month arithmetic')
    print('This verifies published arithmetic, not independent trading authenticity.')


if __name__ == '__main__':
    main()
