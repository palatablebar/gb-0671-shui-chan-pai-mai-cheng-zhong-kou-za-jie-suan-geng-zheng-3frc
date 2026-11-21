# 东港水产拍卖结算

给水产市场结算员处理双次称重、扣杂计价、结算更正与应付复核。

七个模块：称重采信、货品换算、毛皮配对、扣杂竞价、更正裁决、应付对账、结算归档。每个导航页均可重跑同一批数据，隔离行保留编号、原值、行号和原因码。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

浏览器访问 http://localhost:18240 。默认载入现场脏批，包含53条逻辑输入、重复表头、缺列、空值、重复冲突、缺皮重、毛重小于皮重等问题。切换“核验清批”或“旧编码批”后重跑。进入“更正裁决”核对票00001：原应付1168.50CNY，更正后1285.35CNY，已付1168.50CNY，仅待付116.85CNY。

API：各模块 `POST /api/<导航英文名>`；完整运行 `POST /api/runs`，JSON参数可用 `dataset` 或 `csv_text`。查询历史 `GET /api/runs`，复核 `POST /api/runs/{run_id}/review`，传入当前 `revision`。封存下载只有 `GET /api/export/{run_id}` 一个入口。

## 同批次补发与日结封存

上游常沿用同一批次号补发称重成交文件。系统不覆盖已采用结果，也不为补发另起可付账批次，而是把补发包登记进原批次：

1. `POST /api/runs` 携带 `batch_id`（或 `POST /api/batches/{batch_id}/import`）导入补发包。新批次直接采用首包；已有批次则登记为**待确认**源包，运行本身独立保存。
2. 日结页（结算归档）显示采用源包版本、每份补发与采用版的逐票比对：变化票、修复票、未结算票，以及无法对应而暂缓的票。
3. `POST /api/batches/{batch_id}/packages/{package_id}/confirm`，传 `digest` 确认补发包内容。
4. `POST /api/batches/{batch_id}/packages/{package_id}/adopt`，传批次 `revision` 切换到补发包；失败返回409，旧采用版和已封存结果不动。
5. `POST /api/batches/{batch_id}/seal`，传批次 `revision` 封存当前采用版；同一采用版重试返回同一封存结果，不新增结算。

封存前会暂缓：补发包未确认、已确认补发包与采用版有差异但尚未采用或退回、同一票在版本间无法对应、采用版自身不可归档。`GET /api/batches/{batch_id}` 可核对源包版本、变化票和暂缓票；`GET /api/batches` 列出批次。封存包内的 `batch.json` 保留全部源包摘要、事件和比对证据。

同输入和规则摘要生成相同批号，不新增重复记录；复核版本改变后旧版本提交返回409。SQLite保存在命名卷。可使用新卷名称重建演示数据，原卷保留。

定义见 `docs/DATA-DICTIONARY.md`，模块与测试映射见 `docs/DEPTH.md`。源码在 `app/`，业务样例在 `sample_data/`，测试在 `tests/`。不连接实际秤台和支付渠道。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，规则未变时批号不变。切换另一示例则创建或复用另一批次。
