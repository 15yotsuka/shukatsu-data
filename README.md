# shukatsu-data

就活締切収集パイプライン

## 概要

企業の採用ページを巡回し、締切日情報をJSON形式で収集・保存します。
Gemini API（gemini-2.5-flash）でHTMLから締切情報を構造化抽出します。

## ファイル構成

```
shukatsu-data/
├── companies.csv              # 収集対象企業リスト
├── collector.py               # メイン収集スクリプト
├── requirements.txt           # Python依存パッケージ
├── data/
│   ├── deadlines-27.json      # 27卒締切データ
│   └── deadlines-28.json      # 28卒締切データ
└── .github/workflows/
    └── collect.yml            # GitHub Actions ワークフロー
```

## セットアップ

```bash
pip install -r requirements.txt
```

## 実行

```bash
# 本番実行（GEMINI_API_KEY 環境変数が必要）
python collector.py

# ドライラン（APIコール・フェッチなし）
python collector.py --dry-run
```

## GitHub Actions

Actions タブから `Collect Deadlines` ワークフローを手動トリガーで実行。

**必要なSecret:**
- `GEMINI_API_KEY`: Google AI Studio で取得したAPIキー

## 出力JSONスキーマ

```json
{
  "version": "1.0",
  "updated_at": "2026-03-08T10:00:00+09:00",
  "grad_year": 27,
  "companies": [
    {
      "company_id": "mitsubishi_corp",
      "company_name": "三菱商事",
      "industry": "総合商社",
      "deadlines": [
        {
          "type": "本選考",
          "deadline": "2026-03-15",
          "label": "第1期エントリー",
          "source_url": "https://...",
          "fetched_at": "2026-03-08T10:00:00+09:00"
        }
      ]
    }
  ]
}
```
