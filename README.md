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

`--db` 与 `--batch` 必须成对出现。完整扫描结束后（退出码仍为 0 或 1），在 SQLite 数据库 `DB` 中以单事务保存：批次 ID、输入原始字节 SHA-256、解析后的 schema、除 summary 外的发现集。

- 同一 ID 再次运行时，若哈希、schema、发现集三者均相同，则幂等返回报告（不写库）。
- 任一不同则退出 3，stderr 说明冲突的字段及两侧取值；数据库与既有输出文件均不改变。
- 输入、数据库或写出错误退出 2，不改写任何批次。

## diff-audits

解释两个已保存批次的差异：

```bash
python3 -m ops_workbench diff-audits --db batches.db OLD NEW [--output diff.jsonl]
```

发现身份：`invalid` 取 `["invalid",记录号,字段]`；`duplicate`/`conflict` 取 `[类型,order_id,sku]`。

- 仅 NEW 有的输出 `["added",身份,NEW发现]`；仅 OLD 有的输出 `["resolved",身份,OLD发现]`；同身份但完整发现不同输出 `["changed",身份,OLD发现,NEW发现]`；完全相同则省略。
- 结果按身份逐项升序，末行为 `["summary",added数,resolved数,changed数,OLD哈希,NEW哈希]`。
- 任一批次不存在退出 2 且无部分结果；默认写 stdout，指定 `--output O` 时沿用原子替换保护。

仅使用 Python 标准库，不会创建业务数据文件。
