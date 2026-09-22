# 运营数据质量与对账工作台

用于本地运营数据检查与对账的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m ops_workbench --help
python3 -m ops_workbench --version
python3 -m unittest discover -s tests -v
```

无参数显示帮助，未知参数以非零状态退出。

## reconcile 子命令

将订单行与履约发货记录对账，输出 JSON Lines 报告：

```bash
python3 -m ops_workbench reconcile \
  --orders orders.csv \
  --fulfillments fulfillments.csv \
  --output report.jsonl
```

- `--orders O`：UTF-8 逗号 CSV，表头为
  `order_id,line_id,sku,ordered_qty,status`，数量为正整数，
  `status` 为 `open` 或 `cancelled`，`(order_id, line_id)` 唯一。
- `--fulfillments F`：UTF-8 逗号 CSV，表头为
  `shipment_id,order_id,line_id,sku,shipped_qty`，数量为正整数，
  `shipment_id` 唯一。
- `--output R`：生成 JSONL 文件，每行一条记录，字段依次为
  `kind,order_id,line_id,ordered_qty,shipped_qty,difference,evidence`；
  `evidence` 为 `[文件名, 行号]` 元组，按订单行到计入的发货行的顺序排列。

记录类型：

- 每个订单键输出一条主记录，`shipped_qty`（S）只合计相同 SKU 的发货：
  - `open`：`ordered_qty - S` 等于 0 / 大于 0 / 小于 0 时，`kind` 分别为
    `matched` / `under_shipped` / `over_shipped`。
  - `cancelled`：S 为 0 时 `matched`，S 大于 0 时 `cancelled_but_shipped`。
- 每条 SKU 不同的发货单独输出 `sku_conflict`，且不计入 S。
- 找不到对应订单键的发货输出 `missing_order`。

主记录按 `(order_id, line_id)` 排序，孤立发货按 `shipment_id` 排序，
置于报告末尾。

任一输入文件不合规时：进程以非零状态退出，stderr 报告文件名与可定位的行号，
且输出文件不会被创建或修改（写入采用临时文件原子替换）。
