# MinIO Downloader

> ### 🦩 MinIO からのダウンロードがうまくいかない場合は、ぜひこのソフトを使ってください！
> ID とパスワードを入れるだけで、ファイルを選んで（またはパスを書いて）まとめてダウンロードできます。

MinIO（S3 互換ストレージ）からファイルをダウンロードするための Windows 向け GUI ツールです。

## ダウンロード

[**Releases**](../../releases/latest) から `MinioDownloader.exe` をダウンロードして、ダブルクリックするだけで使えます（Python のインストールは不要）。

> 初回起動時に「Windows によって PC が保護されました」と出た場合は、「詳細情報」→「実行」を押してください（署名していない exe のため表示されます）。

## 使い方

1. **接続**
   - 接続先（例: `192.168.0.10:9000`）を入力します。`http://` や末尾の `/` は付けても自動で外します。一度接続すると次回から入力済みになります
   - 環境変数 `MINIO_ENDPOINT` を設定しておくと、それが初期値として入ります（「既定に戻す」ボタンも表示されます）
   - ID とパスワードを入力して「接続」
   - 「記憶する」にチェックすると、次回から自動で接続します
2. **ダウンロードするものを選ぶ**（どちらでも OK）
   - **ツリーから選ぶ**: バケットを選び、Ctrl / Shift で複数選択 →「選択した項目をDL」。フォルダを選ぶと中身をまとめてDLします
   - **パスを指定する**: 右の欄に 1 行に 1 つパスを書いて「リストをDL」。フォルダは末尾に `/` を付けます。リストは次回起動時も残ります
     ```
     data/2026/sample.raw
     data/2026/batch01/
     ```
3. **保存先**
   - 「参照...」で保存先フォルダを指定（「DLのたびに保存先を選ぶ」にチェックすると毎回選べます）
   - 「バケット内のフォルダ構造を保持」: オンにするとバケット内のパスのまま保存します
   - 「既存ファイルを上書き」: オフの場合、同名ファイルはスキップします

ダウンロード中は進捗バーが表示され、途中でキャンセルもできます。

## パスワードの保存について

- 「記憶する」をオンにした場合、パスワードは **Windows 資格情報マネージャー** に保存されます
- 設定ファイル（`%APPDATA%\MinioDownloader\config.json`）には、接続先・ID・保存先・パスリストのみ保存し、パスワードは保存しません

## Python から実行する場合

```bash
pip install -r requirements.txt
python minio_downloader_app.py
```

## exe のビルド

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name MinioDownloader --icon assets/icon.ico --add-data "assets/icon.ico;assets" minio_downloader_app.py
```

`dist/MinioDownloader.exe` ができます。

---

本ツールは MinIO, Inc. とは関係のない非公式ツールです。MinIO およびロゴは MinIO, Inc. の商標です。
