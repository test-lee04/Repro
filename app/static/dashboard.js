/* 기존 API로 제보, 추가 답변, 결과 조회와 다운로드를 연결한다. */
'use strict';

const byId = (id) => document.getElementById(id);
const activeStates = new Set(['queued', 'running', 'cancelling']);
const statusNames = {
  queued: '실행 대기', running: '검사 중', cancelling: '취소 중',
  cancelled: '취소됨', completed: '검사 종료', needs_information: '추가 답변 필요',
  failed: '실행 실패', interrupted: '실행 중단',
};
const verdictNames = {
  reproduced: '제보 현상 재현', not_reproduced: '이번 실행에서 미재현',
  needs_information: '추가 정보 필요', inconclusive: '판정 보류',
  unsupported: '지원 범위 밖', environment_error: '실행 환경 오류', pending: '판정 대기',
};
const testNames = {
  not_generated: '미생성', generated: '생성 완료', validated: '수정 검증 완료',
  awaiting_fix_validation: '수정 검증 대기', environment_error: '실행 오류',
  unsupported: '생성 또는 실행 지원 범위 밖', replay_failed: '재실행 실패', bug_detected: '조건 실패 확인',
};
let epoch = 0;
let controller = new AbortController();
let timer = null;
let current = null;
let renderedQuestions = '';
let questionInputs = new Map();
let replayTimer = null;
let replayRequest = null;
let activeReplay = null;
let replayEpoch = 0;

function notice(message = '') {
  byId('notice').textContent = message;
  byId('notice').hidden = !message;
}

function errorText(error) {
  return error.name === 'AbortError' ? '' : error.message || '요청을 처리하지 못했습니다.';
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options, signal: controller.signal,
    headers: {'Content-Type': 'application/json', ...options.headers},
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map((item) => item.msg).join('\n')
      : `요청을 처리하지 못했습니다. 응답 코드: ${response.status}`;
    throw new Error(message);
  }
  return data;
}

function badge(id, text, tone = '') {
  const element = byId(id);
  element.textContent = text;
  element.dataset.tone = tone;
}

function saveRun(runId) {
  try { localStorage.setItem('repro-last-run', runId); } catch (_) { /* 저장이 제한돼도 조회는 계속한다. */ }
  const url = new URL(location.href);
  url.searchParams.set('run', runId);
  history.replaceState(null, '', url);
}

function linkButton(text, runId) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'secondary';
  button.textContent = text;
  button.addEventListener('click', () => selectRun(runId));
  return button;
}

function renderState(job) {
  current = job;
  byId('job-details').hidden = false;
  byId('current-id').value = job.run_id;
  byId('lookup-id').value = job.run_id;
  badge('job-status', statusNames[job.status] || job.status,
    activeStates.has(job.status) || job.status === 'needs_information' ? 'wait' : '');
  byId('submit-report').disabled = activeStates.has(job.status);
  byId('cancel-job').disabled = !['queued', 'running'].includes(job.status);
  const descriptions = {
    queued: '다른 작업이 끝나면 검사를 시작합니다.',
    running: '브라우저에서 제보를 확인하고 있습니다. 모델 응답에 시간이 걸릴 수 있습니다.',
    cancelling: '실행 취소와 브라우저 정리를 기다리고 있습니다.',
    completed: '실행이 끝났습니다. 아래에서 실제 재현 판정과 근거를 확인하세요.',
    needs_information: job.child_run_id ? '답변을 반영한 후속 작업이 있습니다. 다음 검사 회차를 확인하세요.' : '아래 질문에 답하면 같은 제보를 다시 검사합니다.',
    cancelled: '실행을 취소했습니다. 저장된 결과가 있으면 아래에 표시합니다.',
    interrupted: '서버 종료로 중단된 작업입니다. 새 제보로 다시 시작할 수 있습니다.',
    failed: '실행을 완료하지 못했습니다. 오류 내용을 확인하세요.',
  };
  byId('status-description').textContent = descriptions[job.status] || '작업 상태를 확인하세요.';
  const links = byId('run-links');
  links.replaceChildren();
  if (job.parent_run_id) links.append(linkButton('이전 검사 회차', job.parent_run_id));
  if (job.child_run_id) links.append(linkButton('다음 검사 회차', job.child_run_id));
  if (job.root_run_id && job.root_run_id !== job.run_id && job.root_run_id !== job.parent_run_id) {
    links.append(linkButton('최초 제보', job.root_run_id));
  }
  renderQuestions(job);
  if (job.error) notice(typeof job.error === 'string' ? job.error : job.error.message || JSON.stringify(job.error));
}

