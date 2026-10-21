"""평가에만 사용하는 서로 다른 웹서비스 화면과 정답을 제공한다."""

import hashlib
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

FIXTURE_VERSION = 'r8-1-v1'


@dataclass(frozen=True)
class EvaluationCase:
    """Agent에게 전달할 제보와 평가기가 보관할 정답 화면을 분리한다."""

    case_id: str
    report: str
    buggy_html: str
    fixed_html: str
    needs_information: bool = False


STYLE = '<style>body{font:18px system-ui;padding:30px}label,input,button,output{display:block;margin:12px 0}output,ul{min-height:30px}</style>'


def document(body):
    """한국어 검사 화면의 공통 외곽을 만든다."""
    return '<!doctype html><html lang="ko"><meta charset="utf-8">' + STYLE + '<body>' + body + '</body></html>'


def note_page(fixed):
    """저장 후 새로고침할 때 입력값이 유지되는지 검사할 화면이다."""
    save = "localStorage.setItem('note_title', title.value);" if fixed else ''
    return document('''<label>문서 제목<input id="title"></label>
<button onclick="saveTitle()">저장</button><output aria-label="저장 처리 결과" id="message">대기 중</output>
<script>const title=document.getElementById('title');
title.value=localStorage.getItem('note_title')||'';
function saveTitle(){SAVE_SCRIPT document.getElementById('message').textContent='저장했습니다.';}</script>'''.replace('SAVE_SCRIPT', save))


def search_page(fixed):
    """검색 결과 목록의 표시 문구를 검사할 화면이다."""
    result = "['팀 회의록', '업무 일정'].filter(item=>item.includes(query.value)).forEach(item=>{const li=document.createElement('li');li.textContent=item;results.append(li);});" if fixed else ''
    return document('''<label>검색어<input id="query"></label><button onclick="search()">검색</button>
<ul aria-label="검색 결과 목록" id="results"><li>검색 전입니다.</li></ul>
<script>const query=document.getElementById('query');const results=document.getElementById('results');
function search(){results.replaceChildren();RESULT_SCRIPT if(!results.children.length){const li=document.createElement('li');li.textContent='검색 결과 없음';results.append(li);}}</script>'''.replace('RESULT_SCRIPT', result))


def validation_page(fixed):
    """빈 이메일로 가입을 요청했을 때 안내 문구를 검사할 화면이다."""
    result = "email.value.trim() ? '가입 완료' : '이메일을 입력해 주세요.'" if fixed else "'가입 완료'"
    return document('''<label>이메일<input id="email"></label><button onclick="join()">가입</button>
<output aria-label="가입 처리 결과" id="result">대기 중</output>
<script>const email=document.getElementById('email');
function join(){document.getElementById('result').textContent=RESULT_SCRIPT;}</script>'''.replace('RESULT_SCRIPT', result))


CASES = [
    EvaluationCase('note',
        '문서 제목에 주간 계획을 입력하고 저장 버튼을 누른 뒤 새로고침하면 제목이 비어 있습니다. '
        '저장한 제목은 새로고침 후에도 주간 계획으로 유지돼야 합니다.', note_page(False), note_page(True)),
    EvaluationCase('search',
        '검색어 입력란에 회의록을 입력하고 검색 버튼을 누르면 검색 결과 없음이라고 나옵니다. '
        '검색 결과 목록에는 회의록이라는 문구가 포함돼야 합니다.', search_page(False), search_page(True)),
    EvaluationCase('validation',
        '이메일 입력란이 비어 있는 초기 화면에서 가입 버튼을 누르면 가입 완료라고 나옵니다. '
        '이메일이 비어 있으면 가입 처리 결과에 이메일을 입력해 주세요.라는 문구가 표시돼야 합니다.',
        validation_page(False), validation_page(True)),
    EvaluationCase('unclear', '저장 기능이 이상합니다.', note_page(False), note_page(True), True),
]


def dataset_fingerprint():
    """제보와 검사 화면이 같은 평가 자료인지 확인할 식별값을 만든다."""
    data = [vars(case) for case in CASES]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class FixtureServer:
    """평가 정답이 드러나지 않는 임의 경로로 로컬 화면을 제공한다."""

    def __enter__(self):
        """모든 검사 화면을 등록하고 서버를 시작한다."""
        self.urls = {}
        pages = {}
        for case in CASES:
            for variant, html in [('buggy', case.buggy_html), ('fixed', case.fixed_html)]:
                path = '/' + uuid4().hex
                self.urls[(case.case_id, variant)] = path
                pages[path] = html.encode('utf-8')

        class Handler(BaseHTTPRequestHandler):
            """요청 경로에 해당하는 화면만 반환한다."""

            def do_GET(self):
                """검사 자료와 정답을 목록으로 노출하지 않고 HTML을 제공한다."""
                body = pages.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                """평가 결과와 무관한 서버 요청 로그를 생략한다."""
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
        self.thread.start()
        return self

    def url(self, case_id, variant):
        """평가기가 고른 화면 주소만 반환한다."""
        return f'http://127.0.0.1:{self.server.server_port}' + self.urls[(case_id, variant)]

    def __exit__(self, *args):
        """로컬 서버를 종료하고 소켓을 정리한다."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
