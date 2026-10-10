---
name: token-diet-diagnose
description: Claude Code のローカル利用ログ(~/.claude/projects)から、トークン消費の原因別の無駄分類と、品質リスクによるクイックウィン／要検証の仕分けを、集計値のみのレポート(Markdown+HTML)で出す。ネットワーク不使用。
---

# token-diet-diagnose

## 実行
1. 出力先はリポジトリやログの外にする（例: `/tmp/tokdiet-out`）。
2. `python3 tokdiet.py --out /tmp/tokdiet-out` （既定の入力は `~/.claude/projects`）
3. `python3 leakcheck.py /tmp/tokdiet-out/report.md /tmp/tokdiet-out/report.html --logs ~/.claude/projects`
   - `--logs` は `~/.claude/projects` でもプロジェクト単体（`~/.claude/projects/<proj>`）でも可。
   - 終了コードが0でなければ、レポートを**誰にも渡さず**、検出内容を報告する。
4. ローカル詳細版 `action-brief.md`（ローカル専用・パス/コマンド断片を含む・0600）は**共有しない**。自分のClaudeに読ませて、各項目の再確認→変更案→前後比較（`--snapshot` / `--compare`）に使う。leakcheck は匿名の report.* 専用。
5. 利用者へは `report.html` / `report.md` の場所と、レポートの「測れなかった項目」を必ず伝える。

## action-brief の読み方
- 『実測の重い順 上位5』は提案が立たない項目も載る（理由つき）。優先アクションに無い＝軽い、ではない。
- 『意図的設定の記載あり』のエージェントは、指示ファイルに名前とモデルが併記されているためモデル引き下げを提案しない。
- 履歴系の節は見出し語だけの判定。『内容を確認して判断』であり、規範語を含む節は除外される。

## 守ること
- ネットワークアクセス禁止（価格表も取得しない）。ログ本文・パス・リポジトリ名を匿名レポート（report.*）や第三者向けの会話に書き出さない（action-brief.md はローカル専用の例外）。
- 「品質リスクなし」と言わない。要検証の項目は「自分のログで品質同等を測らないと効果は言えない」と伝える。
- 削減額・削減率を断言しない（根拠ラベル: 実測／推定／根拠なし）。
- 金額はAPI単価での推計。定額プランでは円の削減ではなく枠の拡大として説明する。
