"""
MinIO Downloader（GUI）

必要なパッケージ:
    pip install minio keyring

起動:
    python minio_downloader_app.py

機能:
    - 接続先は一度入力すれば記憶（画面上で編集可）
    - ID / パスワードを入力して接続（「記憶する」でパスワードを Windows 資格情報マネージャーに保存）
    - バケット内をツリーで閲覧し、複数選択してダウンロード（フォルダ選択で中身をまとめてDL）
    - パスを1行ずつ書いたリストからまとめてダウンロード（リストは次回起動時も残る）

設定ファイル: %APPDATA%\\MinioDownloader\\config.json（パスワードはここには保存しない）
"""

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, ttk

import urllib3
from minio import Minio
from minio.error import S3Error

try:
    import keyring
except ImportError:
    keyring = None

APP_NAME = "MinioDownloader"
# 接続先の初期値は環境変数 MINIO_ENDPOINT から（未設定なら空欄。一度接続すれば config に記憶）
DEFAULT_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "")
CONFIG_PATH = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME / "config.json"
DUMMY = "\x00dummy"  # ツリーの未展開フォルダに入れる仮ノード
# exe化したときは同梱ファイルが _MEIPASS に展開される
ICON_PATH = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets" / "icon.ico"


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- helpers

def parse_endpoint(text: str, secure: bool) -> tuple[str, bool]:
    """'http://host:9000/' のような入力も 'host:9000' に直す"""
    text = text.strip()
    if text.startswith("https://"):
        text, secure = text[len("https://"):], True
    elif text.startswith("http://"):
        text, secure = text[len("http://"):], False
    return text.split("/")[0], secure


def human_size(n: int | None) -> str:
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def keyring_user(endpoint: str, access_key: str) -> str:
    return f"{endpoint}|{access_key}"


def get_saved_password(endpoint: str, access_key: str) -> str:
    if keyring is None or not access_key:
        return ""
    try:
        return keyring.get_password(APP_NAME, keyring_user(endpoint, access_key)) or ""
    except Exception:
        return ""


def set_saved_password(endpoint: str, access_key: str, password: str | None) -> None:
    if keyring is None or not access_key:
        return
    try:
        if password:
            keyring.set_password(APP_NAME, keyring_user(endpoint, access_key), password)
        else:
            keyring.delete_password(APP_NAME, keyring_user(endpoint, access_key))
    except Exception:
        pass


def make_client(endpoint: str, access_key: str, secret_key: str, secure: bool) -> Minio:
    # 既定のままだと到達できない時に数分固まるので、タイムアウトを短めに
    http = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=5, read=60),
        retries=urllib3.Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504]),
    )
    return Minio(endpoint, access_key=access_key, secret_key=secret_key,
                 secure=secure, http_client=http)


