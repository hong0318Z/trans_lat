"""
QSP/RAGS 게임 번역 도구 — tkinter UI
"""
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import time

from trans_core import (
    TranslationEngine, StateManager, MODEL_CONFIGS, DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT, LINES_PER_CHUNK, TranslatorClient,
)

# ── 다크 테마 ──────────────────────────────────────────────────────────────
THEME = {
    "bg":        "#1e1e2e",
    "panel":     "#181825",
    "entry_bg":  "#313244",
    "fg":        "#cdd6f4",
    "fg2":       "#a6adc8",
    "accent":    "#89b4fa",
    "ok":        "#a6e3a1",
    "err":       "#f38ba8",
    "warn":      "#fab387",
    "btn":       "#45475a",
    "btn_fg":    "#cdd6f4",
    "sel":       "#585b70",
    "border":    "#45475a",
}


class TranslationApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("QSP/RAGS 게임 번역기")
        self.geometry("1280x900")
        self.configure(bg=THEME["bg"])
        self._engine = TranslationEngine()
        self._current_extractions = []
        self._current_orig_lines = []
        self._current_chunk_idx = -1
        self._current_context = ""
        self._glossary_rows = []  # list of (frame, src_var, tgt_var)
        self._settings_visible = True
        self._syncing_scroll = False
        self._build_style()
        self._build_ui()

    # ── 스타일 ──────────────────────────────────────────────────
    def _build_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        bg, fg, ebg = THEME["bg"], THEME["fg"], THEME["entry_bg"]
        btn, sel = THEME["btn"], THEME["sel"]
        s.configure(".", background=bg, foreground=fg, fieldbackground=ebg,
                    troughcolor=THEME["panel"], borderwidth=0, relief="flat")
        s.configure("TFrame", background=bg)
        s.configure("TLabel", background=bg, foreground=fg)
        s.configure("TLabelframe", background=bg, foreground=THEME["accent"], borderwidth=1)
        s.configure("TLabelframe.Label", background=bg, foreground=THEME["accent"])
        s.configure("TButton", background=btn, foreground=fg, relief="flat", padding=4)
        s.map("TButton", background=[("active", sel)])
        s.configure("TEntry", fieldbackground=ebg, foreground=fg, insertcolor=fg)
        s.configure("TCombobox", fieldbackground=ebg, foreground=fg, selectbackground=sel)
        s.map("TCombobox", fieldbackground=[("readonly", ebg)])
        s.configure("TSpinbox", fieldbackground=ebg, foreground=fg)
        s.configure("Horizontal.TProgressbar", troughcolor=THEME["panel"],
                    background=THEME["accent"])

    # ── UI 빌드 ─────────────────────────────────────────────────
    def _build_ui(self):
        # 최상단 토글 버튼
        top = tk.Frame(self, bg=THEME["bg"])
        top.pack(fill="x", padx=4, pady=2)
        self._toggle_btn = tk.Button(top, text="▼ 설정 접기", bg=THEME["btn"],
                                     fg=THEME["fg"], relief="flat", bd=0,
                                     command=self._toggle_settings)
        self._toggle_btn.pack(side="left")

        # 설정 패널
        self._settings_frame = ttk.LabelFrame(self, text="설정", padding=6)
        self._settings_frame.pack(fill="x", padx=6, pady=2)
        self._build_settings(self._settings_frame)

        # 컨트롤 바
        ctrl = tk.Frame(self, bg=THEME["panel"])
        ctrl.pack(fill="x", padx=6, pady=2)
        self._build_controls(ctrl)

        # 메인 영역 (PanedWindow)
        self._main_pane = tk.PanedWindow(self, orient="vertical",
                                         bg=THEME["bg"], sashwidth=5,
                                         sashrelief="flat")
        self._main_pane.pack(fill="both", expand=True, padx=6, pady=2)

        # 편집 영역 (좌우)
        self._hpane = tk.PanedWindow(self._main_pane, orient="horizontal",
                                     bg=THEME["bg"], sashwidth=5,
                                     sashrelief="flat")
        self._build_edit_pane(self._hpane)
        self._main_pane.add(self._hpane, minsize=200)

        # 로그 패널
        log_frame = ttk.LabelFrame(self._main_pane, text="로그", padding=4)
        self._build_log(log_frame)
        self._main_pane.add(log_frame, minsize=80)

    def _build_settings(self, parent):
        # 파일 경로
        file_fr = ttk.Frame(parent)
        file_fr.pack(fill="x", pady=2)

        self._input_var = tk.StringVar()
        self._debug_var = tk.StringVar()
        self._final_var = tk.StringVar()

        self._make_file_row(file_fr, "입력 파일:", self._input_var,
                            self._browse_input, 0)
        self._make_file_row(file_fr, "디버그 출력:", self._debug_var,
                            lambda: self._browse_out(self._debug_var, "_debug.txr"), 1)
        self._make_file_row(file_fr, "완료 출력:", self._final_var,
                            lambda: self._browse_out(self._final_var, "_kr.txr"), 2)

        # 토큰 + 연결 테스트
        tok_fr = ttk.Frame(parent)
        tok_fr.pack(fill="x", pady=2)
        ttk.Label(tok_fr, text="API 토큰:").pack(side="left")
        self._token_var = tk.StringVar()
        self._token_entry = ttk.Entry(tok_fr, textvariable=self._token_var,
                                      show="*", width=40)
        self._token_entry.pack(side="left", padx=4)
        self._show_tok = tk.BooleanVar(value=False)
        ttk.Checkbutton(tok_fr, text="표시", variable=self._show_tok,
                        command=self._toggle_token_vis).pack(side="left")
        ttk.Button(tok_fr, text="연결 테스트",
                   command=self._on_test_connection).pack(side="left", padx=8)

        # 모델 + 청크 설정
        opt_fr = ttk.Frame(parent)
        opt_fr.pack(fill="x", pady=2)
        ttk.Label(opt_fr, text="모델:").pack(side="left")
        self._model_var = tk.StringVar(value=DEFAULT_MODEL)
        model_labels = [v["label"] for v in MODEL_CONFIGS.values()]
        self._model_keys = list(MODEL_CONFIGS.keys())
        self._model_combo = ttk.Combobox(opt_fr, values=model_labels,
                                         width=28, state="readonly")
        self._model_combo.current(0)
        self._model_combo.pack(side="left", padx=4)
        self._model_combo.bind("<<ComboboxSelected>>", self._on_model_change)

        ttk.Label(opt_fr, text="  이번 실행 청크 수 (0=끝까지):").pack(side="left")
        self._chunks_run_var = tk.StringVar(value="10")
        ttk.Spinbox(opt_fr, from_=0, to=9999, textvariable=self._chunks_run_var,
                    width=6).pack(side="left", padx=4)

        ttk.Label(opt_fr, text="  청크 줄수:").pack(side="left")
        self._lpc_var = tk.StringVar(value=str(LINES_PER_CHUNK))
        ttk.Spinbox(opt_fr, from_=500, to=10000, textvariable=self._lpc_var,
                    width=7).pack(side="left", padx=4)

        # 시스템 프롬프트
        prm_fr = ttk.LabelFrame(parent, text="시스템 프롬프트", padding=4)
        prm_fr.pack(fill="x", pady=4)
        self._prompt_text = tk.Text(prm_fr, height=5, bg=THEME["entry_bg"],
                                    fg=THEME["fg"], insertbackground=THEME["fg"],
                                    relief="flat", wrap="word")
        self._prompt_text.pack(fill="x")
        self._prompt_text.insert("1.0", DEFAULT_SYSTEM_PROMPT)
        ttk.Button(prm_fr, text="기본값으로 초기화",
                   command=lambda: (self._prompt_text.delete("1.0", "end"),
                                    self._prompt_text.insert("1.0", DEFAULT_SYSTEM_PROMPT))
                   ).pack(anchor="e", pady=2)

        # 단어집
        self._glossary_frame = ttk.LabelFrame(parent, text="단어집", padding=4)
        self._glossary_frame.pack(fill="x", pady=4)
        self._glossary_list_frame = tk.Frame(self._glossary_frame, bg=THEME["bg"])
        self._glossary_list_frame.pack(fill="x")
        ttk.Button(self._glossary_frame, text="+ 항목 추가",
                   command=self._add_glossary_row).pack(anchor="w", pady=2)

    def _make_file_row(self, parent, label, var, cmd, row):
        ttk.Label(parent, text=label, width=10).grid(
            row=row, column=0, sticky="w", pady=1)
        ttk.Entry(parent, textvariable=var, width=60).grid(
            row=row, column=1, sticky="ew", padx=4)
        ttk.Button(parent, text="찾기", command=cmd).grid(
            row=row, column=2, padx=2)
        parent.columnconfigure(1, weight=1)

    def _build_controls(self, parent):
        self._btn_start  = tk.Button(parent, text="▶ 시작", bg="#a6e3a1",
                                     fg="#1e1e2e", relief="flat", bd=0,
                                     command=self._on_start, padx=8)
        self._btn_pause  = tk.Button(parent, text="⏸ 일시정지", bg=THEME["btn"],
                                     fg=THEME["fg"], relief="flat", bd=0,
                                     command=self._on_pause, padx=8, state="disabled")
        self._btn_resume = tk.Button(parent, text="▶ 재개", bg=THEME["btn"],
                                     fg=THEME["fg"], relief="flat", bd=0,
                                     command=self._on_resume, padx=8, state="disabled")
        self._btn_stop   = tk.Button(parent, text="⏹ 중지", bg=THEME["err"],
                                     fg="#1e1e2e", relief="flat", bd=0,
                                     command=self._on_stop, padx=8, state="disabled")
        self._btn_retrans = tk.Button(parent, text="🔄 선택 재번역", bg=THEME["warn"],
                                      fg="#1e1e2e", relief="flat", bd=0,
                                      command=self._on_retranslate_selection, padx=8)

        for b in (self._btn_start, self._btn_pause, self._btn_resume,
                  self._btn_stop, self._btn_retrans):
            b.pack(side="left", padx=3, pady=3)

        # 진행률
        self._chunk_label = tk.Label(parent, text="청크: -/-",
                                     bg=THEME["panel"], fg=THEME["fg2"])
        self._chunk_label.pack(side="left", padx=12)
        self._prog_var = tk.DoubleVar(value=0)
        self._prog_bar = ttk.Progressbar(parent, variable=self._prog_var,
                                         maximum=100, length=300,
                                         style="Horizontal.TProgressbar")
        self._prog_bar.pack(side="left", padx=4)
        self._pct_label = tk.Label(parent, text="0.0%",
                                   bg=THEME["panel"], fg=THEME["fg2"])
        self._pct_label.pack(side="left", padx=4)

    def _build_edit_pane(self, parent):
        # 원본 (좌)
        left = ttk.LabelFrame(parent, text="원본 (현재 청크)", padding=4)
        self._orig_text = tk.Text(left, bg=THEME["entry_bg"], fg=THEME["fg"],
                                  insertbackground=THEME["fg"], relief="flat",
                                  wrap="none", state="disabled")
        orig_sby = ttk.Scrollbar(left, orient="vertical",
                                 command=self._orig_text.yview)
        self._orig_text.configure(yscrollcommand=lambda *a: self._on_orig_scroll(*a))
        orig_sby.pack(side="right", fill="y")
        self._orig_text.pack(fill="both", expand=True)
        self._orig_sby = orig_sby
        parent.add(left, minsize=300)

        # 번역 (우)
        right = ttk.LabelFrame(parent, text="번역 결과 (편집 가능)", padding=4)
        self._trans_text = tk.Text(right, bg=THEME["entry_bg"], fg=THEME["fg"],
                                   insertbackground=THEME["fg"], relief="flat",
                                   wrap="none")
        trans_sby = ttk.Scrollbar(right, orient="vertical",
                                  command=self._trans_text.yview)
        self._trans_text.configure(yscrollcommand=lambda *a: self._on_trans_scroll(*a))
        trans_sby.pack(side="right", fill="y")
        self._trans_text.pack(fill="both", expand=True)
        self._trans_sby = trans_sby
        parent.add(right, minsize=300)

    def _build_log(self, parent):
        self._log_text = tk.Text(parent, height=8, bg=THEME["panel"],
                                 fg=THEME["fg2"], insertbackground=THEME["fg"],
                                 relief="flat", wrap="word", state="disabled")
        log_sb = ttk.Scrollbar(parent, orient="vertical",
                               command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self._log_text.pack(fill="both", expand=True)
        self._log_text.tag_configure("ok",   foreground=THEME["ok"])
        self._log_text.tag_configure("err",  foreground=THEME["err"])
        self._log_text.tag_configure("warn", foreground=THEME["warn"])
        self._log_text.tag_configure("info", foreground=THEME["accent"])

    # ── 단어집 ──────────────────────────────────────────────────
    def _add_glossary_row(self, src="", tgt=""):
        row_fr = tk.Frame(self._glossary_list_frame, bg=THEME["bg"])
        row_fr.pack(fill="x", pady=1)
        src_var = tk.StringVar(value=src)
        tgt_var = tk.StringVar(value=tgt)
        ttk.Entry(row_fr, textvariable=src_var, width=20).pack(side="left", padx=2)
        tk.Label(row_fr, text="→", bg=THEME["bg"], fg=THEME["fg2"]).pack(side="left")
        ttk.Entry(row_fr, textvariable=tgt_var, width=20).pack(side="left", padx=2)

        def _del(fr=row_fr, row=(row_fr, src_var, tgt_var)):
            fr.destroy()
            if row in self._glossary_rows:
                self._glossary_rows.remove(row)

        ttk.Button(row_fr, text="✕", width=3, command=_del).pack(side="left", padx=2)
        self._glossary_rows.append((row_fr, src_var, tgt_var))

    def _build_glossary_prompt(self) -> str:
        base = self._prompt_text.get("1.0", "end").strip()
        glossary = [(s.get().strip(), t.get().strip())
                    for _, s, t in self._glossary_rows
                    if s.get().strip() and t.get().strip()]
        if glossary:
            lines = ["", "", "용어집 (반드시 아래 단어는 지정된 번역을 사용하세요):"]
            for src, tgt in glossary:
                lines.append(f"  - {src} → {tgt}")
            base += "\n".join(lines)
        return base

    # ── 설정 토글 ───────────────────────────────────────────────
    def _toggle_settings(self):
        if self._settings_visible:
            self._settings_frame.pack_forget()
            self._toggle_btn.configure(text="▶ 설정 펼치기")
            self._settings_visible = False
        else:
            self._settings_frame.pack(fill="x", padx=6, pady=2,
                                      before=self._main_pane)
            self._toggle_btn.configure(text="▼ 설정 접기")
            self._settings_visible = True

    def _toggle_token_vis(self):
        self._token_entry.configure(show="" if self._show_tok.get() else "*")

    # ── 파일 찾기 ───────────────────────────────────────────────
    def _browse_input(self):
        p = filedialog.askopenfilename(
            filetypes=[("TXR files", "*.txr"), ("All files", "*.*")])
        if not p:
            return
        self._input_var.set(p)
        stem = Path(p).stem
        parent = Path(p).parent
        if not self._debug_var.get():
            self._debug_var.set(str(parent / (stem + "_debug.txr")))
        if not self._final_var.get():
            self._final_var.set(str(parent / (stem + "_kr.txr")))
        # 상태 파일 탐지
        sm = StateManager(p)
        if sm.exists():
            try:
                state = sm.load()
                done = sum(1 for c in state["chunk_map"] if c["status"] == "done")
                total = state["total_chunks"]
                self._append_log(
                    f"[재개 가능] 이전 상태 발견: {done}/{total} 청크 완료. "
                    "시작 버튼을 누르면 이어서 진행합니다.", "warn")
                pct = done / total * 100 if total else 0
                self._prog_var.set(pct)
                self._pct_label.configure(text=f"{pct:.1f}%")
                self._chunk_label.configure(text=f"청크: {done}/{total}")
            except Exception:
                pass

    def _browse_out(self, var, suffix):
        p = filedialog.asksaveasfilename(
            defaultextension=".txr",
            filetypes=[("TXR files", "*.txr"), ("All files", "*.*")])
        if p:
            var.set(p)

    # ── 연결 테스트 ─────────────────────────────────────────────
    def _on_test_connection(self):
        token = self._token_var.get().strip()
        if not token:
            messagebox.showwarning("경고", "API 토큰을 입력하세요.")
            return
        model = self._model_keys[self._model_combo.current()]
        self._append_log("연결 테스트 중...", "info")

        def _test():
            try:
                client = TranslatorClient(token, model)
                result = client.test_connection()
                self.after(0, lambda r=result: self._append_log(
                    f"[연결 성공] {model} 응답: {r[:120]}", "ok"))
            except Exception as ex:
                self.after(0, lambda e=ex: self._append_log(
                    f"[연결 실패] {e}", "err"))

        threading.Thread(target=_test, daemon=True).start()

    def _on_model_change(self, _=None):
        idx = self._model_combo.current()
        key = self._model_keys[idx]
        self._model_var.set(key)

    # ── 시작 / 정지 ─────────────────────────────────────────────
    def _on_start(self):
        inp = self._input_var.get().strip()
        if not inp:
            messagebox.showwarning("경고", "입력 파일을 선택하세요.")
            return
        token = self._token_var.get().strip()
        if not token:
            messagebox.showwarning("경고", "API 토큰을 입력하세요.")
            return

        model   = self._model_keys[self._model_combo.current()]
        debug_p = self._debug_var.get().strip()
        final_p = self._final_var.get().strip()
        try:
            max_chunks = int(self._chunks_run_var.get())
        except ValueError:
            max_chunks = 0
        try:
            lpc = int(self._lpc_var.get())
        except ValueError:
            lpc = LINES_PER_CHUNK

        system_prompt = self._build_glossary_prompt()
        glossary = [(s.get().strip(), t.get().strip())
                    for _, s, t in self._glossary_rows
                    if s.get().strip() and t.get().strip()]

        self._engine.configure(
            input_path=inp,
            output_debug=debug_p,
            output_final=final_p,
            token=token,
            model=model,
            system_prompt=system_prompt,
            lines_per_chunk=lpc,
            max_chunks_per_run=max_chunks,
            glossary=glossary,
        )

        try:
            state = self._engine.load_or_create_state()
            total = state["total_chunks"]
            done  = sum(1 for c in state["chunk_map"] if c["status"] == "done")
            self._chunk_label.configure(text=f"청크: {done}/{total}")
        except Exception as ex:
            messagebox.showerror("오류", f"상태 초기화 실패:\n{ex}")
            return

        self._btn_start.configure(state="disabled")
        self._btn_pause.configure(state="normal")
        self._btn_stop.configure(state="normal")

        self._engine.start(
            on_chunk_start=self._cb_chunk_start,
            on_token=self._cb_token,
            on_chunk_done=self._cb_chunk_done,
            on_error=self._cb_error,
            on_complete=self._cb_complete,
            on_log=self._cb_log,
        )
        self._append_log(f"번역 시작: {Path(inp).name} | 모델: {model}", "info")

    def _on_pause(self):
        self._engine.pause()
        self._btn_pause.configure(state="disabled")
        self._btn_resume.configure(state="normal")
        self._append_log("일시정지됨.", "warn")

    def _on_resume(self):
        self._engine.resume()
        self._btn_resume.configure(state="disabled")
        self._btn_pause.configure(state="normal")
        self._append_log("재개됨.", "info")

    def _on_stop(self):
        self._engine.stop()
        self._btn_stop.configure(state="disabled")
        self._btn_pause.configure(state="disabled")
        self._btn_resume.configure(state="disabled")
        self._btn_start.configure(state="normal")
        self._append_log("중지됨.", "warn")

    # ── 재번역 ──────────────────────────────────────────────────
    def _on_retranslate_selection(self):
        try:
            sel = self._trans_text.get(tk.SEL_FIRST, tk.SEL_LAST).strip()
        except tk.TclError:
            messagebox.showinfo("안내", "번역 패널에서 재번역할 텍스트를 선택하세요.")
            return
        if not sel:
            return

        # 선택된 줄 번호 범위
        sel_start = self._trans_text.index(tk.SEL_FIRST)
        sel_end   = self._trans_text.index(tk.SEL_LAST)
        start_line = int(sel_start.split(".")[0]) - 1
        end_line   = int(sel_end.split(".")[0])

        if not self._current_orig_lines:
            messagebox.showinfo("안내", "청크가 로드되지 않았습니다.")
            return

        # 해당 범위 원본 문자열 추출
        chunk_lines = self._current_orig_lines[start_line:end_line]
        from trans_core import extract_strings
        extractions = extract_strings(chunk_lines)
        orig_strings = [e[4] for e in extractions]
        if not orig_strings:
            messagebox.showinfo("안내", "선택 범위에서 번역 대상 문자열을 찾지 못했습니다.")
            return

        self._append_log(f"선택 재번역: {len(orig_strings)}개 문자열...", "info")

        def _on_token(piece):
            self.after(0, lambda p=piece: self._trans_text.insert("end", ""))

        def _on_done(orig, translated, tok_in, tok_out):
            self.after(0, lambda: self._append_log(
                f"[재번역 완료] {len(translated)}개 | 토큰 {tok_in}/{tok_out}", "ok"))

        prompt = self._build_glossary_prompt()
        self._engine.retranslate_selection(
            orig_strings=orig_strings,
            context=self._current_context,
            system_prompt=prompt,
            on_token=_on_token,
            on_done=_on_done,
        )

    # ── 스크롤 동기화 ────────────────────────────────────────────
    def _on_orig_scroll(self, first, last):
        self._orig_sby.set(first, last)
        if not self._syncing_scroll:
            self._syncing_scroll = True
            self._trans_text.yview_moveto(first)
            self._syncing_scroll = False

    def _on_trans_scroll(self, first, last):
        self._trans_sby.set(first, last)
        if not self._syncing_scroll:
            self._syncing_scroll = True
            self._orig_text.yview_moveto(first)
            self._syncing_scroll = False

    # ── 엔진 콜백 ────────────────────────────────────────────────
    def _cb_chunk_start(self, chunk_idx, total_chunks, orig_lines, extractions):
        self._current_chunk_idx  = chunk_idx
        self._current_orig_lines = orig_lines
        self._current_extractions = extractions

        def _ui():
            # 원본 패널 업데이트
            self._orig_text.configure(state="normal")
            self._orig_text.delete("1.0", "end")
            self._orig_text.insert("1.0", "".join(orig_lines))
            self._orig_text.configure(state="disabled")
            # 번역 패널 초기화 (원본 내용으로 시작)
            self._trans_text.delete("1.0", "end")
            self._trans_text.insert("1.0", "".join(orig_lines))
            self._chunk_label.configure(
                text=f"청크: {chunk_idx + 1}/{total_chunks}")

        self.after(0, _ui)

    def _cb_token(self, piece):
        self.after(0, lambda p=piece: self._insert_token(p))

    def _insert_token(self, piece):
        self._trans_text.insert("end", piece)
        self._trans_text.see("end")

    def _cb_chunk_done(self, chunk_idx, total_chunks, chunks_done_run,
                       max_chunks_run, final_lines, debug_lines,
                       tok_in, tok_out, last_context):
        self._current_context = last_context
        pct = (chunk_idx + 1) / total_chunks * 100

        def _ui():
            # 번역 패널을 완성된 결과로 교체
            self._trans_text.delete("1.0", "end")
            self._trans_text.insert("1.0", "".join(final_lines))
            self._prog_var.set(pct)
            self._pct_label.configure(text=f"{pct:.1f}%")
            self._chunk_label.configure(
                text=f"청크: {chunk_idx + 1}/{total_chunks}  "
                     f"(이번 실행: {chunks_done_run}"
                     + (f"/{max_chunks_run}" if max_chunks_run else "") + ")")
            self._append_log(
                f"[청크 {chunk_idx + 1}/{total_chunks}] 완료 | "
                f"토큰 {tok_in}/{tok_out}", "ok")

        self.after(0, _ui)

    def _cb_error(self, chunk_idx, error_msg):
        self.after(0, lambda: self._append_log(
            f"[오류] 청크 {chunk_idx}: {error_msg}", "err"))
        self.after(0, lambda: (
            self._btn_start.configure(state="normal"),
            self._btn_pause.configure(state="disabled"),
            self._btn_stop.configure(state="disabled"),
        ))

    def _cb_complete(self, total_chunks, chunks_done_total, all_done):
        def _ui():
            self._btn_start.configure(state="normal")
            self._btn_pause.configure(state="disabled")
            self._btn_resume.configure(state="disabled")
            self._btn_stop.configure(state="disabled")
            if all_done:
                self._prog_var.set(100)
                self._pct_label.configure(text="100%")
                self._append_log(
                    f"[완료] 전체 {total_chunks}개 청크 번역 완료!", "ok")
            else:
                self._append_log(
                    f"[정지] {chunks_done_total}/{total_chunks} 청크 완료. "
                    "시작 버튼으로 재개 가능.", "warn")

        self.after(0, _ui)

    def _cb_log(self, msg, level="info"):
        self.after(0, lambda m=msg, lv=level: self._append_log(m, lv))

    # ── 로그 추가 ────────────────────────────────────────────────
    def _append_log(self, msg: str, tag: str = "info"):
        ts = time.strftime("%H:%M:%S")
        self._log_text.configure(state="normal")
        self._log_text.insert("end", f"[{ts}] {msg}\n", tag)
        # 1000줄 제한
        lines = int(self._log_text.index("end-1c").split(".")[0])
        if lines > 1000:
            self._log_text.delete("1.0", f"{lines - 1000}.0")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")


def main():
    app = TranslationApp()
    app.mainloop()


if __name__ == "__main__":
    main()
