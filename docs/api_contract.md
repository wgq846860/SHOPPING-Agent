# API说明

公开协议版本：`api.v0.1`。所有接口由本地FastAPI提供，金额为整数分，币种为CNY。模拟数据状态随响应与页面展示。

## 传统购物

- `GET /api/health`：查看配置、数据与服务状态，不返回密钥。
- `GET /api/products`：分页商品列表，可按搜索词、品牌和支持的条件筛选。
- `GET /api/products/{product_id}`：商品详情，包括规格及字段来源。
- `GET /api/products/{product_id}/offers`：某商品的商家报价和具体购买方案。
- `GET /api/merchants/{merchant_id}`：商家详情与服务条件。

实际路由参数和校验类型见后端OpenAPI：服务启动后访问 `/docs`。

## Agent Shopping

`POST /api/agent-shopping` 仅接受JSON对象：

```json
{"query":"800元以内的无线降噪耳机，通勤使用，续航越长越好"}
```

`query` 去首尾空白后必须为1–2000字符。客户端不能提交SQL、候选商品、价格或排序权重覆盖。每次请求独立。

后端将需求转换为 `ShoppingIntent`：包含品类、到手价预算、硬约束、软偏好、配送与售后要求、澄清项及不支持的要求。未表达的硬条件为null，不自动视为false；有货要求固定开启。原句冲突先澄清；设备口型、续航和重量的口径不明确时不猜测。

| 响应字段 | 含义 |
| --- | --- |
| status | ok / partial / no_results / needs_clarification |
| intent | 经校验的结构化需求，供用户核对 |
| recommendations | 最多3个不同商品及各自具体商家购买方案 |
| decision_summary | 候选、合格报价、返回数量、比较范围与结果说明 |
| filters_applied | 实际执行的过滤步骤 |
| ranking_weights | product与offer两组权重，各自归一化 |
| mode | llm / rule_based_demo |
| algorithm_version | 实际排序版本或null |
| warnings | 模拟来源、未知指标与受控回退说明 |
| meta | 请求与数据版本信息 |

`ok` 返回3项，`partial` 返回1–2项，`no_results` 返回0项；`needs_clarification` 不继续推荐。

每条推荐包含商品摘要、所选Offer及连接模式、到手价组成、库存、配送窗、有效退货与保修、所用规格、商品与报价分解、推荐理由、优缺点和比较。解释的 `fact_refs` 指向同次决策中的事实；语言解释不能改变价格、商品、商家或顺序。

硬约束由Python/SQLite执行；来源未知不视为满足。软指标缺失按中性值处理并明确提示，不把未知当作0或最佳值。总价包含商品价、运费、必要配件，扣除确定即时优惠。

## 运行模式与错误

- `rule_based_demo` 不调用模型。
- `llm` 实际调用配置的模型，并经购物工具取得请求绑定的后端记录。
- 缺少模型密钥/完整购物工具桥配置返回503；模型输出无效返回502；超时返回504。解析失败不会静默切换规则模式。
- 仅语言解释失败时可保留后端推荐并附 `EXPLANATION_FALLBACK`。
- 非法请求返回受控错误；数据库未就绪时返回数据不可用状态。

内部 `/api/internal/tools/*` 路由需要服务端工具桥令牌，仅供本地Harness使用。默认只监听 `127.0.0.1`。
