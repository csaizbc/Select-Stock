# Tomorrow Stock List

一个独立的小工具：每天收盘后用 Tushare 生成下一交易日参考股票列表，并输出一个可离线打开、可转发的单文件 HTML。

## 功能

- 选择股票池：全部 A 股、主板、创业板、科创板、北交所、沪市、深市
- 过滤新股：不过滤、上市满 6 个月、1 年、2 年、3 年
- 过滤 ST
- 过滤近 20 / 40 / 60 / 90 个交易日内有停牌记录的股票
- 过滤没有最新行情的股票
- 按申万 2021 二级行业查看和筛选
- 搜索股票代码或名称
- 按最新交易日涨跌幅、最新收盘价或筹码集中度排序
- 查看行业涨跌图：按当前显示列表的申万二级行业平均涨跌幅从高到低排列
- 查看通过股票、剔除股票或全部股票
- 导出当前列表 CSV

## 使用

安装依赖：

```bash
pip install -r requirements.txt
```

生成 HTML：

```bash
python3 generate.py
```

指定数据日期：

```bash
python3 generate.py --as-of 20260622
```

默认输出：

```text
dist/tomorrow_stock_list.html
output/
```

这个 HTML 是单文件，双击即可打开，不需要数据库或网络。

## 数据口径

- 股票基础、行情、停牌、改名 ST 状态来自 Tushare。
- 筹码集中度来自 Tushare `cyq_perf`，按 `(cost_85pct - cost_15pct) / cost_50pct * 100` 计算，数值越小表示 70% 主要筹码成本区间越集中；如果数据日期当天筹码尚未更新，会回退到最近 5 个交易日内有 `cyq_perf` 数据的交易日。
- 行业分类使用申万 2021 二级行业。
- 行业字典来自 `index_classify(level="L2", src="SW2021")`。
- 个股行业归属来自 `index_member_all(l2_code=..., is_new="Y/N")`。
- 对数据日期仍在 `in_date <= data_date` 且 `out_date` 为空或 `out_date >= data_date` 的记录，视为当日有效行业归属。
- 如果个股没有匹配到申万二级行业，行业字段显示为 `未分类`。

## 本地输出

每次运行会生成完整本地产物：

- `output/RUN_REPORT.md`：本次运行报告
- `output/all_stocks.csv`：全量股票清单
- `output/default_passed.csv`：默认条件通过清单
- `output/default_excluded.csv`：默认条件剔除清单
- `output/default_passed_top_gain.csv`：默认通过股票涨幅前 100
- `output/default_passed_top_loss.csv`：默认通过股票跌幅前 100
- `output/industries/`：按申万二级行业拆分的默认通过清单
- `output/pools/`：按全部 A 股、主板、创业板、科创板、北交所、沪市、深市拆分的清单
- `output/industry_return_summary.csv`：按申万二级行业统计的默认通过股票平均涨跌幅
- `output/summary.json`：汇总统计
- `output/payload.json`：HTML 使用的完整数据

## 自动更新

仓库里带了 GitHub Actions 模板：

```text
.github/workflows/update.yml
```

把这个工程放到 GitHub 后，在仓库 Settings 里添加 `TUSHARE_TOKEN` secret，即可手动运行生成并提交新的 HTML。

当前推荐用 Vercel Cron 调用仓库里的 API：

```text
api/trigger_github_action.js
vercel.json
```

Vercel 环境变量：

- `GITHUB_DISPATCH_TOKEN`：有本仓库 Actions 读写权限的 GitHub token
- `CRON_SECRET`：保护触发入口的随机密钥

`vercel.json` 里配置为 10 条工作日定时，分别对应北京时间 15:00-00:59 每小时触发一次。Vercel Hobby 计划会在对应小时内触发；GitHub Actions 另有工作日北京时间 20:10 的备用定时，用于补救 Vercel 触发失败或 Tushare 筹码数据延迟。