function renderQuestions(job) {
  const show = job.status === 'needs_information' && !job.child_run_id && job.questions?.length;
  byId('questions-card').hidden = !show;
  if (!show || renderedQuestions === job.run_id) return;
  renderedQuestions = job.run_id;
  byId('reparse-report').checked = false;
  questionInputs = new Map();
  const container = byId('questions');
  container.replaceChildren();
  for (const [index, question] of job.questions.entries()) {
    const label = document.createElement('label');
    const textarea = document.createElement('textarea');
    textarea.id = `answer-${index}`;
    textarea.required = true;
    textarea.maxLength = 10000;
    textarea.rows = 3;
    label.htmlFor = textarea.id;
    label.textContent = question.question;
    container.append(label, textarea);
    questionInputs.set(question.question_id, textarea);
  }
  byId('submit-answers').disabled = false;
}

function addLine(parent, label, value) {
  const line = document.createElement('p');
  line.textContent = `${label}: ${typeof value === 'string' ? JSON.stringify(value) : JSON.stringify(value ?? null)}`;
  parent.append(line);
}

function renderResult(result) {
  byId('result-card').hidden = false;
  const status = result.reproduction_status;
  badge('verdict', verdictNames[status] || status || '판정 대기',
    status === 'reproduced' ? 'bad' : status === 'not_reproduced' ? 'good' : 'wait');
  byId('verdict-reason').textContent = result.assessment?.reason || '최종 판정 기록이 없습니다.';
  const list = byId('comparison-list');
  list.replaceChildren();
  const comparisons = result.assessment?.comparisons || result.checks?.map((item) => item.comparison).filter(Boolean) || [];
  for (const comparison of comparisons) {
    const card = document.createElement('div');
    card.className = 'comparison';
    const title = document.createElement('h3');
    const condition = result.checks?.find((item) => item.condition?.condition_id === comparison.condition_id)?.condition;
    title.textContent = condition?.target_description || comparison.condition_id;
    card.append(title);
    addLine(card, '정상 기대값', comparison.expected_value);
    addLine(card, '실제 값', comparison.actual_value);
    const reason = document.createElement('p');
    reason.textContent = comparison.reason || comparison.status;
    card.append(reason);
    list.append(card);
  }
  const generation = result.test_generation || {};
  byId('replay-card').hidden = generation.generation_status !== 'completed' || !generation.case_path;
  byId('test-status').textContent = testNames[generation.test_status] || '테스트 상태 정보 없음';
  byId('test-reason').textContent = generation.reason || '이번 실행의 테스트 생성 기록이 없습니다.';
  const actions = byId('action-list');
  actions.replaceChildren();
  for (const item of result.execution?.history || []) {
    const line = document.createElement('li');
    const action = item.action || {};
    const name = {fill: '입력', click: '클릭', reload: '새로고침'}[action.kind] || action.kind || '행동';
    line.textContent = `${name} / ${action.element_id || ''}${action.value != null ? ` / ${JSON.stringify(action.value)}` : ''} / ${item.success === true ? '성공' : '미완료'}`;
    actions.append(line);
  }
  if (!actions.children.length) {
    const line = document.createElement('li');
    line.textContent = '이번 회차에서 기록된 브라우저 행동이 없습니다.';
    actions.append(line);
  }
  byId('raw-result').textContent = JSON.stringify(result, null, 2);
}

function renderFiles(data) {
  const list = byId('file-list');
  list.replaceChildren();
  for (const item of data.artifacts || []) {
    const line = document.createElement('li');
    const link = document.createElement('a');
    link.textContent = item.name;
    link.href = `/api/runs/${encodeURIComponent(current.run_id)}/artifacts/${encodeURIComponent(item.artifact_id)}`;
    link.download = item.name.split('/').pop();
    line.append(link);
    list.append(line);
  }
  byId('files-card').hidden = !list.children.length;
}

