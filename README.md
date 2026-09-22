# 运营数据质量与对账工作台

用于本地运营数据检查与对账的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m ops_workbench --help
python3 -m ops_workbench --version
python3 -m unittest discover -s tests -v
```

无参数显示帮助，未知参数以非零状态退出。

## adjust-stock

```bash
python3 -m ops_workbench adjust-stock SNAPSHOT ADJUSTMENTS REPORT
```

- `SNAPSHOT`：UTF-8 CSV，表头 `warehouse,sku,on_hand,updated_at`；`(warehouse, sku)` 唯一，`on_hand` 为非负整数，`updated_at` 为含时区的 RFC3339 时间。
- `ADJUSTMENTS`：UTF-8 CSV，表头 `id,warehouse,sku,expected,delta,reason,occurred_at`；`id` 唯一，`expected` 为非负整数，`delta` 为非零整数，`occurred_at` 为含时区的 RFC3339 时间。
- 所有字段去首尾空白；`warehouse`、`sku`、`id`、`reason` 非空。调整按 `occurred_at` 换算 UTC 后、`id` 升序应用；只能引用快照已有键，其时刻不得早于该键当前 `updated_at`，`expected` 须等于应用前库存，应用后库存不得为负，否则整批失败。
- `REPORT`：JSON 对象，含 `stock`（按业务键排序，库存为最终值，时间取最后调整时刻或原值）与 `audit`（按应用序，含调整原字段及 `before`、`after`）；数值为 JSON 整数，所有时间输出 UTC `Z`。
- 无效输入或规则冲突以非零状态退出，stderr 含文件、数据行号与原因，旧 `REPORT` 不变；成功时原子替换 `REPORT` 且无终端输出。`REPORT` 不得与输入同路径。
