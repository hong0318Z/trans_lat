"""
QSP/RAGS 게임 번역 도구 — 핵심 엔진

번역 흐름:
  1. FileChunker  — 파일을 N줄 단위로 분할
  2. extract_strings — 번역 대상 문자열만 추출 (코드/경로 제외)
  3. TranslatorClient.translate_batch — LLM에 번호 목록으로 전송
  4. merge_final / merge_debug — 번역문을 원본 줄에 병합
  5. StateManager — JSON 상태 파일로 재개 지원
"""

import json
import re
import os
import time
import threading
import hashlib
from datetime import datetime
from pathlib import Path

import httpx

# ── 상수 ───────────────────────────────────────────────────────────────────

APP_DIR = Path(__file__).parent.resolve()

GITHUB_COPILOT_ENDPOINT = "https://api.githubcopilot.com"
DEFAULT_MODEL = "claude-sonnet-4.5"
LINES_PER_CHUNK = 10
CONTEXT_TAIL_CHARS = 500

MODEL_CONFIGS = {
    "claude-sonnet-4.5": {"max_tokens": 64000, "label": "Claude Sonnet 4.5 (64K)"},
    "claude-haiku-4.5":  {"max_tokens": 32000, "label": "Claude Haiku 4.5 (32K)"},
    "gpt-5-mini":        {"max_tokens": 64000, "label": "GPT-5 Mini / Azure (64K)"},
}

_COPILOT_HEADERS = {
    "Editor-Version":         "vscode/1.95.0",
    "Editor-Plugin-Version":  "copilot-chat/0.22.0",
    "Copilot-Integration-Id": "vscode-chat",
    "Openai-Organization":    "github-copilot",
}

DEFAULT_SYSTEM_PROMPT = """\
당신은 비주얼 노벨/RPG 게임 한국어 현지화 번역가입니다.
영어 게임 텍스트를 자연스러운 한국어로 번역하세요.

규칙:
- 번호 순서와 개수를 정확히 유지하세요
- HTML 태그(<p>, <span>, <div> 등) 안의 텍스트는 번역하고 태그 구조는 보존하세요
- 러시아어가 섞인 경우 영어 부분만 번역하세요
- 게임 캐릭터의 말투와 감정을 살려 번역하세요
- <<$var>> 같은 변수 구문은 그대로 유지하세요
- 각 항목을 반드시 번호. "번역문" 형식으로만 출력하세요
- 설명이나 주석 없이 번역 목록만 출력하세요"""

# ── 문자열 추출 ────────────────────────────────────────────────────────────

# QSP 단일 인용 문자열: '' 는 이스케이프된 단일 따옴표
_QSP_STRING_RE = re.compile(r"'((?:[^']|'')*)'")

# 영어 단어 최소 3자 이상 포함 여부
_HAS_ENGLISH_RE = re.compile(r'[A-Za-z]{3,}')

# 번역 제외 패턴
_EXEC_RE = re.compile(r'\bexec\s*:', re.IGNORECASE)
_SRC_PATH_RE = re.compile(r'src\s*=\s*["\'](?:pic|vid|audio|img)/', re.IGNORECASE)

# 코드 전용 줄 시작 패턴 (번역 대상 없음)
_CODE_LINE_RE = re.compile(
    r'^\s*(?:'
    r'if\b|elseif\b|else\b|end\b|cls\b|'
    r"gs\s*'|gt\s*'|xgt\s*'|xgs\s*'|"
    r'gs\b|gt\b|xgt\b|xgs\b|'
    r'act\b|menu\b|exit\b|'
    r'killvar\b|addobj\b|delobj\b|'
    r'p\b|pl\b|mn\b|mp\b|no\b'
    r')',
    re.IGNORECASE
)


def _is_translatable_inner(unescaped: str) -> bool:
    """문자열 내용이 번역 대상인지 판별."""
    if _EXEC_RE.search(unescaped):
        return False
    if _SRC_PATH_RE.search(unescaped):
        return False
    # HTML 태그 제거 후 영어 단어 유무 확인
    text = re.sub(r'<[^>]+>', '', unescaped)
    return bool(_HAS_ENGLISH_RE.search(text))