# ---------------------------------------------------------------- app

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MinIO Downloader")
        self.geometry("1100x720")
        self.minsize(900, 560)
        try:
            self.iconbitmap(default=str(ICON_PATH))
        except tk.TclError:
            pass

        self.cfg = load_config()
        self.client: Minio | None = None
        self.busy = False
        self.cancel_event = threading.Event()
        self.ui_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._load_values()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._poll_queue)

        # パスワードを記憶済みなら自動接続
        if self.var_pass.get():
            self.after(200, self.connect)

    # ------------------------------------------------------------ UI 構築
    def _build_ui(self):
        pad = {"padx": 4, "pady": 3}

        # --- 接続
        conn = ttk.LabelFrame(self, text="接続")
        conn.pack(fill="x", padx=8, pady=(8, 4))
        self.var_endpoint = tk.StringVar()
        self.var_https = tk.BooleanVar()
        self.var_user = tk.StringVar()
        self.var_pass = tk.StringVar()
        self.var_remember = tk.BooleanVar()

        ttk.Label(conn, text="接続先").grid(row=0, column=0, **pad)
        ttk.Entry(conn, textvariable=self.var_endpoint, width=24).grid(row=0, column=1, **pad)
        ttk.Checkbutton(conn, text="HTTPS", variable=self.var_https).grid(row=0, column=2, **pad)
        if DEFAULT_ENDPOINT:
            ttk.Button(conn, text="既定に戻す", command=self._reset_endpoint).grid(row=0, column=3, **pad)
        ttk.Label(conn, text="ID").grid(row=0, column=4, **pad)
        ttk.Entry(conn, textvariable=self.var_user, width=16).grid(row=0, column=5, **pad)
        ttk.Label(conn, text="パスワード").grid(row=0, column=6, **pad)
        pw = ttk.Entry(conn, textvariable=self.var_pass, width=16, show="●")
        pw.grid(row=0, column=7, **pad)
        pw.bind("<Return>", lambda e: self.connect())
        remember_text = "記憶する" if keyring else "IDを記憶する（keyring未導入）"
        ttk.Checkbutton(conn, text=remember_text, variable=self.var_remember).grid(row=0, column=8, **pad)
        self.btn_connect = ttk.Button(conn, text="接続", command=self.connect)
        self.btn_connect.grid(row=0, column=9, **pad)
        self.lbl_conn = ttk.Label(conn, text="未接続", foreground="gray")
        self.lbl_conn.grid(row=0, column=10, **pad)

        # --- 中央（左: ツリー / 右: パスリスト）
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.LabelFrame(paned, text="ファイル一覧（Ctrl / Shift で複数選択）")
        paned.add(left, weight=3)
        top = ttk.Frame(left)
        top.pack(fill="x")
        ttk.Label(top, text="バケット").pack(side="left", **pad)
        self.var_bucket = tk.StringVar()
        self.cmb_bucket = ttk.Combobox(top, textvariable=self.var_bucket, width=28)
        self.cmb_bucket.pack(side="left", **pad)
        self.cmb_bucket.bind("<<ComboboxSelected>>", lambda e: self.reload_tree())
        self.cmb_bucket.bind("<Return>", lambda e: self.reload_tree())
        ttk.Button(top, text="更新", command=self.reload_tree).pack(side="left", **pad)

        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("size", "modified"), selectmode="extended")
        self.tree.heading("#0", text="名前")
        self.tree.heading("size", text="サイズ")
        self.tree.heading("modified", text="更新日時")
        self.tree.column("#0", width=360)
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("modified", width=130, anchor="center")
        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)

        btns = ttk.Frame(left)
        btns.pack(fill="x")
        self.btn_dl_sel = ttk.Button(btns, text="選択した項目をDL", command=self.download_selected)
        self.btn_dl_sel.pack(side="left", **pad)
        ttk.Button(btns, text="→ パスリストに追加", command=self.add_selected_to_list).pack(side="left", **pad)

        right = ttk.LabelFrame(paned, text="パスを指定してDL（1行に1つ、フォルダは末尾に /）")
        paned.add(right, weight=2)
        self.txt_paths = tk.Text(right, height=10, wrap="none", undo=True)
        self.txt_paths.pack(fill="both", expand=True, **pad)
        ttk.Label(right, text="例:  data/2026/sample.raw\n       data/2026/batch01/",
                  foreground="gray").pack(anchor="w", **pad)
        rbtns = ttk.Frame(right)
        rbtns.pack(fill="x")
        self.btn_dl_list = ttk.Button(rbtns, text="リストをDL", command=self.download_list)
        self.btn_dl_list.pack(side="left", **pad)
        ttk.Button(rbtns, text="クリア", command=lambda: self.txt_paths.delete("1.0", "end")).pack(side="left", **pad)

        # --- 保存先
        out = ttk.LabelFrame(self, text="保存先")
        out.pack(fill="x", padx=8, pady=4)
        self.var_outdir = tk.StringVar()
        self.var_keepdirs = tk.BooleanVar()
        self.var_overwrite = tk.BooleanVar()
        self.var_ask_outdir = tk.BooleanVar()
        out_row1 = ttk.Frame(out)
        out_row1.pack(fill="x")
        ttk.Entry(out_row1, textvariable=self.var_outdir).pack(side="left", fill="x", expand=True, **pad)
        ttk.Button(out_row1, text="参照...", command=self._choose_outdir).pack(side="left", **pad)
        ttk.Button(out_row1, text="フォルダを開く", command=self._open_outdir).pack(side="left", **pad)
        out_row2 = ttk.Frame(out)
        out_row2.pack(fill="x")
        ttk.Checkbutton(out_row2, text="DLのたびに保存先を選ぶ", variable=self.var_ask_outdir).pack(side="left", **pad)
        ttk.Checkbutton(out_row2, text="バケット内のフォルダ構造を保持", variable=self.var_keepdirs).pack(side="left", **pad)
        ttk.Checkbutton(out_row2, text="既存ファイルを上書き", variable=self.var_overwrite).pack(side="left", **pad)

        # --- 進捗
        prog = ttk.Frame(self)
        prog.pack(fill="x", padx=8, pady=4)
        self.progress = ttk.Progressbar(prog, maximum=1.0)
        self.progress.pack(side="left", fill="x", expand=True, **pad)
        self.lbl_status = ttk.Label(prog, text="", width=40)
        self.lbl_status.pack(side="left", **pad)
        self.btn_cancel = ttk.Button(prog, text="キャンセル", command=self.cancel_event.set, state="disabled")
        self.btn_cancel.pack(side="left", **pad)

        # --- ログ
        self.txt_log = tk.Text(self, height=8, state="disabled", wrap="none")
        self.txt_log.pack(fill="both", padx=8, pady=(0, 8))

    def _load_values(self):
        c = self.cfg
        self.var_endpoint.set(c.get("endpoint", DEFAULT_ENDPOINT))
        self.var_https.set(c.get("secure", False))
        self.var_user.set(c.get("access_key", ""))
        self.var_remember.set(c.get("remember", False))
        if self.var_remember.get():
            self.var_pass.set(get_saved_password(self.var_endpoint.get(), self.var_user.get()))
        self.var_bucket.set(c.get("bucket", ""))
        self.var_outdir.set(c.get("out_dir", str(Path.home() / "Downloads" / "minio")))
        self.var_keepdirs.set(c.get("keep_dirs", False))
        self.var_overwrite.set(c.get("overwrite", False))
        self.var_ask_outdir.set(c.get("ask_out_dir", False))
        self.txt_paths.insert("1.0", c.get("paths", ""))

    def _save_values(self):
        self.cfg.update(
            endpoint=self.var_endpoint.get().strip(),
            secure=self.var_https.get(),
            access_key=self.var_user.get().strip() if self.var_remember.get() else "",
            remember=self.var_remember.get(),
            bucket=self.var_bucket.get().strip(),
            out_dir=self.var_outdir.get().strip(),
            keep_dirs=self.var_keepdirs.get(),
            overwrite=self.var_overwrite.get(),
            ask_out_dir=self.var_ask_outdir.get(),
            paths=self.txt_paths.get("1.0", "end").rstrip("\n"),
        )
        try:
            save_config(self.cfg)
        except OSError as e:
            self.log(f"設定の保存に失敗: {e}")

    # ------------------------------------------------------------ 小物
    def log(self, msg: str):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", msg + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _reset_endpoint(self):
        self.var_endpoint.set(DEFAULT_ENDPOINT)
        self.var_https.set(False)

    def _choose_outdir(self):
        d = filedialog.askdirectory(initialdir=self.var_outdir.get() or str(Path.home()))
        if d:
            self.var_outdir.set(d)

    def _open_outdir(self):
        d = Path(self.var_outdir.get())
        d.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(d)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(d)])

    def run_bg(self, func, on_done=None, on_error=None):
        """func を別スレッドで実行し、結果をUIスレッドのコールバックに渡す"""
        def worker():
            try:
                result = func()
            except Exception as e:  # noqa: BLE001
                self.ui_queue.put((on_error or self._default_error, e))
            else:
                if on_done:
                    self.ui_queue.put((on_done, result))
        threading.Thread(target=worker, daemon=True).start()

    def _default_error(self, e: Exception):
        msg = f"{e.code}: {e.message}" if isinstance(e, S3Error) else str(e)
        self.log(f"エラー: {msg}")
        messagebox.showerror("エラー", msg)

    def _poll_queue(self):
        try:
            while True:
                callback, arg = self.ui_queue.get_nowait()
                callback(arg)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------ 接続
    def connect(self):
        endpoint, secure = parse_endpoint(self.var_endpoint.get(), self.var_https.get())
        self.var_endpoint.set(endpoint)
        self.var_https.set(secure)
        user, pw = self.var_user.get().strip(), self.var_pass.get()
        if not (endpoint and user and pw):
            messagebox.showwarning("入力不足", "接続先・ID・パスワードを入力してください")
            return

        self.btn_connect.configure(state="disabled")
        self.lbl_conn.configure(text="接続中...", foreground="gray")
        client = make_client(endpoint, user, pw, secure)

        def check():
            # 権限によってはバケット一覧を取れないので、その場合は手入力してもらう
            try:
                return [b.name for b in client.list_buckets()]
            except S3Error as e:
                if e.code == "AccessDenied":
                    return None
                raise

        def done(buckets):
            self.client = client
            self.btn_connect.configure(state="normal")
            self.lbl_conn.configure(text=f"接続OK（{user}）", foreground="green")
            set_saved_password(endpoint, user, pw if self.var_remember.get() else None)
            self._save_values()
            if buckets is None:
                self.log("バケット一覧を取得する権限がありません。バケット名を直接入力して Enter を押してください。")
                buckets = []
            self.cmb_bucket["values"] = buckets
            if self.var_bucket.get() not in buckets and buckets:
                self.var_bucket.set(buckets[0])
            self.log(f"接続しました: {endpoint}（バケット {len(buckets)} 個）")
            if self.var_bucket.get():
                self.reload_tree()

        def fail(e):
            self.client = None
            self.btn_connect.configure(state="normal")
            self.lbl_conn.configure(text="接続失敗", foreground="red")
            if isinstance(e, S3Error) and e.code in ("InvalidAccessKeyId", "SignatureDoesNotMatch"):
                msg = "ID またはパスワードが違います"
            elif isinstance(e, urllib3.exceptions.HTTPError):
                msg = f"接続先に到達できません: {endpoint}\n（ネットワーク / VPN / ポート番号を確認してください）"
            else:
                msg = f"{e.code}: {e.message}" if isinstance(e, S3Error) else str(e)
            self.log(f"接続失敗: {msg}")
            messagebox.showerror("接続失敗", msg)

        self.run_bg(check, done, fail)

    def _require_client(self) -> bool:
        if self.client is None:
            messagebox.showinfo("未接続", "先に「接続」してください")
            return False
        if not self.var_bucket.get().strip():
            messagebox.showinfo("バケット未選択", "バケットを選択してください")
            return False
        return True

    # ------------------------------------------------------------ ツリー
    def reload_tree(self):
        self.tree.delete(*self.tree.get_children())
        if self.client is None or not self.var_bucket.get().strip():
            return
        self._load_children("", "")

    def _load_children(self, parent_iid: str, prefix: str):
        bucket = self.var_bucket.get().strip()
        client = self.client

        def fetch():
            items = list(client.list_objects(bucket, prefix=prefix or None, recursive=False))
            items.sort(key=lambda o: (not o.is_dir, o.object_name.lower()))
            return items

        def done(items):
            if parent_iid and not self.tree.exists(parent_iid):
                return
            for child in self.tree.get_children(parent_iid):
                self.tree.delete(child)
            for o in items:
                name = o.object_name[len(prefix):]
                if self.tree.exists(o.object_name):
                    continue
                if o.is_dir:
                    self.tree.insert(parent_iid, "end", iid=o.object_name, text="📁 " + name.rstrip("/"),
                                     values=("", ""))
                    self.tree.insert(o.object_name, "end", iid=o.object_name + DUMMY, text="読み込み中...")
                else:
                    mod = o.last_modified.astimezone().strftime("%Y-%m-%d %H:%M") if o.last_modified else ""
                    self.tree.insert(parent_iid, "end", iid=o.object_name, text="📄 " + name,
                                     values=(human_size(o.size), mod))
            if not items and not parent_iid:
                self.log(f"バケット '{bucket}' は空です")

        self.run_bg(fetch, done)

    def _on_tree_open(self, _event):
        iid = self.tree.focus()
        if self.tree.exists(iid + DUMMY):
            self._load_children(iid, iid)

    def add_selected_to_list(self):
        sel = self.tree.selection()
        if not sel:
            return
        current = self.txt_paths.get("1.0", "end").rstrip("\n")
        existing = set(current.splitlines())
        new = [s for s in sel if s not in existing and not s.endswith(DUMMY)]
        if new:
            self.txt_paths.insert("end", ("\n" if current else "") + "\n".join(new))

    # ------------------------------------------------------------ ダウンロード
    def download_selected(self):
        sel = [s for s in self.tree.selection() if not s.endswith(DUMMY)]
        if not sel:
            messagebox.showinfo("未選択", "ダウンロードする項目をツリーで選択してください")
            return
        self._start_download(sel)

    def download_list(self):
        paths = []
        for line in self.txt_paths.get("1.0", "end").splitlines():
            line = line.strip().lstrip("/")
            if line and not line.startswith("#"):
                paths.append(line)
        if not paths:
            messagebox.showinfo("リストが空", "ダウンロードするパスを1行に1つ書いてください")
            return
        self._start_download(paths)

    def _start_download(self, paths: list[str]):
        if self.busy or not self._require_client():
            return
        if self.var_ask_outdir.get() or not self.var_outdir.get().strip():
            d = filedialog.askdirectory(title="保存先フォルダを選択",
                                        initialdir=self.var_outdir.get() or str(Path.home()))
            if not d:
                return
            self.var_outdir.set(d)
        out_dir = Path(self.var_outdir.get().strip())
        self._save_values()

        bucket = self.var_bucket.get().strip()
        client = self.client
        keep_dirs = self.var_keepdirs.get()
        overwrite = self.var_overwrite.get()
        self.cancel_event.clear()
        self._set_busy(True)
        self.log(f"--- ダウンロード開始（{len(paths)} 件指定）→ {out_dir}")

        def post(func, *args):
            self.ui_queue.put((lambda a: func(*a), args))

        def job():
            # 1) 指定パスを実ファイルのリストに展開
            targets: list[tuple[str, int, PurePosixPath]] = []  # (key, size, 保存先の相対パス)
            missing = []
            for p in paths:
                if self.cancel_event.is_set():
                    raise Cancelled
                if p.endswith("/"):
                    objs = [o for o in client.list_objects(bucket, prefix=p, recursive=True)
                            if not o.object_name.endswith("/")]
                    if not objs:
                        missing.append(p)
                    # フォルダ指定時は「選択したフォルダ名/以下」を保存
                    base = PurePosixPath(p.rstrip("/")).parent
                    for o in objs:
                        rel = (PurePosixPath(o.object_name) if keep_dirs
                               else PurePosixPath(o.object_name).relative_to(base) if str(base) != "."
                               else PurePosixPath(o.object_name))
                        targets.append((o.object_name, o.size or 0, rel))
                else:
                    try:
                        st = client.stat_object(bucket, p)
                    except S3Error as e:
                        if e.code in ("NoSuchKey", "NoSuchObject"):
                            # 末尾 / を付け忘れたフォルダかもしれない
                            objs = list(client.list_objects(bucket, prefix=p + "/", recursive=True))
                            if objs:
                                post(self.log, f"'{p}' はフォルダとして扱います")
                                paths.append(p + "/")
                                continue
                            missing.append(p)
                            continue
                        raise
                    rel = PurePosixPath(p) if keep_dirs else PurePosixPath(PurePosixPath(p).name)
                    targets.append((p, st.size or 0, rel))

            for m in missing:
                post(self.log, f"見つかりません: {m}")

            # 2) ダウンロード
            total = sum(t[1] for t in targets) or 1
            done_bytes = 0
            ok = skipped = failed = 0
            out_resolved = out_dir.resolve()

            class Progress:
                def set_meta(self_, object_name, total_length):
                    pass

                def update(self_, size):
                    nonlocal done_bytes
                    if self.cancel_event.is_set():
                        raise Cancelled
                    done_bytes += size
                    post(self._update_progress, done_bytes / total, None)

            for i, (key, size, rel) in enumerate(targets, 1):
                if self.cancel_event.is_set():
                    raise Cancelled
                dest = (out_dir / Path(*rel.parts)).resolve()
                if not dest.is_relative_to(out_resolved):  # '../' を含むキー対策
                    post(self.log, f"スキップ（不正なパス）: {key}")
                    failed += 1
                    continue
                post(self._update_progress, done_bytes / total, f"{i}/{len(targets)}  {rel.name}")
                if dest.exists() and not overwrite:
                    post(self.log, f"スキップ（既存）: {dest}")
                    skipped += 1
                    done_bytes += size
                    continue
                tmp = dest.with_name(dest.name + ".part")
                try:
                    client.fget_object(bucket, key, str(dest), tmp_file_path=str(tmp), progress=Progress())
                    post(self.log, f"OK: {key}  ({human_size(size)})")
                    ok += 1
                except Cancelled:
                    tmp.unlink(missing_ok=True)
                    raise
                except Exception as e:  # noqa: BLE001
                    tmp.unlink(missing_ok=True)
                    msg = f"{e.code}" if isinstance(e, S3Error) else str(e)
                    post(self.log, f"失敗: {key}  ({msg})")
                    failed += 1
            return ok, skipped, failed + len(missing)

        def done(result):
            ok, skipped, failed = result
            self._set_busy(False)
            self._update_progress(1.0, f"完了: 成功 {ok} / スキップ {skipped} / 失敗 {failed}")
            self.log(f"--- 完了: 成功 {ok} / スキップ {skipped} / 失敗 {failed}")
            if failed:
                messagebox.showwarning("一部失敗", f"{failed} 件ダウンロードできませんでした。ログを確認してください。")

        def fail(e):
            self._set_busy(False)
            if isinstance(e, Cancelled):
                self._update_progress(0, "キャンセルしました")
                self.log("--- キャンセルしました")
            else:
                self._update_progress(0, "エラー")
                self._default_error(e)

        self.run_bg(job, done, fail)

    def _update_progress(self, fraction: float, text: str | None):
        self.progress["value"] = fraction
        if text is not None:
            self.lbl_status.configure(text=text)

    def _set_busy(self, busy: bool):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_dl_sel.configure(state=state)
        self.btn_dl_list.configure(state=state)
        self.btn_cancel.configure(state="normal" if busy else "disabled")

    def on_close(self):
        if self.busy and not messagebox.askyesno("確認", "ダウンロード中です。終了しますか？"):
            return
        self.cancel_event.set()
        self._save_values()
        self.destroy()


def main():
    if sys.platform == "win32":
        try:  # 高DPI画面で文字がぼやけないように
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
