// =============================================================================
// S3.1 购物工具插件 — Node 薄桥接（deepseek_harness_integration.md §4）
// -----------------------------------------------------------------------------
// 注册三个购物工具，全部转发到本地 FastAPI 的固定内部入口
// /api/internal/tools/*（X-Agent-Bridge-Token 鉴权）：
//   - recommend_from_intent(intent)：组合工具——检索/硬过滤/两类排序必经，
//     模型不自行算总价或改排序；intent 的最终校验在服务端 Pydantic 完成。
//   - get_product_details(product_id)：只读商品详情（含 unknown 字段）。
//   - get_product_offers(product_id, mode_id?)：只读报价与有效服务条件。
// 桥地址/令牌来自插件配置（由 harness_client.compose_shopping_overlay 注入，
// 固定派生自服务监听地址），模型不可指定任意地址。
// config.selfTest 存在时在注入（apply）阶段做一次离线自检并写报告 JSON，
// 供 scripts/verify_s31.py 取证（不发送任何模型请求）。
// =============================================================================

const { writeFileSync } = require('node:fs');

const BRIDGE_TIMEOUT_MS = 20000;

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

async function callBridge(config, name, args, signal) {
  const response = await fetch(`${config.bridgeOrigin}/api/internal/tools/${name}`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-agent-bridge-token': String(config.bridgeToken || ''),
    },
    body: JSON.stringify(args),
    signal,
  });
  let data = null;
  try {
    data = await response.json();
  } catch (_error) {
    data = null;
  }
  if (!response.ok) {
    const message = data && data.error && data.error.message
      ? data.error.message
      : `bridge ${name} http ${response.status}`;
    const error = new Error(message);
    error.isBridgeError = true;
    error.status = response.status;
    throw error;
  }
  return data;
}

function registerShoppingTool(ctx, config, spec) {
  ctx.tools.register({
    name: spec.name,
    description: spec.description,
    parameters: spec.parameters,
    output: {
      // 决策记录/实体字段由服务端定义并校验；此处只声明对象形态
      schema: { type: 'object' },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute(args, exec) {
      exec.signal.throwIfAborted();
      if (!isPlainObject(args)) {
        throw new TypeError(`${spec.name}: arguments must be an object`);
      }
      return callBridge(config, spec.name, spec.validate(args), exec.signal);
    },
  });
}

const SPECS = [
  {
    name: 'recommend_from_intent',
    description:
      '按受校验的购物意图执行检索、硬约束过滤、商品排序与报价排序，'
      + '返回可查证的决策记录（候选数、过滤统计、排序权重与前若干方案）。',
    parameters: {
      type: 'object',
      properties: {
        intent: {
          type: 'object',
          description: 'ShoppingIntent 对象；服务端按严格 schema 校验',
        },
        decision_request_id: {
          type: 'string', minLength: 32, maxLength: 32,
          description: '完整购物请求提供的绑定ID；调用时原样复制',
        },
      },
      required: ['intent'],
      additionalProperties: false,
    },
    validate: (args) => {
      if (!isPlainObject(args.intent)) {
        throw new TypeError('recommend_from_intent: intent must be an object');
      }
      const payload = { intent: args.intent };
      if (args.decision_request_id !== undefined) payload.decision_request_id = args.decision_request_id;
      return payload;
    },
  },
  {
    name: 'get_product_details',
    description: '读取单个商品的完整字段与指标（未知字段保持 null，不虚构）。',
    parameters: {
      type: 'object',
      properties: {
        product_id: { type: 'string', minLength: 1, maxLength: 64 },
      },
      required: ['product_id'],
      additionalProperties: false,
    },
    validate: (args) => ({ product_id: args.product_id }),
  },
  {
    name: 'get_product_offers',
    description: '读取单个商品的全部报价与有效退货/保修条件（含缺货报价）。',
    parameters: {
      type: 'object',
      properties: {
        product_id: { type: 'string', minLength: 1, maxLength: 64 },
        mode_id: { type: 'string', minLength: 1, maxLength: 64 },
      },
      required: ['product_id'],
      additionalProperties: false,
    },
    validate: (args) => {
      const payload = { product_id: args.product_id };
      if (args.mode_id !== undefined) payload.mode_id = args.mode_id;
      return payload;
    },
  },
];

exports.name = 'agentic-shopping-tools';
exports.inject = ['tools'];

exports.apply = async function (ctx, config) {
  for (const spec of SPECS) {
    registerShoppingTool(ctx, config, spec);
  }

  if (!config || !config.selfTest) return;
  // 离线自检（仅 verify 脚本使用）：经运行时工具服务执行合法调用；本地
  // 形状校验拒绝非法参数；另经 HTTP 直接证明服务端对"本地可过、语义非法"
  // 的意图返回 422（审验 §3：isError 布尔不能证明服务端收到了该调用）。
  // 证据写报告 JSON（全程无模型回合）。
  const report = {
    tools: ctx.tools.schemas().map((t) => t.name).sort(),
    valid_call: null,
    invalid_input_rejected: null,
    server_invalid_intent_rejected: null,
    unauthorized_rejected: null,
  };
  const callTool = (args) => ctx.tools.execute({
    callId: `selftest-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    name: 'recommend_from_intent',
    arguments: args,
    signal: AbortSignal.timeout(BRIDGE_TIMEOUT_MS),
  });
  const valid = await callTool({ intent: config.selfTest.intent });
  if (valid.isError) throw new Error(`shopping tool valid call failed: ${JSON.stringify(valid.value)}`);
  report.valid_call = valid.value;
  const invalid = await callTool({ intent: 'not-an-object' });
  report.invalid_input_rejected = invalid.isError === true;
  try {
    // 本地形状校验可过（intent 是对象），但 stock_required=false 在服务端
    // 被 Pydantic 拒绝 → 证明非法意图确实到达服务端并被 422 拒绝
    await callBridge(
      config,
      'recommend_from_intent',
      { intent: { category: 'headphones', hard_constraints: { stock_required: false } } },
      AbortSignal.timeout(BRIDGE_TIMEOUT_MS),
    );
  } catch (error) {
    report.server_invalid_intent_rejected = error.isBridgeError === true && error.status === 422;
  }
  try {
    await callBridge(
      { ...config, bridgeToken: 'wrong-token' },
      'recommend_from_intent',
      { intent: config.selfTest.intent },
      AbortSignal.timeout(BRIDGE_TIMEOUT_MS),
    );
  } catch (error) {
    report.unauthorized_rejected = error.isBridgeError === true && error.status === 403;
  }
  writeFileSync(config.selfTest.reportPath, JSON.stringify(report), 'utf8');
};