async function pollRun(runId, token) {
  try {
    const job = await api(`/api/runs/${encodeURIComponent(runId)}`);
    if (token !== epoch) return;
    renderState(job);
    if (activeStates.has(job.status)) {
      timer = setTimeout(() => pollRun(runId, token), 2000);
      return;
    }
    if (['failed', 'interrupted', 'cancelled'].includes(job.status)) {
      try {
        const result = await api(job.result_url);
        if (token === epoch) renderResult(result);
      } catch (error) {
        if (error.name === 'AbortError') return;
        if (!job.error && token === epoch) notice(errorText(error));
      }
    } else {
      const result = await api(job.result_url);
      if (token !== epoch) return;
      renderResult(result);
    }
    if (token === epoch && job.status === 'completed') await loadReplays(runId, token);
    try {
      const files = await api(job.artifacts_url);
      if (token === epoch) renderFiles(files);
    } catch (error) {
      if (!['failed', 'interrupted', 'cancelled'].includes(job.status)) throw error;
    }
  } catch (error) {
    if (token !== epoch || error.name === 'AbortError') return;
    notice(errorText(error) + '\n상태 새로고침으로 다시 조회할 수 있습니다.');
    byId('submit-report').disabled = false;
  }
}

function selectRun(runId) {
  replayEpoch += 1;
  clearTimeout(replayTimer);
  activeReplay = null;
  replayRequest = null;
  byId('submit-replay').disabled = false;
  byId('cancel-replay').disabled = true;
  epoch += 1;
  clearTimeout(timer);
  controller.abort();
  controller = new AbortController();
  current = {run_id: runId};
  renderedQuestions = '';
  notice();
  for (const id of ['result-card', 'files-card', 'questions-card', 'replay-card']) byId(id).hidden = true;
  byId('job-details').hidden = false;
  byId('current-id').value = runId;
  byId('cancel-job').disabled = true;
  badge('job-status', '조회 중', 'wait');
  saveRun(runId);
  pollRun(runId, epoch);
}

byId('report-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!byId('report').value.trim()) { notice('문제 상황을 입력하세요.'); return; }
  const token = epoch;
  byId('submit-report').disabled = true;
  notice();
  try {
    const target = {start_url: byId('target-url').value.trim()};
    if (byId('public-spec').value.trim()) target.public_spec = byId('public-spec').value.trim();
    const job = await api('/api/runs', {method: 'POST', body: JSON.stringify({report: byId('report').value.trim(), target})});
    if (token === epoch) selectRun(job.run_id);
  } catch (error) {
    if (token === epoch) { notice(errorText(error)); byId('submit-report').disabled = false; }
  }
});

byId('lookup-form').addEventListener('submit', (event) => {
  event.preventDefault();
  selectRun(byId('lookup-id').value.trim());
});
byId('refresh-job').addEventListener('click', () => { if (current) selectRun(current.run_id); });
byId('cancel-job').addEventListener('click', async () => {
  if (!current) return;
  const runId = current.run_id;
  const token = epoch;
  byId('cancel-job').disabled = true;
  try {
    await api(`/api/runs/${encodeURIComponent(runId)}/cancel`, {method: 'POST'});
    if (token === epoch) selectRun(runId);
  } catch (error) { if (token === epoch) { notice(errorText(error)); byId('cancel-job').disabled = false; } }
});
byId('answers-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!current) return;
  const token = epoch;
  const answers = {};
  for (const [id, input] of questionInputs) {
    if (!input.value.trim()) { notice('모든 질문에 답변을 입력하세요.'); return; }
    answers[id] = input.value.trim();
  }
  byId('submit-answers').disabled = true;
  notice();
  try {
    const job = await api(`/api/runs/${encodeURIComponent(current.run_id)}/answers`, {method: 'POST', body: JSON.stringify({answers, reparse_report: byId('reparse-report').checked})});
    if (token === epoch) selectRun(job.run_id);
  } catch (error) { if (token === epoch) { notice(errorText(error)); byId('submit-answers').disabled = false; } }
});

/* 재실행 접수 상태와 실제 테스트 결과를 별도로 표시한다. */
const replayNames = {passed: '조건 통과', assertion_failed: '조건 실패 확인', replay_error: '실행 오류'};

function renderReplays(records) {
  const list = byId('replay-history');
  list.replaceChildren();
  for (const record of records) {
    const line = document.createElement('li');
    const result = record.result?.replay;
    const role = record.request.purpose === 'original' ? '원래 환경' : '수정 환경';
    const outcome = result ? replayNames[result.status] || result.status : statusNames[record.status] || record.status;
    const error = record.error?.message || result?.error?.message;
    line.textContent = `${role} / ${outcome}\n${record.request.target_url}\n${record.created_at}${error ? `\n${error}` : ''}`;
    list.append(line);
  }
  if (!records.length) {
    const line = document.createElement('li');
    line.textContent = '화면에서 접수한 재실행 이력이 없습니다. 기존 명령 실행 기록은 결과 파일에서 확인할 수 있습니다.';
    list.append(line);
  }
}

