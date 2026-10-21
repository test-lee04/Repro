"""실제 평가 기록을 분모가 명시된 JSON, CSV와 결과표로 정리한다."""

import argparse
import csv
import json
import statistics
from collections import Counter
from pathlib import Path

from app.testing.replay import write_json


def rate(numerator, denominator):
    """평가 시도가 없을 때 0퍼센트 대신 미측정을 표현한다."""
    return {'count': numerator, 'total': denominator,
            'percent': round(100 * numerator / denominator, 2) if denominator else None}


def summarize(manifest):
    """실패와 보류를 포함한 모든 실제 시도를 집계한다."""
    rows = manifest['rows']
    identities = [row['trial_id'] for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError('같은 평가 시도를 중복 집계할 수 없습니다.')
    binary = [row for row in rows if row['expected_status'] in {'reproduced', 'not_reproduced'}]
    buggy = [row for row in binary if row['expected_status'] == 'reproduced']
    fixed = [row for row in binary if row['expected_status'] == 'not_reproduced']
    unclear = [row for row in rows if row['expected_status'] == 'needs_information']
    verification = [row for row in rows if row.get('verification_attempted')]
    durations = [row['elapsed_seconds'] for row in rows]
    result = {
        'attempted': len(rows), 'planned': manifest['planned_trials'],
        'evaluation_status': manifest['status'],
        'exact_match': rate(sum(row['actual_status'] == row['expected_status'] for row in rows), len(rows)),
        'bug_detection': rate(sum(row['actual_status'] == 'reproduced' for row in buggy), len(buggy)),
        'normal_confirmation': rate(sum(row['actual_status'] == 'not_reproduced' for row in fixed), len(fixed)),
        'false_positive': rate(sum(row['actual_status'] == 'reproduced' for row in fixed), len(fixed)),
        'clarification': rate(sum(row['actual_status'] == 'needs_information' for row in unclear), len(unclear)),
        'resolved_binary': rate(sum(row['actual_status'] in {'reproduced', 'not_reproduced'} for row in binary), len(binary)),
        'test_generation': rate(sum(row['test_generated'] for row in binary), len(binary)),
        'fix_validation': rate(sum(row.get('test_status') == 'validated' for row in verification), len(verification)),
        'status_counts': dict(Counter(row['actual_status'] for row in rows)),
        'mean_seconds': round(statistics.mean(durations), 2) if durations else None,
        'median_seconds': round(statistics.median(durations), 2) if durations else None,
        'model_calls': sum(row['usage']['model_calls'] for row in rows),
        'model_errors': sum(row['usage']['model_errors'] for row in rows),
        'models': sorted({model for row in rows for model in row['usage'].get('models', [])}),
        'backends': sorted({backend for row in rows for backend in row['usage'].get('backends', [])}),
    }
    for key in ['input_tokens', 'output_tokens']:
        result[key + '_known_subtotal'] = sum(row['usage'][key + '_known_subtotal'] for row in rows)
        result[key + '_known_calls'] = sum(row['usage'][key + '_known_calls'] for row in rows)
        result[key] = result[key + '_known_subtotal'] if result[key + '_known_calls'] == result['model_calls'] else None
    result['by_case'] = {
        case: rate(sum(row['actual_status'] == row['expected_status'] for row in rows if row['case_id'] == case),
                   sum(row['case_id'] == case for row in rows))
        for case in sorted({row['case_id'] for row in rows})
    }
    return result


def format_rate(value):
    """분자, 분모와 비율을 함께 표시한다."""
    return f"{value['count']}/{value['total']} ({value['percent']}%)" if value['total'] else '미측정'


def safe_cell(value):
    """평가 결과가 Markdown 표의 구조를 바꾸지 않도록 표시한다."""
    return str(value).replace('|', '\\|').replace('\n', ' ')


def write_reports(folder):
    """평가 원본을 읽어 실제 측정값으로만 보고서를 저장한다."""
    folder = Path(folder)
    manifest = json.loads((folder / 'evaluation.json').read_text(encoding='utf-8'))
    summary = summarize(manifest)
    write_json(folder / 'summary.json', summary)
    fields = ['trial_id', 'case_id', 'variant', 'repeat', 'expected_status', 'actual_status',
              'elapsed_seconds', 'action_attempts', 'successful_actions', 'test_generated',
              'verification_attempted', 'verification_seconds', 'test_status', 'artifact_dir', 'error']
    with (folder / 'results.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=fields + ['model_calls', 'model_errors', 'input_tokens', 'output_tokens'])
        writer.writeheader()
        for row in manifest['rows']:
            data = {key: row.get(key) for key in fields}
            data.update({key: row['usage'].get(key) for key in ['model_calls', 'model_errors', 'input_tokens', 'output_tokens']})
            writer.writerow(data)
    lines = ['# Repro 평가 결과', '',
             f"평가 상태: {manifest['status']} / 실제 시도: {summary['attempted']} / 계획: {summary['planned']}",
             f"자료 버전: {manifest['dataset_version']}", f"자료 식별값: {manifest['dataset_fingerprint']}",
             f"앱 코드 식별값: {manifest.get('agent_code_fingerprint', '기록 없음')}", '',
             '오류·보류도 시도 수에 포함합니다. 이 결과는 아래 평가 자료에 한정됩니다.', '',
             '| 지표 | 실측 결과 |', '| --- | --- |']
    for key, label in [('exact_match', '정답 판정 일치'), ('bug_detection', '버그 환경 탐지'),
                       ('normal_confirmation', '정상 환경 미재현 확인'), ('false_positive', '정상 환경 오탐'),
                       ('resolved_binary', '버그·정상 판정 완료'), ('clarification', '정보 부족 질문 요청'),
                       ('test_generation', '회귀 테스트 생성'), ('fix_validation', '수정 검증 완료')]:
        lines.append(f'| {label} | {format_rate(summary[key])} |')
    lines += ['', f"평균 전체 소요 시간: {summary['mean_seconds']}초 / 중앙값: {summary['median_seconds']}초",
              f"실제 모델 호출: {summary['model_calls']}회 / 호출 오류: {summary['model_errors']}회",
              f"응답에서 확인한 모델: {', '.join(summary['models']) or '기록 없음'}", '']
    for key, label in [('input_tokens', '입력 토큰'), ('output_tokens', '출력 토큰')]:
        total = summary[key] if summary[key] is not None else '전체 합계 확인 불가'
        lines.append(f"{label}: {total} / 확인된 소계: {summary[key + '_known_subtotal']} / 사용량 제공 호출: {summary[key + '_known_calls']}/{summary['model_calls']}")
    lines += ['', '## 개별 결과', '', '| 사례 | 환경 | 회차 | 정답 | 실제 판정 | 전체 시간 | 모델 호출 |', '| --- | --- | --- | --- | --- | --- | --- |']
    for row in manifest['rows']:
        cells = [row['case_id'], row['variant'], row['repeat'], row['expected_status'],
                 row['actual_status'], row['elapsed_seconds'], row['usage']['model_calls']]
        lines.append('| ' + ' | '.join(safe_cell(cell) for cell in cells) + ' |')
    lines += ['', '## 해석 범위', '',
              '유한한 로컬 검사 자료의 결과이며 임의의 모든 웹사이트에 대한 성공률이 아닙니다.',
              '수정 검증은 선택 실행입니다. 실행하지 않은 경우 미측정으로 표시합니다.',
              '전체 시간에는 브라우저 준비와 정리를 포함하고 선택한 재실행 검증 시간은 별도로 기록합니다.',
              '수동 작업이나 다른 실행 방식과의 비교 측정은 아직 수행하지 않았습니다. 시간 절감률은 주장하지 않습니다.',
              '실패 원인과 전체 실행 기록은 evaluation.json과 각 artifact_dir에서 확인합니다.', '']
    (folder / 'summary.md').write_text('\n'.join(lines), encoding='utf-8')
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--folder', type=Path)
    group.add_argument('--latest', action='store_true')
    args = parser.parse_args()
    if args.latest:
        candidates = list(Path('artifacts').glob('evaluation-*/evaluation.json'))
        if not candidates:
            parser.error('저장된 실제 평가 기록이 없습니다.')
        folder = max(candidates, key=lambda path: path.stat().st_mtime).parent
    else:
        folder = args.folder
    print(json.dumps(write_reports(folder), ensure_ascii=False, indent=2))
    print('보고서 위치:', folder.resolve())