def extract_strings(lines: list) -> list:
    """
    번역 대상 문자열 위치와 내용 추출.

    반환: list of (line_idx, match_start, match_end, raw_inner, unescaped_inner)
      raw_inner     — 파일 원본 내용 ('' 이스케이프 유지)
      unescaped_inner — LLM 전송용 ('' → ' 변환)
    """
    results = []
    for line_idx, line in enumerate(lines):
        if _CODE_LINE_RE.match(line):
            continue
        for m in _QSP_STRING_RE.finditer(line):
            raw_inner = m.group(1)
            unescaped = raw_inner.replace("''", "'")
            if _is_translatable_inner(unescaped):
                results.append((line_idx, m.start(), m.end(), raw_inner, unescaped))
    return results


def extract_lines_for_translation(lines: list) -> list:
    """
    Plain txt 모드: 비어있지 않은 각 줄 전체를 번역 단위로 추출.
    반환 형식은 extract_strings와 동일: (line_idx, 0, len, content, content)
    """
    results = []
    for line_idx, line in enumerate(lines):
        content = line.rstrip('\r\n')
        if content.strip():
            results.append((line_idx, 0, len(content), content, content))
    return results


# ── 문자열 병합 ────────────────────────────────────────────────────────────

def _escape_for_qsp(text: str) -> str:
    """번역문 내 단일 따옴표를 QSP 이스케이프 형식으로 변환."""
    return text.replace("'", "''")


def _apply_replacements(lines: list, by_line: dict) -> list:
    """
    by_line: {line_idx: [(start, end, new_inner), ...]}
    오른쪽에서 왼쪽으로 처리해 위치 이동 문제 방지.
    """
    result = list(lines)
    for li, ops in by_line.items():
        line = result[li]
        for start, end, new_inner in sorted(ops, key=lambda x: -x[0]):
            escaped = _escape_for_qsp(new_inner)
            line = line[:start] + "'" + escaped + "'" + line[end:]
        result[li] = line
    return result


def merge_final(lines: list, translations: dict, extractions: list) -> list:
    """
    완료 출력: 번역문으로만 교체.
    translations: {(line_idx, match_start): translated_unescaped_text}
    """
    by_line = {}
    for (li, start, end, raw, unesc) in extractions:
        key = (li, start)
        if key in translations and translations[key]:
            by_line.setdefault(li, []).append((start, end, translations[key]))
    return _apply_replacements(lines, by_line)


def merge_debug(lines: list, translations: dict, extractions: list) -> list:
    """
    디버그 출력: '원문 (번역문)' 형식.
    """
    by_line = {}
    for (li, start, end, raw, unesc) in extractions:
        key = (li, start)
        if key in translations and translations[key]:
            trans = translations[key]
            orig_text = re.sub(r'<[^>]+>', '', unesc).strip()
            combined = f"{trans} ({orig_text})" if orig_text else trans
            by_line.setdefault(li, []).append((start, end, combined))
    return _apply_replacements(lines, by_line)


def merge_final_plain(lines: list, translations: dict, extractions: list) -> list:
    """Plain txt: 각 줄 전체를 번역문으로 교체."""
    result = list(lines)
    for (li, start, end, raw, unesc) in extractions:
        key = (li, start)
        if key in translations and translations[key]:
            result[li] = translations[key]
    return result


def merge_debug_plain(lines: list, translations: dict, extractions: list) -> list:
    """Plain txt 디버그: '번역문  ←  원문' 형식."""
    result = list(lines)
    for (li, start, end, raw, unesc) in extractions:
        key = (li, start)
        if key in translations and translations[key]:
            result[li] = f"{translations[key]}  ←  {unesc}"
    return result


# ── LLM 응답 파싱 ──────────────────────────────────────────────────────────

def _parse_numbered_response(text: str, expected: int) -> list:
    """
    'N. "번역문"' 또는 'N. 번역문' 형식 파싱.
    expected 개수와 맞지 않으면 최선 결과 반환.
    """
    # 큰따옴표 있는 형식 우선
    items = re.findall(r'^\s*\d+\.\s*"(.*?)"', text, re.MULTILINE)
    if len(items) == expected:
        return items

    # 따옴표 없는 형식
    items2 = re.findall(r'^\s*\d+\.\s*(.+)', text, re.MULTILINE)
    items2 = [s.strip().strip('"').strip("'") for s in items2]
    if len(items2) == expected:
        return items2

    # 개수 불일치 시 더 많은 결과 사용, 부족하면 빈 문자열로 채움
    best = items if len(items) >= len(items2) else items2
    while len(best) < expected:
        best.append("")
    return best[:expected]


