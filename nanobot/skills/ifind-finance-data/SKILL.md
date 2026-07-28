---
name: ifind-finance-data
description: 使用同花顺 iFinD 查询并核验 A股、港美股、基金、债券、宏观、行业、指数、新闻和公告等结构化金融数据。适用于证券研究、财报分析、行情与估值查询、资金和行业比较；服务未配置或返回错误时说明缺口，并优先切换到团队绑定的其他结构化金融数据源。
metadata:
  nanobot:
    requires:
      bins:
        - node
      files:
        - scripts/call-node.js
---

# 同花顺 iFinD 金融数据

使用随 Skill 提供的 `scripts/call-node.js` 调用同花顺 MCP 服务。运行时已经通过
Skill 注册表提供当前 Skill 的准确目录；直接使用该目录，禁止通过 `find_files`、
`grep`、`list_dir` 或扫描 Home 目录寻找另一份 Skill。

## 执行规则

1. 先根据证券市场选择服务：
   - A股：`stock`
   - 港股或美股：`global_stock`
   - 基金：`fund`
   - 债券：`bond`
   - 宏观或行业指标：`edb`
   - 新闻或公告：`news`
   - 指数或板块：`index`
2. 第一次调用前执行一次配置检查：

   ```bash
   node scripts/call-node.js --check
   ```

3. 配置检查失败，或任一查询首次出现硬失败、内层 `call failed`、429、权限错误
   或重复查询熔断时，立即停止本轮 iFinD 调用。不要搜索其他 Skill 副本，也不要
   扫描项目外目录；如果当前团队还绑定了聚源 MCP，立即切换到可用的
   `mcp_juyuan_...` 工具。聚源仍有缺口时使用已有可信数据、交易所公告、公司 IR、
   监管披露或 `web_search` 补齐并明确降级。
4. 查询时合并同一主体的相关指标，避免逐字段反复请求。运行时要求切换来源时不得
   继续重试、轮换可比公司或轻微改写命令来规避熔断。
5. 检查返回 JSON 的 `ok` 字段以及内层 `data/content/text`；外层成功但内层包含
   `call failed`、`429` 或权限错误时仍视为失败。
6. 关键结论用交易所、公司公告或监管披露交叉核验。不得把预测或媒体推测写成已
   发生事实。

## 固定 CLI

```text
node scripts/call-node.js <server_type> <tool_name> '<json_params>'
node scripts/call-node.js list-tools <server_type>
```

示例：

```bash
node scripts/call-node.js stock get_stock_info '{"query":"贵州茅台 600519.SH 日频行情与估值"}'
node scripts/call-node.js stock get_stock_financials '{"query":"贵州茅台 600519.SH 最近五年营收、净利润、ROE和现金流"}'
node scripts/call-node.js global_stock global_stock_quotes '{"query":"小米集团 1810.HK 最新行情与估值"}'
node scripts/call-node.js global_stock global_stock_financial '{"query":"小米集团 1810.HK 最近五年财务数据"}'
node scripts/call-node.js news search_notice '{"query":"小米集团 1810.HK 最新公告","size":20}'
```

## 服务和工具

| 服务 | 常用工具 | 用途 |
|---|---|---|
| `stock` | `search_stocks`、`get_stock_summary`、`get_stock_info`、`get_stock_financials` | A股资料、行情、技术和财务 |
| `global_stock` | `search_global_stocks`、`global_stock_quotes`、`global_stock_financial` | 港美股行情和财务 |
| `fund` | 使用 `list-tools fund` 确认可用工具 | 基金资料和筛选 |
| `bond` | `bond_basic_info`、`bond_market_data`、`bond_financial_data` | 债券和发债主体 |
| `edb` | `get_edb_data` | 宏观和行业指标 |
| `news` | `search_news`、`search_notice` | 新闻和公告 |
| `index` | `index_data`、`sector_data` | 指数和板块 |

`search_edb`、`smart_stock_picking` 等工具可能受账号权限限制。遇到
`Tool not allowed` 时不要循环重试。

## 配置

脚本按以下顺序读取认证信息：

1. `IFIND_AUTH_TOKEN` 环境变量；
2. `IFIND_MCP_CONFIG` 指向的 JSON 文件；
3. Skill 根目录的 `mcp_config.json`。

配置文件格式：

```json
{
  "auth_token": "由用户在本机配置，禁止写入项目或报告"
}
```

不得读取、打印或复制认证内容。内置 Skill 不携带用户凭证。
