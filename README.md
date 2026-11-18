# harnessmith

Claude Code に仕事を任せるハーネスを作り、点数で育てるためのスキルと道具。

A set of Claude Code skills for building a harness around an unattended agent: the agent works without the answer, its result is scored, and the setup (prompt and permissions) is grown by the score. The checks on the measuring side come first.

## 何をするか

エージェントに仕事を無人で任せたいとき、指示や権限をどう組めばよいかは、試して比べないと分かりません。harnessmith は、その「試して比べる」を、次の決まりで回します。

- 作業エージェントには解答を見せない。資料だけを渡し、隔離した Claude Code で解かせる
- 採点は別の役がする。手本の項目を拾えたかを判定者 2 体が判定するか、コマンド（テストなど）の結果を点にする
- 改善の案は、解答を見ない振り返り役が 1 つずつ出す
- 候補は 2 回以上回し、平均で比べる。同じ構成のぶれの幅を超えた差だけを採る
- 試行と採否は、足すだけの台帳に残す

構成をいじる前に、測る側の点検を 4 つ通します。ここを飛ばすと、エージェントではなく測り方の誤りを追いかけることになります。

| 点検 | 飛ばすと起きること |
| --- | --- |
| 採点表を、書いた者とは別の目で解答と照らす | 写し違いで、同じ成果物の点が動く |
| 判定の基準を書く | 判定者の判定が割れ、点が安定しない |
| 比べる相手が、何を読んで作られたかを確かめる | 解答を見て直した版を基準にし、届かない線を追う |
| 何も足さない構成を 3 回以上回し、ぶれの幅を測る | ぶれと同じ大きさの差を「効いた」と読む |

考え方の全体は [METHOD.md](skills/harnessmith-setup/METHOD.md) にあります。

## 使える仕事

同じ仕事を何度もやり直せて、出来を点数にでき、正解を作業エージェントから隠せる仕事に使えます。

| 仕事の例 | 正解にあたるもの | 採点 |
| --- | --- | --- |
| 資料から成果物を作る（設計、図、文書） | 手本の成果物 | 採点表の項目ごとに「ある・一部・ない」 |
| 仕様書からの実装 | 受け入れテスト（作業役には見せない） | 通ったテストの割合 |
| 既存コードの移行・書き換え | 元の実装のふるまい | 同じ入力への応答が一致する割合 |
| 調査・レビュー | 既知の不具合の一覧 | 見つけた割合 |

## スキル

| スキル | やること |
| --- | --- |
| `/harnessmith-setup` | ベンチ（作業場所）を作る。採点表を別のエージェントに書かせて点検させ、素の構成を 3 回回して、出発点とぶれの幅を出す |
| `/harnessmith-run` | 振り返り役に変更を 1 つ出させ、候補を回し、採否を台帳に書く。止める条件まで繰り返す |
| `/harnessmith-status` | 点検の通過、構成ごとの点、使った額、次の一手を返す |

## 入れ方

Claude Code と Python 3.9 以上が要ります。ほかの依存はありません。

```sh
git clone https://github.com/O6lvl4/harnessmith
cd harnessmith
for s in setup run status; do ln -s "$PWD/skills/harnessmith-$s" ~/.claude/skills/harnessmith-$s; done

# 一度だけ: 作業エージェント用の、隔離した設定でログインする
CLAUDE_CONFIG_DIR=~/.harnessmith/config claude   # /login して抜ける
```

あとは Claude Code で `/harnessmith-setup` を呼びます。

## 置き場所

| 場所 | 中身 | commit |
| --- | --- | --- |
| ベンチ（あなたのリポジトリの中） | `harnessmith.json`、構成 `comp/`、台帳 `ledger.jsonl`、振り返り `notes/`、道具の写し `tool/` | する |
| `~/.harnessmith/<名前>/materials/` | 作業エージェントに渡す資料 | しない |
| `~/.harnessmith/<名前>/private/` | 解答と採点表 | しない |
| `~/.harnessmith/<名前>/runs/` | 試行の記録 | しない |

道具は 1 ファイル（[harnessmith.py](skills/harnessmith-setup/tool/harnessmith.py)）で、スキルなしでも使えます。

```sh
python3 tool/harnessmith.py --help
```

## 費用と注意

- 試行のたびに Claude を動かします。1 回の上限額、全体の予算は `harnessmith.json` で決めます
- 作業エージェントは、権限の設定で読める場所を絞っていますが、サンドボックスには入っていません。Bash は作業フォルダの外にも届きます。信頼できない資料を渡すときは、コンテナなどの中で回してください
- 隔離は、設定フォルダの分離、MCP の取り外し、読み取りの拒否、記録の事後検査で行っています

## ライセンス

MIT
