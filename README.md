# 运营数据质量与对账工作台

用于本地运营数据检查与对账的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m ops_workbench --help
python3 -m ops_workbench --version
python3 -m ops_workbench adjust-stock SNAPSHOT ADJUSTMENTS REPORT
python3 -m ops_workbench replay-stock BASE ADJUSTMENTS REPORT
python3 -m unittest discover -s tests -v
```

## 命令

- 无参数：显示总帮助（列出全部命令）；未知参数以非零状态退出。
- `adjust-stock SNAPSHOT ADJUSTMENTS REPORT`：读取快照与调整两个 UTF-8 CSV，
  将全部调整作为一个原子批次应用，并写出 JSON 报告。
- `replay-stock BASE ADJUSTMENTS REPORT`：以 `adjust-stock` 或
  `replay-stock` 写出的 JSON 报告（BASE）为可信库存起点，合并调整 CSV 并
  写出新的 JSON 报告。

## adjust-stock 输入格式

快照 CSV 表头：`warehouse,sku,on_hand,updated_at`
调整 CSV 表头：`id,warehouse,sku,expected,delta,reason,occurred_at`

规则：

- 所有字段先去首尾空白；`warehouse`、`sku`、`id`、`reason` 非空。
- 快照键 `(warehouse, sku)` 唯一，调整 `id` 唯一。
- `on_hand`、`expected` 为非负整数；`delta` 为非零整数。
- 时间字段须为带时区的 RFC3339，内部统一换算为 UTC。
- 调整只能引用快照中已有的键；按 `occurred_at` 的 UTC 时刻升序、
  同时刻按 `id` 升序应用。
- 每条调整的时刻不得早于该键当前的 `updated_at`；`expected` 必须等于
  应用前库存；应用后库存不得为负。任一规则冲突则整批失败。

## adjust-stock 输出

`REPORT` 为 JSON 对象，含 `stock` 与 `audit` 两个数组：

- `stock`：沿用快照字段，按 `(warehouse, sku)` 排序；库存为最终值，
  `updated_at` 取该键最后一条调整的时刻（无调整时保留原值）。
- `audit`：按应用序排列，含每条调整的原字段以及 `before`、`after`。
- 数值均为 JSON 整数；所有时间以 UTC `Z` 输出。

成功时原子替换 `REPORT` 且终端无输出；`REPORT` 不得与任一输入文件同路径。
输入无效或规则冲突时以非零状态退出，stderr 含文件名、数据行号与原因，
且已有的旧报告保持不变。

## replay-stock 输入格式

BASE 为 `adjust-stock` 或 `replay-stock` 的 JSON 输出，须符合现有输出契约：

- `stock`、`audit` 字段及类型与 `adjust-stock` 输出一致；业务键
  `(warehouse, sku)` 与 audit `id` 各自唯一；数值为 JSON 整数，时间为
  UTC `Z`。库存以 `stock` 为准，不从 `audit` 反算。
- 可选 `batches`：互异的 64 位小写十六进制字符串数组（调整文件原始字节的
  SHA-256 摘要）。

ADJUSTMENTS 沿用现有调整 CSV 契约。按该契约规范化后：

- 调整 `id` 若已在 BASE 的 `audit` 中：七个原输入字段
  （`id, warehouse, sku, expected, delta, reason, occurred_at`）全同则
  跳过；任一不同则整批失败。
- 其余行按 `occurred_at` 的 UTC 时刻升序、同时刻按 `id` 升序应用：
  业务键须存在于 BASE.stock；时刻不得早于该键当前 `updated_at`；
  `expected` 必须等于应用前库存；应用后库存不得为负。

## replay-stock 输出

`REPORT` 为 JSON 对象，含 `stock`、`audit` 与 `batches`：

- `stock`：按 `(warehouse, sku)` 排序，字段同 `adjust-stock`。
- `audit`：保持 BASE.audit 的原顺序，并在末尾追加本次应用项（含 `before`、
  `after`）；跳过的已存在 id 不重复出现。
- `batches`：保留 BASE.batches 原值，仅在摘要不存在时追加 ADJUSTMENTS
  原始字节的 SHA-256 小写值。
- 重复提交同一批（摘要已在 `batches`）不得改变库存，也不增加 audit、
  batches 条目。

数值均为 JSON 整数；所有时间以 UTC `Z` 输出。成功时原子替换 `REPORT` 且
终端无输出；`REPORT` 不得与任一输入文件同路径。BASE 无效、id 冲突或应用
失败时以非零状态退出，stderr 含文件名、适用 CSV 数据行号与原因，且已有
的旧报告保持不变。
