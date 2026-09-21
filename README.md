# 运营数据质量与对账工作台

用于本地运营数据检查与对账的命令行项目。

需要 Python 3.12，无第三方依赖（仅标准库）。在仓库根目录运行：

```bash
python3 -m ops_workbench --help
python3 -m ops_workbench --version
python3 -m unittest discover -s tests -v
```

无参数显示帮助，未知参数以非零状态退出。

## audit-orders

按列映射模式审计订单 CSV，逐行输出 JSON Lines 报告：

```bash
python3 -m ops_workbench audit-orders --schema S INPUT [--output O]
```

- `S`：UTF-8 JSON 对象（内联 JSON 文本，或 JSON 文件路径），只能含
  `order_id`、`sku`、`qty`、`status`、`updated_at` 五个键；每个值是非空的
  候选列名字符串数组。每个逻辑字段必须在 `INPUT` 表头中恰好命中一列，且五个
  字段所选列互异。
- `INPUT`：UTF-8 CSV 文件（允许开头 BOM）。表头不得为空、不得有重名列，允许
  存在与五个逻辑字段无关的额外列。
- 数据行校验（按修剪后的值）：
  - `order_id`、`sku`：非空；
  - `qty`：匹配 `[1-9][0-9]*`；
  - `status`：仅 `open` 或 `cancelled`；
  - `updated_at`：带时区的 ISO 8601、秒精度时间。
- 五个字段全部有效的行按修剪后的 `order_id` + `sku` 分组：同组多于一行报
  `duplicate`；若各行 qty 数值、status 或时间文本不全一致，再报 `conflict`。

报告每行是一个 JSON 数组：

- 行错误：`["invalid", 记录号, 逻辑字段, 原值]`（`原值` 为修剪前单元格文本）；
- 组错误：`["duplicate"|"conflict", [order_id, sku], [升序记录号...]]`；
- 末行：`["summary", 数据行数, 发现数, INPUT 原始字节 SHA-256]`。

发现按“涉及的最小记录号、类型、字段”升序排列；记录号以表头为 1。

退出码：

- `0`：无发现；`1`：有发现；
- `2`：模式、编码、CSV 或表头错误——原因写入 stderr，不生成报告；
- `--output O` 时报告先完整写入同目录临时文件，成功后原子替换 `O`；失败保留
  旧 `O` 并清理临时文件。
