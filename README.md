# 运营数据质量与对账工作台

用于本地运营数据检查与对账的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m ops_workbench --help
python3 -m ops_workbench --version
python3 -m unittest discover -s tests -v
```

无参数显示帮助，未知参数以非零状态退出。

## audit-orders

校验订单 CSV 并以 JSON Lines 输出发现：

```bash
python3 -m ops_workbench audit-orders \
  --schema '{"order_id":["oid"],"sku":["sku"],"qty":["qty"],"status":["status"],"updated_at":["updated_at"]}' \
  orders.csv [--output report.jsonl]
```

- `--schema S`：UTF-8 JSON 对象，只能含 `order_id, sku, qty, status, updated_at` 五个键，各值为非空候选列名数组；每个字段须在表头恰好命中一列且所选列互异。
- `INPUT`：可带 BOM 的 UTF-8 CSV；表头不得为空或重名，允许额外列。
- 行级发现输出 `["invalid",记录号,逻辑字段,原值]`；分组发现输出 `["duplicate"|"conflict",[order_id,sku],[记录号…]]`；末行为 `["summary",数据行数,发现数,INPUT原始字节SHA-256]`。
- 记录号为 CSV 逻辑记录序号：表头为 1，此后 CSV 解析器读出的每个逻辑记录依次加 1；空行及仅含空白的记录跳过且不计入数据行，但仍占用记录号；字段内引号包裹的换行不增号。发现按涉及的最小记录号、类型（`conflict`、`duplicate`、`invalid` 字典序）、逻辑字段名升序排列。
- 无发现退出 0，有发现退出 1；schema、编码、CSV 或表头错误退出 2（原因写入 stderr，不生成报告）。
- 默认写 stdout；指定 `--output O` 时报告完整生成后原子替换 `O`，失败时保留旧文件并清理临时文件。

### 批次追溯：`--db DB --batch ID`

`--db` 与 `--batch` 必须成对给出（单独给出退出 2）。完整扫描后（退出 0 或 1），在 SQLite 数据库 `DB` 中以单事务保存：批次 ID、输入原始字节 SHA-256、解析后的 schema，以及除 summary 外的发现集；报告写出参与同一事务，写出失败则批次一并回滚。

同一 ID 再次运行时，仅当哈希、schema、发现集三者均相同才幂等返回报告（数据库不变）；否则退出 3，stderr 说明冲突的方面，数据库与旧输出均不变。输入、数据库或写出错误退出 2，不会改写批次。

## diff-audits

比较两个已存批次的发现集并输出差异解释：

```bash
python3 -m ops_workbench diff-audits --db batches.db OLD NEW [--output O]
```

- 发现身份：`invalid` 取 `["invalid",记录号,字段]`；`duplicate`/`conflict` 取 `[类型,order_id,sku]`。
- 仅 NEW 有的输出 `["added",身份,NEW发现]`；仅 OLD 有的输出 `["resolved",身份,OLD发现]`；同身份但完整发现不同输出 `["changed",身份,OLD发现,NEW发现]`；完全相同的省略。
- 结果按身份逐项升序，末行为 `["summary",added数,resolved数,changed数,OLD哈希,NEW哈希]`。
- 成功退出 0；数据库或批次不存在退出 2（原因写入 stderr，不产生部分结果）。默认写 stdout；指定 `--output O` 时沿用原子替换保护。

## decide

对已存批次的某条发现登记人工处置：

```bash
python3 -m ops_workbench decide --db batches.db BATCH ID ACTION REASON
```

- `ID` 为 `diff-audits` 所用身份的 JSON 文本（如 `'["invalid",2,"qty"]'`），且必须命中 `BATCH` 的某条发现；发现身份与完整发现一并保存。
- `ACTION` 仅可为 `confirm`、`ignore`、`fix`；`REASON` 修剪空白后保存且不得为空。
- 决定在单事务中写入独立的 `decisions` 表（`batch_id` + 身份 JSON 为主键）。重复完全相同的决定幂等返回 0；同一身份但动作、原因或所存发现不同时退出 3 且不改记录。
- 数据库或批次不存在、ID 非法或未命中、参数非法均退出 2（原因写入 stderr，不创建或改写决定）。

## review-decisions

以较新批次复核对较旧批次登记的人工处置，输出 JSONL：

```bash
python3 -m ops_workbench review-decisions --db batches.db OLD NEW [--output O]
```

`D` 表示 `[ACTION,REASON]`（原因为已修剪的存储文本）：

- OLD 决定的身份在 NEW 中仍存在且完整发现未变：`["kept",身份,D,NEW发现]`。
- 身份仍在但完整发现改变：`["invalid",身份,"changed",D,OLD发现,NEW发现]`，并额外输出该身份的 `["pending",身份,NEW发现]`。
- 身份已消失：`["invalid",身份,"resolved",D,OLD发现,null]`。
- NEW 中没有对应 kept 决定的发现输出 `["pending",身份,NEW发现]`。
- 结果按身份逐项升序；同一身份的 `invalid` 先于 `pending`。末行为 `["summary",kept数,invalid数,pending数,OLD哈希,NEW哈希]`。
- 成功退出 0；数据库或批次不存在退出 2（原因写入 stderr，不产生部分结果）。默认写 stdout；指定 `--output O` 时沿用原子替换保护，失败保留旧文件。

## propose-fix

把对某条 `fix` 决定的可追溯修正登记为提案：

```bash
python3 -m ops_workbench propose-fix --db batches.db BATCH ID PATCH
```

- `ID` 沿用 `diff-audits` 的发现身份；`BATCH` 中该身份必须已有一条 **`fix`** 决定，且决定所存完整发现仍与批次中的发现匹配。
- `PATCH` 为非空 JSON 数组，元素是 `[记录号,字段,新值]`；字段限五个审计逻辑字段 `order_id, sku, qty, status, updated_at`，新值必须以字符串给出并通过对应字段校验（`order_id/sku` 修剪后非空、`qty` 为正整数、`status` 为 `open|cancelled`、`updated_at` 为秒级带时区 ISO 8601）。
- 目标范围：`invalid` 只能改其身份指定的单个单元格（记录号与字段都必须一致）；`duplicate`/`conflict` 只能改发现记录号列表中的记录（字段任取五个之一）。目标单元格不得重复。
- 单事务在独立的 `fix_proposals` 表保存身份、**原发现**与 PATCH（`batch_id` + 身份 JSON 为主键）。PATCH 完全相同则幂等返回 0；同身份而异内容退出 3 且不改记录；其他错误（参数非法、无 fix 决定、发现不匹配、新值不合规、数据库错误等）退出 2 且不写库。

## apply-fixes

应用一个来源批次的全部修正并追溯派生出新批次：

```bash
python3 -m ops_workbench apply-fixes --db batches.db SOURCE DERIVED INPUT \
  --output corrected.csv [--report report.jsonl]
