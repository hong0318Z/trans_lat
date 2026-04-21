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
    AppConfig, ProjectManager, LineExtractor,
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
        self._config = AppConfig()
        self._proj_mgr = ProjectManager()
        self._current_project: dict | None = None
        # 줄 번역 모드 (TXT)
        self._extractor: LineExtractor | None = None
        self._line_mode = False
        self._line_stop = threading.Event()
        self._line_pause = threading.Event()
        self._build_style()
        self._build_ui()
        self._load_config_and_projects()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
        # 최상단 — 토글 버튼 + 프로젝트 바
        top = tk.Frame(self, bg=THEME["bg"])
        top.pack(fill="x", padx=4, pady=2)
        self._toggle_btn = tk.Button(top, text="▼ 설정 접기", bg=THEME["btn"],
                                     fg=THEME["fg"], relief="flat", bd=0,
                                     command=self._toggle_settings)
        self._toggle_btn.pack(side="left")
        self._build_project_bar(top)

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

        ttk.Label(opt_fr, text="  줄 수/배치:").pack(side="left")
        self._lpc_var = tk.StringVar(value=str(LINES_PER_CHUNK))
        ttk.Spinbox(opt_fr, from_=1, to=10000, textvariable=self._lpc_var,
                    width=7).pack(side="left", padx=4)

        ttk.Label(opt_fr, text="  연속 배치 수 (0=끝까지):").pack(side="left")
        self._chunks_run_var = tk.StringVar(value="0")
        ttk.Spinbox(opt_fr, from_=0, to=9999, textvariable=self._chunks_run_var,
                    width=6).pack(side="left", padx=4)

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
        self._btn_save = tk.Button(parent, text="💾 저장", bg=THEME["accent"],
                                   fg="#1e1e2e", relief="flat", bd=0,
                                   command=self._on_save_output, padx=8,
                                   state="disabled")

        for b in (self._btn_start, self._btn_pause, self._btn_resume,
                  self._btn_stop, self._btn_retrans, self._btn_save):
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

    # ── 프로젝트 바 ─────────────────────────────────────────────
    def _build_project_bar(self, parent):
        fr = tk.Frame(parent, bg=THEME["bg"])
        fr.pack(side="left", padx=(12, 0))

        tk.Label(fr, text="프로젝트:", bg=THEME["bg"],
                 fg=THEME["fg2"]).pack(side="left", padx=(0, 4))

        self._project_combo = ttk.Combobox(fr, width=28, state="readonly")
        self._project_combo.pack(side="left", padx=4)
        self._project_combo.bind("<<ComboboxSelected>>", self._on_project_selected)

        tk.Button(fr, text="+ 새 프로젝트", bg=THEME["btn"], fg=THEME["fg"],
                  relief="flat", bd=0,
                  command=self._on_new_project, padx=6).pack(side="left", padx=4)
        self._btn_del_proj = tk.Button(fr, text="삭제", bg=THEME["err"], fg="#1e1e2e",
                                       relief="flat", bd=0,
                                       command=self._on_delete_project, padx=6)
        self._btn_del_proj.pack(side="left", padx=2)

        self._project_status = tk.Label(fr, text="", bg=THEME["bg"],
                                        fg=THEME["fg2"])
        self._project_status.pack(side="left", padx=8)

    def _refresh_project_combo(self):
        names = self._proj_mgr.get_names()
        self._project_combo['values'] = names
        if not names:
            self._project_combo.set("")
            self._project_status.configure(text="프로젝트 없음")
        elif self._current_project and self._current_project["name"] in names:
            self._project_combo.set(self._current_project["name"])

    def _on_project_selected(self, event=None):
        name = self._project_combo.get()
        proj = self._proj_mgr.get_project(name)
        if not proj:
            return
        self._current_project = proj
        self._input_var.set(proj["input_path"])
        p = Path(proj["input_path"])
        if not self._debug_var.get():
            self._debug_var.set(str(p.parent / (p.stem + "_debug.txr")))
        if not self._final_var.get():
            self._final_var.set(str(p.parent / (p.stem + "_kr.txr")))

        state_path = proj.get("state_file_path", "")
        if state_path and Path(state_path).exists():
            try:
                sm = StateManager(proj["input_path"], state_path)
                state = sm.load()
                done  = sum(1 for c in state["chunk_map"] if c["status"] == "done")
                total = state["total_chunks"]
                pct   = done / total * 100 if total else 0
                self._prog_var.set(pct)
                self._pct_label.configure(text=f"{pct:.1f}%")
                self._chunk_label.configure(text=f"청크: {done}/{total}")
                self._project_status.configure(
                    text=f"{done}/{total} 청크 완료 ({pct:.0f}%)")
                self._append_log(
                    f"[프로젝트] {name}: {done}/{total} 청크 완료.", "warn")
            except Exception:
                self._project_status.configure(text="상태 파일 읽기 실패")
        else:
            self._prog_var.set(0)
            self._pct_label.configure(text="0.0%")
            self._chunk_label.configure(text="청크: -/-")
            self._project_status.configure(text="미시작")
        self._preview_file(proj["input_path"])

    def _on_new_project(self):
        dlg = tk.Toplevel(self)
        dlg.title("새 프로젝트 만들기")
        dlg.configure(bg=THEME["bg"])
        dlg.resizable(False, False)
        dlg.grab_set()

        name_var  = tk.StringVar()
        input_var = tk.StringVar()

        ttk.Label(dlg, text="프로젝트 이름:").grid(
            row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(dlg, textvariable=name_var, width=36).grid(
            row=0, column=1, padx=4, pady=4, sticky="ew")

        ttk.Label(dlg, text="입력 파일:").grid(
            row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(dlg, textvariable=input_var, width=36).grid(
            row=1, column=1, padx=4, pady=4, sticky="ew")

        def _browse():
            p = filedialog.askopenfilename(
                filetypes=[("TXR files", "*.txr"), ("Text files", "*.txt"),
                           ("All files", "*.*")])
            if p:
                input_var.set(p)

        ttk.Button(dlg, text="찾기", command=_browse).grid(row=1, column=2, padx=4)

        def _confirm():
            name  = name_var.get().strip()
            ipath = input_var.get().strip()
            if not name:
                messagebox.showwarning("경고", "프로젝트 이름을 입력하세요.", parent=dlg)
                return
            if not ipath or not Path(ipath).exists():
                messagebox.showwarning("경고", "유효한 입력 파일을 선택하세요.", parent=dlg)
                return
            try:
                self._proj_mgr.create_project(name, ipath)
            except ValueError as e:
                messagebox.showerror("오류", str(e), parent=dlg)
                return
            dlg.destroy()
            self._refresh_project_combo()
            self._project_combo.set(name)
            self._debug_var.set("")
            self._final_var.set("")
            self._on_project_selected()

        btn_fr = tk.Frame(dlg, bg=THEME["bg"])
        btn_fr.grid(row=2, column=0, columnspan=3, pady=8)
        ttk.Button(btn_fr, text="만들기", command=_confirm).pack(side="left", padx=4)
        ttk.Button(btn_fr, text="취소",   command=dlg.destroy).pack(side="left", padx=4)
        dlg.columnconfigure(1, weight=1)

    def _on_delete_project(self):
        if not self._current_project:
            messagebox.showinfo("안내", "삭제할 프로젝트를 선택하세요.")
            return
        name = self._current_project["name"]
        if not messagebox.askyesno("확인",
                                   f"'{name}' 프로젝트를 삭제하시겠습니까?\n"
                                   "상태 파일도 함께 삭제됩니다."):
            return
        self._proj_mgr.delete_project(name)
        self._current_project = None
        self._input_var.set("")
        self._debug_var.set("")
        self._final_var.set("")
        self._prog_var.set(0)
        self._pct_label.configure(text="0.0%")
        self._chunk_label.configure(text="청크: -/-")
        self._project_status.configure(text="")
        self._refresh_project_combo()
        self._append_log(f"[프로젝트 삭제] {name}", "warn")

    # ── 설정 로드/저장 ───────────────────────────────────────────
    def _load_config_and_projects(self):
        cfg = self._config.load()
        self._proj_mgr.load()

        self._token_var.set(cfg.get("llm_token") or "")

        model_key = cfg.get("model") or DEFAULT_MODEL
        if model_key in self._model_keys:
            self._model_combo.current(self._model_keys.index(model_key))

        self._lpc_var.set(str(cfg.get("lines_per_batch") or LINES_PER_CHUNK))
        self._chunks_run_var.set(str(cfg.get("max_consecutive_runs") or 0))

        prompt = cfg.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        self._prompt_text.delete("1.0", "end")
        self._prompt_text.insert("1.0", prompt)

        for fr, _, _ in list(self._glossary_rows):
            fr.destroy()
        self._glossary_rows.clear()
        for pair in (cfg.get("glossary") or []):
            if len(pair) == 2:
                self._add_glossary_row(pair[0], pair[1])

        self._refresh_project_combo()
        last_name = cfg.get("last_project_name") or ""
        if last_name and last_name in self._proj_mgr.get_names():
            self._project_combo.set(last_name)
            self._on_project_selected()

    def _save_config(self):
        glossary = [[s.get().strip(), t.get().strip()]
                    for _, s, t in self._glossary_rows
                    if s.get().strip() and t.get().strip()]
        model_key = self._model_keys[self._model_combo.current()]
        self._config.save({
            "llm_token":            self._token_var.get().strip(),
            "model":                model_key,
            "lines_per_batch":      self._lpc_var.get(),
            "max_consecutive_runs": self._chunks_run_var.get(),
            "system_prompt":        self._prompt_text.get("1.0", "end").strip(),
            "glossary":             glossary,
            "last_project_name":    self._current_project["name"]
                                    if self._current_project else "",
        })

    def _on_close(self):
        self._save_config()
        self.destroy()

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
            filetypes=[("TXR files", "*.txr"), ("Text files", "*.txt"),
                       ("All files", "*.*")])
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
        self._preview_file(p)

    def _browse_out(self, var, suffix):
        p = filedialog.asksaveasfilename(
            defaultextension=".txr",
            filetypes=[("TXR files", "*.txr"), ("All files", "*.*")])
        if p:
            var.set(p)

    def _preview_file(self, path: str):
        """파일 로드 미리보기. txt → 추출 대상 줄만, txr → 원본 전체."""
        try:
            ext = Path(path).suffix.lower()
            is_txt = ext not in ('.txr', '.qsp')

            text = ""
            for enc in ('utf-8', 'utf-8-sig', 'cp1252', 'latin-1'):
                try:
                    text = Path(path).read_text(encoding=enc)
                    break
                except UnicodeDecodeError:
                    continue

            lines = text.splitlines()
            total_lines = len(lines)

            if is_txt:
                # TXT 모드: HTML 태그 제거 후 비어있지 않은 줄만 추출해서 표시
                from trans_core import _strip_html
                extracted = [(i, _strip_html(l)) for i, l in enumerate(lines) if _strip_html(l)]
                ext_total = len(extracted)
                preview_items = extracted[:500]
                preview = '\n'.join(f"줄{i+1}: {c}" for i, c in preview_items)
                if ext_total > 500:
                    preview += f"\n... (총 {ext_total}개 중 500개 표시)"
                try:
                    lpc = int(self._lpc_var.get())
                except Exception:
                    lpc = LINES_PER_CHUNK
                batches = (ext_total + lpc - 1) // lpc if lpc > 0 else 1
                self._chunk_label.configure(text=f"추출: {ext_total}줄 / 예상 배치: {batches}")
                self._append_log(
                    f"[파일 로드] {Path(path).name}: 전체 {total_lines}줄 → "
                    f"번역 대상 {ext_total}줄 추출, 배치 {batches}개 예상", "info")
            else:
                # QSP/TXR 모드: 전체 내용 표시
                preview = '\n'.join(lines[:1000])
                if total_lines > 1000:
                    preview += f"\n... (총 {total_lines}줄 중 1000줄만 표시)"
                try:
                    lpc = int(self._lpc_var.get())
                except Exception:
                    lpc = LINES_PER_CHUNK
                chunks = (total_lines + lpc - 1) // lpc if lpc > 0 else 1
                self._chunk_label.configure(text=f"예상 배치: 0/{chunks}")
                self._append_log(
                    f"[파일 로드] {Path(path).name}: 총 {total_lines}줄 → 배치 {chunks}개 예상", "info")

            self._orig_text.configure(state="normal")
            self._orig_text.delete("1.0", "end")
            self._orig_text.insert("1.0", preview)
            self._orig_text.configure(state="disabled")
            self._trans_text.delete("1.0", "end")
        except Exception as ex:
            self._append_log(f"[미리보기 실패] {ex}", "err")

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
        final_p = self._final_var.get().strip()
        try:
            max_runs = int(self._chunks_run_var.get())
        except ValueError:
            max_runs = 0
        try:
            lpc = int(self._lpc_var.get())
        except ValueError:
            lpc = LINES_PER_CHUNK

        self._save_config()

        is_txt = Path(inp).suffix.lower() not in ('.txr', '.qsp')
        if is_txt:
            self._start_line_translation(inp, token, model, lpc, max_runs, final_p)
        else:
            self._start_qsp_translation(inp, token, model, lpc, max_runs, final_p)

    def _start_line_translation(self, inp, token, model, lpc, max_runs, final_p):
        """TXT 줄 번역 모드 시작."""
        state_path = self._current_project.get("state_file_path") if self._current_project else None
        ext = LineExtractor(inp, state_path)
        try:
            ext.load()
        except Exception as ex:
            messagebox.showerror("오류", f"파일 로드 실패:\n{ex}")
            return

        if ext.exists_state():
            try:
                ext.load_state()
                self._append_log(
                    f"[재개] 이전 번역 복원: {ext.translated_count}/{ext.total_extracted}줄", "warn")
            except Exception:
                pass

        self._extractor = ext
        self._line_mode = True
        self._line_stop.clear()
        self._line_pause.clear()

        # 왼쪽 패널: 추출된 줄 전체 표시
        self._show_extracted_lines()
        # 오른쪽 패널: 번역 현황 (이전 번역 있으면 표시)
        self._refresh_right_panel()

        system_prompt = self._build_glossary_prompt()
        client = TranslatorClient(token, model)

        self._btn_start.configure(state="disabled")
        self._btn_pause.configure(state="normal")
        self._btn_stop.configure(state="normal")
        self._btn_save.configure(state="disabled")

        threading.Thread(
            target=self._line_translation_loop,
            args=(client, system_prompt, lpc, max_runs),
            daemon=True,
        ).start()
        self._append_log(
            f"[번역 시작] {Path(inp).name} | 모델: {model} | "
            f"추출 {ext.total_extracted}줄 / 배치 {lpc}줄", "info")

    def _start_qsp_translation(self, inp, token, model, lpc, max_runs, final_p):
        """TXR/QSP 청크 번역 모드 시작."""
        self._line_mode = False
        debug_p = self._debug_var.get().strip()
        system_prompt = self._build_glossary_prompt()
        glossary = [(s.get().strip(), t.get().strip())
                    for _, s, t in self._glossary_rows
                    if s.get().strip() and t.get().strip()]
        state_path = self._current_project.get("state_file_path") if self._current_project else None

        self._engine.configure(
            input_path=inp, output_debug=debug_p, output_final=final_p,
            token=token, model=model, system_prompt=system_prompt,
            lines_per_chunk=lpc, max_chunks_per_run=max_runs,
            glossary=glossary, state_path=state_path,
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

    # ── 줄 번역 루프 (백그라운드) ─────────────────────────────────
    def _line_translation_loop(self, client, system_prompt, lpc, max_runs):
        ext = self._extractor
        # 이미 번역된 줄 수부터 시작
        start = ext.translated_count
        runs = 0
        last_context = ""

        while start < ext.total_extracted:
            if self._line_stop.is_set():
                break
            while self._line_pause.is_set():
                if self._line_stop.is_set():
                    break
                time.sleep(0.2)
            if self._line_stop.is_set():
                break

            batch = ext.get_batch(start, lpc)
            contents = [content for _, content in batch]

            try:
                translated, tok_in, tok_out = client.translate_batch(
                    contents, last_context, system_prompt)
            except Exception as ex:
                self.after(0, lambda e=ex: self._append_log(f"[오류] {e}", "err"))
                break

            for i, (orig_idx, _) in enumerate(batch):
                if i < len(translated) and translated[i]:
                    ext.set_translation(orig_idx, translated[i])

            try:
                ext.save_state()
            except Exception:
                pass

            last_context = translated[-1] if translated else ""
            runs += 1
            start += len(batch)
            done = ext.translated_count
            total = ext.total_extracted
            pct = done / total * 100 if total else 0

            self.after(0, lambda d=done, t=total, p=pct, r=runs, mr=max_runs,
                              ti=tok_in, to=tok_out: self._on_line_batch_done(d, t, p, r, mr, ti, to))

            if max_runs > 0 and runs >= max_runs:
                break

        all_done = ext.translated_count >= ext.total_extracted
        self.after(0, lambda ad=all_done: self._on_line_translation_complete(ad))

    def _on_line_batch_done(self, done, total, pct, runs, max_runs, tok_in, tok_out):
        self._prog_var.set(pct)
        self._pct_label.configure(text=f"{pct:.1f}%")
        run_info = f"/{max_runs}" if max_runs else ""
        self._chunk_label.configure(text=f"번역: {done}/{total}줄  (배치 {runs}{run_info})")
        self._append_log(
            f"[배치 {runs}] {done}/{total}줄 완료 | 토큰 {tok_in}/{tok_out}", "ok")
        self._refresh_right_panel()
        if self._extractor and self._extractor.translated_count > 0:
            self._btn_save.configure(state="normal")

    def _on_line_translation_complete(self, all_done):
        self._btn_start.configure(state="normal")
        self._btn_pause.configure(state="disabled")
        self._btn_resume.configure(state="disabled")
        self._btn_stop.configure(state="disabled")
        if self._extractor and self._extractor.translated_count > 0:
            self._btn_save.configure(state="normal")
        if all_done:
            self._prog_var.set(100)
            self._pct_label.configure(text="100%")
            self._append_log(
                f"[완료] 전체 {self._extractor.total_extracted}줄 번역 완료! "
                "'💾 저장' 버튼으로 파일에 적용하세요.", "ok")
        else:
            self._append_log("번역 중지됨. '▶ 시작'으로 이어서 번역 가능.", "warn")

    def _show_extracted_lines(self):
        """추출된 줄 전체를 왼쪽 패널에 표시."""
        if not self._extractor:
            return
        lines = [f"줄{i+1}: {c}" for i, c in self._extractor.extracted]
        text = '\n'.join(lines)
        self._orig_text.configure(state="normal")
        self._orig_text.delete("1.0", "end")
        self._orig_text.insert("1.0", text)
        self._orig_text.configure(state="disabled")

    def _refresh_right_panel(self):
        """번역 현황을 오른쪽 패널에 표시 (번역된 것만)."""
        if not self._extractor:
            return
        tr = self._extractor.translations
        lines = []
        for orig_idx, content in self._extractor.extracted:
            if orig_idx in tr:
                lines.append(f"줄{orig_idx+1}: {tr[orig_idx]}")
            else:
                lines.append(f"줄{orig_idx+1}: ...")
        self._trans_text.delete("1.0", "end")
        self._trans_text.insert("1.0", '\n'.join(lines))

    def _on_save_output(self):
        """번역 결과를 파일로 저장."""
        if not self._extractor or not self._extractor.translations:
            messagebox.showinfo("안내", "저장할 번역이 없습니다.")
            return
        out = self._final_var.get().strip()
        if not out:
            out = filedialog.asksaveasfilename(
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
            if not out:
                return
            self._final_var.set(out)
        try:
            self._extractor.save_output(out)
            done = self._extractor.translated_count
            total = self._extractor.total_extracted
            self._append_log(f"[저장 완료] {Path(out).name} ({done}/{total}줄 번역 적용)", "ok")
        except Exception as ex:
            messagebox.showerror("저장 오류", str(ex))

    def _on_pause(self):
        if self._line_mode:
            self._line_pause.set()
        else:
            self._engine.pause()
        self._btn_pause.configure(state="disabled")
        self._btn_resume.configure(state="normal")
        self._append_log("일시정지됨.", "warn")

    def _on_resume(self):
        if self._line_mode:
            self._line_pause.clear()
        else:
            self._engine.resume()
        self._btn_resume.configure(state="disabled")
        self._btn_pause.configure(state="normal")
        self._append_log("재개됨.", "info")

    def _on_stop(self):
        if self._line_mode:
            self._line_stop.set()
            self._line_pause.clear()
        else:
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
        from trans_core import extract_strings, extract_lines_for_translation
        inp = self._input_var.get().strip()
        plain = Path(inp).suffix.lower() not in ('.txr', '.qsp') if inp else True
        extractions = extract_lines_for_translation(chunk_lines) if plain else extract_strings(chunk_lines)
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
    def _cb_chunk_start(self, chunk_idx, total_chunks, line_start, line_end, orig_text, extraction_count):
        self._current_chunk_idx  = chunk_idx
        self._current_orig_lines = orig_text.splitlines()

        def _ui():
            self._orig_text.configure(state="normal")
            self._orig_text.delete("1.0", "end")
            self._orig_text.insert("1.0", orig_text)
            self._orig_text.configure(state="disabled")
            self._trans_text.delete("1.0", "end")
            self._trans_text.insert("1.0", orig_text)
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
            self._trans_text.insert("1.0", '\n'.join(final_lines))
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
        if self._current_project:
            try:
                self._proj_mgr.touch_modified(self._current_project["name"])
            except Exception:
                pass

        def _ui():
            self._btn_start.configure(state="normal")
            self._btn_pause.configure(state="disabled")
            self._btn_resume.configure(state="disabled")
            self._btn_stop.configure(state="disabled")
            if self._current_project:
                self._on_project_selected()
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