async function loadReplays(runId, token) {
  const data = await api(`/api/runs/${encodeURIComponent(runId)}/replays`);
  if (token !== epoch) return;
  renderReplays(data.replays);
  const pending = data.replays.find((item) => activeStates.has(item.status));
  const last = data.replays[data.replays.length - 1];
  if (pending) {
    activeReplay = pending;
    byId('submit-replay').disabled = true;
    byId('cancel-replay').disabled = pending.status === 'cancelling';
    badge('replay-state', statusNames[pending.status], 'wait');
    clearTimeout(replayTimer);
    const replayToken = ++replayEpoch;
    replayTimer = setTimeout(() => watchReplay(runId, pending.replay_id, token, replayToken), 1000);
  } else {
    activeReplay = null;
    byId('submit-replay').disabled = false;
    byId('cancel-replay').disabled = true;
    const outcome = last?.result?.replay;
    badge('replay-state', outcome ? replayNames[outcome.status] || outcome.status
      : last ? statusNames[last.status] || last.status : '실행 전', outcome?.status === 'passed' ? 'good' : '');
  }
}

async function watchReplay(runId, replayId, token, replayToken) {
  try {
    const record = await api(`/api/runs/${encodeURIComponent(runId)}/replays/${encodeURIComponent(replayId)}`);
    if (token !== epoch || replayToken !== replayEpoch) return;
    activeReplay = record;
    if (activeStates.has(record.status)) {
      badge('replay-state', statusNames[record.status], 'wait');
      byId('cancel-replay').disabled = record.status === 'cancelling';
      replayTimer = setTimeout(() => watchReplay(runId, replayId, token, replayToken), 1000);
      return;
    }
    const result = await api(current.result_url);
    if (token !== epoch || replayToken !== replayEpoch) return;
    renderResult(result);
    await loadReplays(runId, token);
    const files = await api(current.artifacts_url);
    if (token === epoch) renderFiles(files);
    if (record.error && token === epoch) notice(record.error.message || JSON.stringify(record.error));
  } catch (error) {
    if (token !== epoch || replayToken !== replayEpoch || error.name === 'AbortError') return;
    notice(errorText(error) + '\n상태 새로고침으로 재실행 이력을 다시 조회하세요.');
    byId('submit-replay').disabled = false;
  }
}

byId('replay-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!current) return;
  const token = epoch;
  const runId = current.run_id;
  const targetUrl = byId('replay-url').value.trim();
  const purpose = byId('replay-purpose').value;
  if (!replayRequest || replayRequest.target_url !== targetUrl || replayRequest.purpose !== purpose) {
    replayRequest = {target_url: targetUrl, purpose, request_id: crypto.randomUUID().replaceAll('-', '')};
  }
  byId('submit-replay').disabled = true;
  notice();
  try {
    const record = await api(`/api/runs/${encodeURIComponent(runId)}/replays`, {
      method: 'POST', body: JSON.stringify(replayRequest),
    });
    if (token !== epoch) return;
    replayRequest = null;
    await loadReplays(runId, token);
    if (!activeStates.has(record.status)) {
      const result = await api(current.result_url);
      if (token === epoch) renderResult(result);
      const files = await api(current.artifacts_url);
      if (token === epoch) renderFiles(files);
    }
  } catch (error) {
    if (token === epoch) { notice(errorText(error)); byId('submit-replay').disabled = false; }
  }
});
byId('cancel-replay').addEventListener('click', async () => {
  if (!current || !activeReplay) return;
  const token = epoch;
  const runId = current.run_id;
  const replayId = activeReplay.replay_id;
  byId('cancel-replay').disabled = true;
  try {
    await api(`/api/runs/${encodeURIComponent(runId)}/replays/${encodeURIComponent(replayId)}/cancel`, {method: 'POST'});
    if (token === epoch) await loadReplays(runId, token);
  } catch (error) { if (token === epoch) notice(errorText(error)); }
});

let remembered = new URL(location.href).searchParams.get('run');
if (!remembered) { try { remembered = localStorage.getItem('repro-last-run'); } catch (_) { /* 저장 제한을 무시한다. */ } }
if (remembered && /^[a-f0-9]{32}$/.test(remembered)) selectRun(remembered);