```

- `INPUT` 的原始字节 SHA-256 必须等于 `SOURCE` 批次保存的哈希；否则退出 2，不产生任何输出。
- `SOURCE` 的每条 `fix` 决定都必须有对应提案；提案所存发现、决定所存发现都必须仍与来源批次的发现匹配，提案 PATCH 重新通过范围与取值校验。任何失效决定、缺提案或非法修正均退出 2。
- 全部提案先按发现身份逐项升序，同一发现内再按记录号、逻辑字段文本升序合并执行。每份 PATCH 内部目标单元格仍不得重复，但不同发现可以修改同一单元格；同一单元格以后执行的值覆盖先前值，最终 CSV、重审计结果与 `DERIVED` 内容均以该值为准。随后用 `SOURCE` 的 schema 对修正后的 CSV 重新审计，审计 JSONL 写到 stdout 或 `--report R`。
- 修正后 CSV 写到 `--output`：保持表头、列序、额外列及所有未改单元格（含引号包裹的逗号与字段内换行），UTF-8 无 BOM、统一 `\n` 行结束；`INPUT` 始终不变。
- 单事务在 `derived_batches` 表以 `DERIVED` 保存新哈希、schema、发现集、`SOURCE` 批次 ID 以及按身份升序的决定/提案快照：逐项保存决定的 `action`、修剪后的 `reason`、决定绑定的完整原发现，以及提案绑定的完整原发现与 PATCH（均取自存储内容，不从当前发现反推）。同一 `DERIVED` 且内容完全相同幂等返回 0（仍重新生成输出）；内容不同退出 3 且不触碰旧输出。
- 退出 0 表示应用与重审计成功（无论重审计是否还有发现，审计结果以 JSONL 的 summary 为准）。失效决定、非法修正以及读写、数据库、审计错误一律退出 2：替换前先保存 CSV 与报告各自的原有字节及“原先不存在”状态，任一暂存、替换或事务提交失败都会回滚 `DERIVED`，并把两个输出恢复到调用前的字节或不存在状态（已成功替换的也撤回），同时清理本次临时文件与恢复备份；若恢复本身也失败，stderr 会同时说明原失败与恢复失败，绝不宣称成功。

## reconcile-fulfillments

在同一 schema 下对订单 CSV 与履约 CSV 做数量对账：

```bash
python3 -m ops_workbench reconcile-fulfillments \
  --schema '{"order_id":["oid"],"sku":["sku"],"qty":["qty"],"status":["status"],"updated_at":["updated_at"]}' \
  orders.csv fulfillments.csv [--output report.jsonl]
```

- schema、BOM、表头、列映射、行宽与五字段校验沿用 `audit-orders`（`order_id`、`sku` 修剪）；`ORDERS.status` 限 `open|cancelled`，`FULFILLMENTS.status` 限 `shipped|cancelled`。
- schema、编码、CSV、表头、行宽或字段值错误一律退出 2（原因写入 stderr，不生成报告，也不产生 `invalid` 记录）。
- 按 `(order_id, sku)` 取并集：`O` 为 ORDERS `open` 行 qty 之和，`F` 为 FULFILLMENTS `shipped` 行 qty 之和，`cancelled` 行不计入。按键升序输出 `["reconcile",order_id,sku,O,F,outcome]`。
- `outcome` 唯一：`0/0` 为 `cancelled-only`，`0/F>0` 为 `orphan-fulfillment`，`O>0/0` 为 `no-fulfillment`；均正时 `F=O` 为 `balanced`、`F<O` 为 `under`、`F>O` 为 `over`。
- 末行 `["summary",g,c,H1,H2]`：`g` 为组数，`c` 以六种 outcome 为键、值为对应组数，`H1`/`H2` 为两输入原始字节的 SHA-256（64 字符小写十六进制）。空结果只输出 summary 行。
- 成功退出 0；默认写 stdout，指定 `--output O` 时报告完整生成后原子替换，失败保留旧文件并清理临时文件。

仅使用 Python 标准库。
