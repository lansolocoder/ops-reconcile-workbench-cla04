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

仅使用 Python 标准库，不会创建业务数据文件。
