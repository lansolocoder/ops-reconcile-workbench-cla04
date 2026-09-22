# 运营数据质量与对账工作台

用于本地运营数据检查与对账的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m ops_workbench --help
python3 -m ops_workbench --version
python3 -m unittest discover -s tests -v
```

无参数显示帮助，未知参数以非零状态退出。

## 对账

```bash
python3 -m ops_workbench reconcile --orders O.csv --fulfillments F.csv --output R.jsonl
```

- `O.csv`：UTF-8 逗号 CSV，表头 `order_id,line_id,sku,ordered_qty,status`；`ordered_qty` 为正整数，`status` 为 `open` 或 `cancelled`，`(order_id, line_id)` 唯一。
- `F.csv`：UTF-8 逗号 CSV，表头 `shipment_id,order_id,line_id,sku,shipped_qty`；`shipped_qty` 为正整数，`shipment_id` 唯一。
- `R.jsonl`：每行一个 JSON 对象，字段依次为 `kind, order_id, line_id, ordered_qty, shipped_qty, difference, evidence`；`evidence` 为 `[文件名, 行号]` 元组列表。

每个订单键输出一条主行：`shipped_qty` 仅合计同 SKU 的发货，`difference = ordered_qty - shipped_qty`。`open` 订单按差额为 0 / 正 / 负分为 `matched` / `under_shipped` / `over_shipped`；`cancelled` 订单按发货合计为 0 / 正分为 `matched` / `cancelled_but_shipped`。异 SKU 发货不计入合计，每条另输出 `sku_conflict`；无对应订单的发货输出 `missing_order`。报告按订单键排序，孤立发货按 `shipment_id` 排序。

输入不合规（缺列、数量非正整数、状态非法、键重复、文件不可读等）时以非零状态退出，stderr 报告文件与行号，且不创建、不修改输出文件。