# ── LLM 클라이언트 ─────────────────────────────────────────────────────────

class TranslatorClient:
    """GitHub Copilot API를 통한 번역 클라이언트 (llm_api.py 패턴 재사용)."""

    def __init__(self, token: str,
                 model: str = DEFAULT_MODEL,
                 endpoint: str = GITHUB_COPILOT_ENDPOINT):
        self.model = model
        self._endpoint = endpoint.rstrip('/')
        self._max_tokens = MODEL_CONFIGS.get(model, {}).get("max_tokens", 64000)
        self._headers = {
            **_COPILOT_HEADERS,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _chat_tracked(self, messages: list, on_chunk=None) -> tuple:
        """SSE 스트리밍으로 LLM 호출. (content, tok_in, tok_out) 반환."""
        url = f"{self._endpoint}/chat/completions"
        payload = {
            "model":      self.model,
            "messages":   messages,
            "max_tokens": self._max_tokens,
            "stream":     True,
        }
        with httpx.Client(timeout=httpx.Timeout(
            connect=30, read=60, write=30, pool=10
        )) as client:
            chunks = []
            tok_in = tok_out = 0
            with client.stream('POST', url, json=payload,
                               headers=self._headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    if 'usage' in obj:
                        u = obj['usage']
                        tok_in  = u.get('prompt_tokens', tok_in)
                        tok_out = u.get('completion_tokens', tok_out)
                    delta = (obj.get('choices') or [{}])[0].get('delta', {})
                    piece = delta.get('content') or ''
                    if piece:
                        chunks.append(piece)
                        tok_out += 1
                        if on_chunk:
                            on_chunk(piece)
        return ''.join(chunks).strip(), tok_in, tok_out

    def test_connection(self) -> str:
        """API 연결 테스트 (간단한 질문으로 확인)."""
        content, _, _ = self._chat_tracked(
            [{"role": "user", "content": "2+2는 뭔가요? 한 줄로만 답해주세요."}]
        )
        return content

    def translate_batch(self, strings: list, last_context: str,
                        system_prompt: str, on_token=None) -> tuple:
        """
        문자열 목록을 번역.
        반환: (translated_list, prompt_tokens, completion_tokens)
        translated_list 길이 = strings 길이 (최선 보장).
        """
        numbered = "\n".join(f'{i+1}. "{s}"' for i, s in enumerate(strings))

        ctx_block = ""
        if last_context:
            ctx_block = (
                f"[이전 번역 컨텍스트 — 서사 연속성 유지용]:\n{last_context}\n\n"
            )

        user_msg = (
            f"{ctx_block}"
            f"아래 번호가 붙은 영어 문자열들을 한국어로 번역하세요.\n"
            f"번호와 따옴표 구조를 유지하고, 번역문만 바꿔주세요.\n"
            f"설명 없이 번역 목록만 출력하세요.\n\n"
            f"{numbered}"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_msg},
        ]

        content, tok_in, tok_out = self._chat_tracked(messages, on_chunk=on_token)
        translated = _parse_numbered_response(content, len(strings))
        return translated, tok_in, tok_out

    def retranslate(self, strings: list, context: str,
                    system_prompt: str, on_token=None) -> tuple:
        """선택 문자열 재번역 (translate_batch와 동일)."""
        return self.translate_batch(strings, context, system_prompt, on_token)


# ── 앱 설정 영속성 ────────────────────────────────────────────────────────

class AppConfig:
    """앱 전역 설정을 trans_config.json에 저장/복원."""

    CONFIG_PATH = APP_DIR / "trans_config.json"

    DEFAULTS: dict = {
        "llm_token":            "",
        "model":                DEFAULT_MODEL,
        "endpoint":             GITHUB_COPILOT_ENDPOINT,
        "lines_per_batch":      10,
        "max_consecutive_runs": 0,
        "system_prompt":        DEFAULT_SYSTEM_PROMPT,
        "glossary":             [],
        "last_project_name":    "",
    }

    def __init__(self):
        self._data: dict = dict(self.DEFAULTS)

    def load(self) -> dict:
        if self.CONFIG_PATH.exists():
            try:
                with open(self.CONFIG_PATH, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                for key, default in self.DEFAULTS.items():
                    self._data[key] = loaded.get(key, default)
            except Exception:
                self._data = dict(self.DEFAULTS)
        return dict(self._data)

    def save(self, data: dict):
        self._data.update(data)
        tmp = str(self.CONFIG_PATH) + ".tmp"
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.CONFIG_PATH)
        except Exception:
            pass

    def get(self, key: str):
        return self._data.get(key, self.DEFAULTS.get(key))


# ── 프로젝트 관리 ──────────────────────────────────────────────────────────

class ProjectManager:
    """trans_projects.json으로 명명된 프로젝트를 관리."""

    PROJECTS_PATH = APP_DIR / "trans_projects.json"
    PROJECTS_DIR  = APP_DIR / "projects"

    def __init__(self):
        self._projects: list = []
        self._ensure_dirs()

    def _ensure_dirs(self):
        self.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    def load(self):
        if self.PROJECTS_PATH.exists():
            try:
                with open(self.PROJECTS_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._projects = data.get("projects", [])
            except Exception:
                self._projects = []

    def _save(self):
        tmp = str(self.PROJECTS_PATH) + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({"version": 1, "projects": self._projects},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.PROJECTS_PATH)

    @staticmethod
    def _make_state_filename(name: str, input_path: str) -> str:
        sanitized = re.sub(r'[^\w가-힣]', '_', name)[:40]
        h = hashlib.md5((name + input_path).encode()).hexdigest()[:8]
        return f"{sanitized}_{h}.trans_state.json"

    def get_names(self) -> list:
        return [p["name"] for p in self._projects]

    def get_project(self, name: str) -> dict | None:
        for p in self._projects:
            if p["name"] == name:
                return p
        return None

    def create_project(self, name: str, input_path: str) -> dict:
        if self.get_project(name):
            raise ValueError(f"이미 존재하는 프로젝트 이름: {name}")
        now = datetime.now().isoformat(timespec='seconds')
        state_fname = self._make_state_filename(name, input_path)
        state_path  = str(self.PROJECTS_DIR / state_fname)
        proj = {
            "name":            name,
            "input_path":      str(input_path),
            "state_file_path": state_path,
            "created_at":      now,
            "last_modified":   now,
        }
        self._projects.append(proj)
        self._save()
        return proj

    def delete_project(self, name: str):
        proj = self.get_project(name)
        if not proj:
            return
        state = Path(proj["state_file_path"])
        if state.exists():
            state.unlink()
        self._projects = [p for p in self._projects if p["name"] != name]
        self._save()

    def touch_modified(self, name: str):
        proj = self.get_project(name)
        if proj:
            proj["last_modified"] = datetime.now().isoformat(timespec='seconds')
            self._save()


# ── 줄 단위 추출 번역기 (TXT 모드) ────────────────────────────────────────

_HTML_TAG_RE   = re.compile(r'<[^>]+>')
_HTML_OPEN_RE  = re.compile(r'^(<[^>]+>)+')   # 줄 앞쪽 연속 태그
_HTML_CLOSE_RE = re.compile(r'(</[^>]+>)+$')  # 줄 뒤쪽 연속 닫힘 태그


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub('', text).strip()


def _apply_with_tags(orig: str, translation: str) -> str:
    """원본 줄의 HTML 태그 구조를 보존하면서 텍스트만 번역으로 교체."""
    m_pre = _HTML_OPEN_RE.match(orig)
    prefix = m_pre.group(0) if m_pre else ''
    m_suf = _HTML_CLOSE_RE.search(orig)
    suffix = m_suf.group(0) if m_suf else ''
    if prefix or suffix:
        return f"{prefix}{translation}{suffix}"
    return translation


class LineExtractor:
    """
    TXT 파일에서 번역 대상 줄만 추출하여 번역 관리.
    HTML 태그는 표시/LLM 전송 시 제거, 저장 시 원본 태그 구조에 번역 적용.
    번역 결과는 메모리에 보관 → save_output() 호출 시 파일에 적용.
    """

    def __init__(self, input_path: str, state_path: str = None):
        self.input_path = str(input_path)
        if state_path:
            self._state_path = Path(state_path).with_suffix('.line.json')
        else:
            p = Path(input_path)
            self._state_path = p.parent / (p.stem + "_line.json")
        self.all_lines: list = []       # 파일 전체 줄 (원본 그대로)
        self.extracted: list = []       # [(orig_line_idx, clean_text), ...]
        self.translations: dict = {}    # orig_line_idx → translated_text

    def load(self):
        """파일 로드 + HTML 태그 제거 후 비어있지 않은 줄 추출."""
        text = ""
        for enc in ('utf-8', 'utf-8-sig', 'cp1252', 'latin-1'):
            try:
                text = Path(self.input_path).read_text(encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        self.all_lines = text.splitlines()
        result = []
        for i, line in enumerate(self.all_lines):
            clean = _strip_html(line)
            if clean:
                result.append((i, clean))
        self.extracted = result

    @property
    def total_extracted(self) -> int:
        return len(self.extracted)

    @property
    def translated_count(self) -> int:
        return len(self.translations)

    def get_batch(self, start: int, size: int) -> list:
        """extracted 배열에서 start부터 size개 반환."""
        return self.extracted[start:start + size]

    def set_translation(self, orig_idx: int, text: str):
        self.translations[orig_idx] = text

    def exists_state(self) -> bool:
        return self._state_path.exists()

    def load_state(self):
        with open(self._state_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.translations = {int(k): v for k, v in data.get("translations", {}).items()}

    def save_state(self):
        tmp = str(self._state_path) + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({
                "version": 1,
                "input_path": self.input_path,
                "extracted_count": len(self.extracted),
                "translations": {str(k): v for k, v in self.translations.items()},
            }, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._state_path)

    def save_output(self, output_path: str):
        """번역 적용 결과 파일 저장 (원본 HTML 태그 구조 보존)."""
        result = list(self.all_lines)
        for line_idx, translation in self.translations.items():
            orig = self.all_lines[line_idx]
            result[line_idx] = _apply_with_tags(orig, translation)
        Path(output_path).write_text('\n'.join(result), encoding='utf-8')


# ── 파일 청커 ──────────────────────────────────────────────────────────────

class FileChunker:
    """텍스트 파일을 줄 단위 청크로 분할."""

    def __init__(self, input_path: str, lines_per_chunk: int = LINES_PER_CHUNK):
        self._path = input_path
        self.lines_per_chunk = lines_per_chunk
        self._lines: list = []

    def load(self):
        """파일 읽기 (utf-8 → utf-8-sig → cp1252 → latin-1 순서)."""
        path = Path(self._path)
        for enc in ('utf-8', 'utf-8-sig', 'cp1252', 'latin-1'):
            try:
                self._lines = path.read_text(encoding=enc).splitlines()
                return
            except UnicodeDecodeError:
                continue
        raise ValueError(f"파일 인코딩을 감지할 수 없습니다: {self._path}")

    @property
    def total_lines(self) -> int:
        return len(self._lines)

    @property
    def total_chunks(self) -> int:
        if not self._lines:
            return 0
        return (len(self._lines) + self.lines_per_chunk - 1) // self.lines_per_chunk

    def get_chunk_lines(self, chunk_idx: int) -> list:
        start = chunk_idx * self.lines_per_chunk
        end = min(start + self.lines_per_chunk, len(self._lines))
        return self._lines[start:end]

    def build_chunk_map(self) -> list:
        chunks = []
        total = len(self._lines)
        for i in range(self.total_chunks):
            start = i * self.lines_per_chunk
            end = min(start + self.lines_per_chunk, total) - 1
            chunks.append({
                "chunk_idx":      i,
                "line_start":     start,
                "line_end":       end,
                "status":         "pending",
                "debug_byte_end": None,
                "final_byte_end": None,
            })
        return chunks


# ── 상태 관리 ──────────────────────────────────────────────────────────────

class StateManager:
    """JSON 상태 파일로 번역 진행 상태 저장/복원."""

    def __init__(self, input_path: str, state_path: str = None):
        if state_path:
            self._state_path = Path(state_path)
        else:
            p = Path(input_path)
            self._state_path = p.parent / (p.stem + ".trans_state.json")
        self.state: dict = {}

    def exists(self) -> bool:
        return self._state_path.exists()

    def load(self) -> dict:
        with open(self._state_path, 'r', encoding='utf-8') as f:
            self.state = json.load(f)
        return self.state

    def create(self, input_path: str, output_debug: str, output_final: str,
               total_lines: int, chunk_map: list, glossary: list) -> dict:
        self.state = {
            "version":           1,
            "input_path":        str(input_path),
            "output_debug_path": str(output_debug),
            "output_final_path": str(output_final),
            "total_lines":       total_lines,
            "total_chunks":      len(chunk_map),
            "chunks_done":       0,
            "last_context":      "",
            "glossary":          glossary,
            "chunk_map":         chunk_map,
        }
        return self.state

    def save(self):
        """원자적 쓰기 (tmp → replace)."""
        tmp = str(self._state_path) + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._state_path)

    def mark_chunk_done(self, chunk_idx: int,
                        debug_byte_end: int, final_byte_end: int,
                        last_context: str):
        cm = self.state["chunk_map"][chunk_idx]
        cm["status"]         = "done"
        cm["debug_byte_end"] = debug_byte_end
        cm["final_byte_end"] = final_byte_end
        self.state["chunks_done"] = sum(
            1 for c in self.state["chunk_map"] if c["status"] == "done"
        )
        self.state["last_context"] = last_context


# ── 파일 쓰기 유틸 ─────────────────────────────────────────────────────────

def _write_lines(path: str, lines: list, truncate: bool) -> int:
    """줄 목록을 파일에 쓰고 파일 끝 바이트 위치 반환."""
    content = '\n'.join(lines) + '\n'
    encoded = content.encode('utf-8')
    if truncate:
        with open(path, 'wb') as f:
            f.write(encoded)
            return f.tell()
    else:
        with open(path, 'ab') as f:
            f.write(encoded)
            return f.tell()


def _truncate_file(path: str, byte_offset: int):
    """파일을 지정 바이트 위치에서 잘라냄 (재개 시 부분 청크 제거)."""
    if Path(path).exists():
        with open(path, 'r+b') as f:
            f.truncate(byte_offset)


def _extract_context(text: str, max_chars: int = CONTEXT_TAIL_CHARS) -> str:
    """텍스트 끝 max_chars자를 완성된 줄 경계에서 추출."""
    if len(text) <= max_chars:
        return text
    tail = text[-(max_chars * 2):]
    lines = tail.splitlines()
    result = []
    total = 0
    for line in reversed(lines):
        total += len(line) + 1
        result.append(line)
        if total >= max_chars:
            break
    return '\n'.join(reversed(result))


# ── 번역 엔진 ──────────────────────────────────────────────────────────────

class TranslationEngine:
    """번역 실행 오케스트레이터. UI가 이 클래스를 통해 엔진을 제어."""

    def __init__(self):
        self._chunker:   FileChunker   = None
        self._state_mgr: StateManager  = None
        self._client:    TranslatorClient = None
        self._pause_event = threading.Event()
        self._stop_event  = threading.Event()
        self._running = False
        self._paused  = False

        # 현재 청크 데이터 (재번역 기능용)
        self.current_chunk_lines:  list = []
        self.current_extractions:  list = []
        self.current_translations: dict = {}
        self.current_last_context: str  = ""

    # ── 설정 ──────────────────────────────────────────────────
    def configure(self, input_path: str, output_debug: str, output_final: str,
                  token: str, model: str, system_prompt: str,
                  lines_per_chunk: int = LINES_PER_CHUNK,
                  max_chunks_per_run: int = 0,
                  glossary: list = None,
                  state_path: str = None,
                  plain_mode: bool = None):
        self._input_path        = input_path
        self._output_debug      = output_debug
        self._output_final      = output_final
        self._system_prompt     = system_prompt
        self._max_chunks_per_run = max_chunks_per_run
        self._glossary          = glossary or []
        if plain_mode is None:
            self._plain_mode = Path(input_path).suffix.lower() not in ('.txr', '.qsp')
        else:
            self._plain_mode = plain_mode
        self._chunker   = FileChunker(input_path, lines_per_chunk)
        self._state_mgr = StateManager(input_path, state_path)
        self._client    = TranslatorClient(token, model)

    # ── 상태 ──────────────────────────────────────────────────
    def load_or_create_state(self) -> dict:
        if self._state_mgr.exists():
            try:
                return self._state_mgr.load()
            except Exception:
                pass
        # 새 세션: 파일 로드 후 상태 생성
        self._chunker.load()
        chunk_map = self._chunker.build_chunk_map()
        p = Path(self._input_path)
        debug_path = self._output_debug or str(p.parent / (p.stem + "_debug.txr"))
        final_path = self._output_final or str(p.parent / (p.stem + "_kr.txr"))
        return self._state_mgr.create(
            self._input_path, debug_path, final_path,
            self._chunker.total_lines, chunk_map, self._glossary
        )

    def save_state(self):
        self._state_mgr.save()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    # ── 제어 ──────────────────────────────────────────────────
    def start(self, on_chunk_start, on_token, on_chunk_done,
              on_error, on_complete, on_log):
        self._stop_event.clear()
        self._pause_event.clear()
        self._running = True
        self._paused  = False
        threading.Thread(
            target=self._run_loop,
            args=(on_chunk_start, on_token, on_chunk_done,
                  on_error, on_complete, on_log),
            daemon=True,
        ).start()

    def pause(self):
        self._pause_event.set()
        self._paused = True

    def resume(self):
        self._pause_event.clear()
        self._paused = False

    def stop(self):
        self._stop_event.set()
        self._pause_event.clear()
        self._paused = False

    # ── 메인 루프 ──────────────────────────────────────────────
    def _run_loop(self, on_chunk_start, on_token, on_chunk_done,
                  on_error, on_complete, on_log):
        try:
            state     = self._state_mgr.state
            chunk_map = state["chunk_map"]
            debug_path = state["output_debug_path"]
            final_path = state["output_final_path"]
            last_context = state.get("last_context", "")

            if not self._chunker._lines:
                self._chunker.load()

            # 재개 시: 출력 파일을 마지막 성공 청크 위치로 truncate
            chunks_done_before = state.get("chunks_done", 0)
            if chunks_done_before > 0:
                last_done = chunk_map[chunks_done_before - 1]
                if last_done.get("debug_byte_end"):
                    _truncate_file(debug_path, last_done["debug_byte_end"])
                if last_done.get("final_byte_end"):
                    _truncate_file(final_path, last_done["final_byte_end"])

            run_start       = time.time()
            chunks_this_run = 0
            # 이번 세션에서 첫 번째 쓰기인지 (신규면 truncate, 재개면 append)
            first_write = (chunks_done_before == 0)

            for entry in chunk_map:
                if self._stop_event.is_set():
                    break
                if entry["status"] == "done":
                    continue

                # 일시정지 대기
                while self._pause_event.is_set():
                    if self._stop_event.is_set():
                        break
                    time.sleep(0.2)
                if self._stop_event.is_set():
                    break

                # 이번 실행 청크 수 제한
                if self._max_chunks_per_run > 0 and chunks_this_run >= self._max_chunks_per_run:
                    break

                chunk_idx  = entry["chunk_idx"]
                line_start = entry["line_start"]
                line_end   = entry["line_end"]
                lines      = self._chunker.get_chunk_lines(chunk_idx)
                if self._plain_mode:
                    extractions = extract_lines_for_translation(lines)
                else:
                    extractions = extract_strings(lines)

                on_chunk_start(chunk_idx, len(chunk_map),
                               line_start, line_end,
                               '\n'.join(lines), len(extractions))
                on_log(
                    f"[청크 {chunk_idx+1}/{len(chunk_map)}] "
                    f"줄 {line_start+1}~{line_end+1}, "
                    f"추출 문자열 {len(extractions)}개"
                )

                if not extractions:
                    # 번역 대상 없음 — 원본 그대로 출력
                    debug_lines = list(lines)
                    final_lines = list(lines)
                    translations = {}
                    tok_in = tok_out = 0
                else:
                    strings_to_translate = [unesc for (_, _, _, _, unesc) in extractions]
                    translated_list = None
                    tok_in = tok_out = 0

                    for attempt in range(3):
                        try:
                            translated_list, tok_in, tok_out = self._client.translate_batch(
                                strings_to_translate,
                                last_context,
                                self._system_prompt,
                                on_token=on_token,
                            )
                            break
                        except httpx.HTTPStatusError as e:
                            code = e.response.status_code
                            if code in (401, 403):
                                on_error(chunk_idx, f"API 인증 오류: {code}")
                                self._running = False
                                return
                            wait = [5, 15, 45][attempt]
                            on_log(f"[오류] HTTP {code}, {wait}초 후 재시도...")
                            time.sleep(wait)
                        except Exception as e:
                            wait = [5, 15, 45][attempt]
                            on_log(f"[오류] {e}, {wait}초 후 재시도...")
                            time.sleep(wait)

                    if translated_list is None:
                        on_error(chunk_idx, "번역 실패 (3회 재시도 모두 실패)")
                        entry["status"] = "error"
                        continue

                    if len(translated_list) != len(extractions):
                        on_log(
                            f"[경고] 번역 응답 수 불일치: "
                            f"요청 {len(extractions)}개, 수신 {len(translated_list)}개"
                        )

                    # 번역 딕셔너리 구성
                    translations = {}
                    for i, (li, start, end, raw, unesc) in enumerate(extractions):
                        if i < len(translated_list) and translated_list[i]:
                            translations[(li, start)] = translated_list[i]

                    on_log(
                        f"[청크 {chunk_idx+1}/{len(chunk_map)}] "
                        f"번역 완료, 토큰 입력 {tok_in} / 출력 {tok_out}"
                    )

                    if self._plain_mode:
                        debug_lines = merge_debug_plain(lines, translations, extractions)
                        final_lines = merge_final_plain(lines, translations, extractions)
                    else:
                        debug_lines = merge_debug(lines, translations, extractions)
                        final_lines = merge_final(lines, translations, extractions)

                # 출력 파일 쓰기
                debug_byte_end = _write_lines(debug_path, debug_lines, first_write)
                final_byte_end = _write_lines(final_path, final_lines, first_write)
                first_write = False

                # 컨텍스트 갱신
                last_context = _extract_context('\n'.join(final_lines))

                # 현재 청크 데이터 저장 (재번역 기능용)
                self.current_chunk_lines  = list(lines)
                self.current_extractions  = list(extractions)
                self.current_translations = dict(translations)
                self.current_last_context = last_context

                # 상태 업데이트 & 저장
                self._state_mgr.mark_chunk_done(
                    chunk_idx, debug_byte_end, final_byte_end, last_context
                )
                self._state_mgr.save()

                chunks_this_run += 1
                on_chunk_done(
                    chunk_idx, len(chunk_map),
                    chunks_this_run, self._max_chunks_per_run,
                    final_lines, debug_lines,
                    tok_in, tok_out,
                    last_context,
                )
                on_log(f"[청크 {chunk_idx+1}/{len(chunk_map)}] 완료. 상태 저장됨.")

            elapsed = time.time() - run_start
            all_done = state["chunks_done"] >= state["total_chunks"]
            on_complete(state["total_chunks"], state["chunks_done"], all_done)

        except Exception as e:
            try:
                on_log(f"[치명적 오류] {e}")
            except Exception:
                pass
        finally:
            self._running = False

    # ── 선택 항목 재번역 ───────────────────────────────────────
    def retranslate_selection(self, orig_strings: list, context: str,
                              system_prompt: str,
                              on_token=None, on_done=None):
        """선택 문자열만 다시 번역. 백그라운드 스레드에서 실행."""
        def _work():
            try:
                translated, tok_in, tok_out = self._client.retranslate(
                    orig_strings, context, system_prompt, on_token
                )
                if on_done:
                    on_done(orig_strings, translated, tok_in, tok_out)
            except Exception as e:
                if on_done:
                    on_done(orig_strings, [], 0, 0)
        threading.Thread(target=_work, daemon=True).start()
