(() => {
  "use strict";

  const EXAMPLES = [
    "我想买一副 800 元以内的无线降噪耳机，主要通勤使用，希望续航长一点，不要太重。",
    "预算可以浮动，降噪和佩戴舒适最重要，价格不是第一考虑。",
    "想要真无线耳塞，运动时戴，必须防水，续航要够跑完一次半马。",
    "给玩游戏用，要有麦克风、低延迟，最好在 1000 元以内。",
  ];

  const DIMENSION_LABELS = {
    price: "价格", battery: "续航", weight: "重量", comfort: "佩戴舒适",
    anc: "主动降噪", sound_quality: "音质", call: "通话", gaming: "游戏",
    portability: "便携", commute: "通勤", fit_stability: "佩戴稳固",
    low_leakage: "低漏音", low_latency: "低延迟", shipping: "配送",
    merchant_service: "商家服务", returns: "退货", warranty: "保修",
    wireless: "无线", foldable: "折叠", fast_charge: "快充",
    tone_match: "音色偏好", environment_awareness: "环境音感知",
  };

  const CONSTRAINT_LABELS = {
    wireless: "无线", active_noise_cancelling: "主动降噪", microphone: "麦克风",
    multipoint: "多设备连接", open_ear: "开放式", requires_power: "需要供电",
    opened_return_allowed: "支持开封退货", sweat_use_allowed: "适合运动出汗",
    sub_categories: "品类", allowed_form_factors: "允许形态",
    excluded_form_factors: "排除形态", ear_tip_sizes: "耳塞尺寸",
    min_active_devices: "同时连接设备数", allowed_transports: "连接方式",
    battery_requirement: "续航门槛", weight_requirement: "重量上限",
    device_requirements: "设备兼容", item_condition: "成色",
    delivery_requirement: "配送要求", stock_required: "要求有货",
  };

  const FILTER_LABELS = {
    "budget.max_cents": "到手价预算",
    "hard_constraints.stock_required": "库存",
    "hard_constraints.wireless": "无线能力",
    "hard_constraints.active_noise_cancelling": "主动降噪",
    "hard_constraints.microphone": "麦克风",
    "hard_constraints.multipoint": "多设备连接",
    "hard_constraints.sub_categories": "品类",
    "hard_constraints.allowed_form_factors": "允许形态",
    "hard_constraints.excluded_form_factors": "排除形态",
    "hard_constraints.allowed_transports": "连接方式",
    "hard_constraints.battery_requirement": "续航门槛",
    "hard_constraints.weight_requirement": "重量上限",
    "offer.eligible": "报价资格",
    "hard_constraints.device_requirements": "设备兼容",
    "hard_constraints.item_condition": "成色",
    "hard_constraints.delivery_requirement": "配送要求",
  };

  const VALUE_LABELS = {
    over_ear_headphones: "头戴式耳机", on_ear_headphones: "贴耳式耳机",
    true_wireless_earbuds: "真无线耳塞", wireless_earbuds: "无线耳塞",
    gaming_headsets: "游戏耳机", wired_headphones: "有线耳机",
    over_ear: "头戴式", on_ear: "贴耳式", in_ear: "入耳式", open_ear: "开放式",
    wired_3_5mm: "3.5 mm 有线", usb_audio: "USB 音频", bluetooth: "蓝牙",
    wireless_dongle: "无线接收器", optical_transmitter: "光纤发射器",
    new: "全新", refurbished: "翻新", used: "二手",
  };

  const STATUS_LABELS = {
    ok: "推荐结果",
    partial: "符合条件的候选较少",
    no_results: "没有符合全部条件的候选",
    needs_clarification: "需要先确认需求",
  };

  const OPERATOR_LABELS = {
    eq: "等于", lte: "不高于", gte: "不低于", in: "属于",
    not_in: "不属于", all: "全部满足", compatible: "兼容",
  };

  const CNY = new Intl.NumberFormat("zh-CN", {
    style: "currency", currency: "CNY", minimumFractionDigits: 2,
  });

  function formatMoney(cents) {
    if (!Number.isSafeInteger(cents) || cents < 0) return "未知";
    return CNY.format(cents / 100);
  }

  function normalizeApiError(payload, status) {
    const error = payload && typeof payload.error === "object" ? payload.error : {};
    const message = typeof error.message === "string" && error.message.trim()
      ? error.message.trim()
      : "服务暂时无法完成这次请求，请稍后重试。";
    return {
      code: typeof error.code === "string" ? error.code : "HTTP_ERROR",
      message,
      retryable: error.retryable === true,
      status,
    };
  }

  function createSubmissionHandler({ fetchImpl, onLoading, onSuccess, onError }) {
    let latestRequest = 0;

    return async function submit(query) {
      const requestId = ++latestRequest;
      onLoading(true, query);

      try {
        const response = await fetchImpl("/api/agent-shopping", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query }),
        });

        let payload;
        try {
          payload = await response.json();
        } catch {
          if (requestId === latestRequest) {
            onError({
              code: "INVALID_RESPONSE",
              message: "服务返回了无法识别的内容，请重试。",
              retryable: true,
              status: response.status,
            });
          }
          return;
        }

        if (requestId !== latestRequest) return;
        if (!response.ok) {
          onError(normalizeApiError(payload, response.status));
          return;
        }
        onSuccess(payload);
      } catch {
        if (requestId === latestRequest) {
          onError({
            code: "NETWORK_ERROR",
            message: "暂时无法连接购物服务，请确认服务正在运行后重试。",
            retryable: true,
            status: null,
          });
        }
      } finally {
        if (requestId === latestRequest) onLoading(false);
      }
    };
  }

  function makeElement(doc, tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function appendTextRow(doc, list, label, value) {
    const item = makeElement(doc, "li", "agent-fact-row");
    item.append(
      makeElement(doc, "span", "agent-fact-label", label),
      makeElement(doc, "span", "agent-fact-value", value),
    );
    list.append(item);
  }

  function displayValue(value) {
    if (value === null || value === undefined) return "未知";
    if (typeof value === "boolean") return value ? "是" : "否";
    if (Array.isArray(value)) return value.map(displayValue).join("、");
    if (typeof value === "object") return JSON.stringify(value);
    return VALUE_LABELS[value] || String(value);
  }

  function formatRequirement(key, value) {
    if (key === "battery_requirement") {
      const hours = Number.isFinite(value.min_hours) ? `${value.min_hours} 小时` : "续航";
      return `${hours}（${displayValue(value.basis)}，降噪${displayValue(value.anc_state)}）`;
    }
    if (key === "weight_requirement") {
      return `${value.max_grams} 克（${displayValue(value.basis)}）`;
    }
    if (key === "delivery_requirement") {
      const parts = [value.destination_region, value.deadline_date, value.commitment_type]
        .filter(Boolean);
      return parts.length ? parts.join(" · ") : "已提出配送要求";
    }
    if (key === "device_requirements") {
      return value.map((device) => [device.platform, device.os_version, device.transport]
        .filter(Boolean).join(" · ")).join("；");
    }
    return displayValue(value);
  }

  function addSection(doc, host, title, className = "") {
    const section = makeElement(doc, "section", `agent-detail-section ${className}`.trim());
    section.append(makeElement(doc, "h3", "agent-detail-title", title));
    host.append(section);
    return section;
  }

  function renderIntent(doc, host, intent) {
    const section = addSection(doc, host, "解析意图", "agent-intent-section");
    const facts = makeElement(doc, "ul", "agent-fact-list");
    const maxCents = intent?.budget?.max_cents;
    appendTextRow(
      doc,
      facts,
      "预算口径",
      Number.isSafeInteger(maxCents) ? `到手价不高于 ${formatMoney(maxCents)}` : "未设置预算上限",
    );

    const constraints = intent?.hard_constraints || {};
    for (const [key, value] of Object.entries(constraints)) {
      if (value === null || value === undefined || (Array.isArray(value) && value.length === 0)) continue;
      appendTextRow(
        doc,
        facts,
        CONSTRAINT_LABELS[key] || key,
        formatRequirement(key, value),
      );
    }
    section.append(facts);

    const preferences = Object.entries(intent?.preferences || {})
      .filter(([, value]) => Number.isFinite(value) && value > 0);
    const preferenceSection = addSection(doc, host, "解析出的偏好重要度", "agent-preference-section");
    if (preferences.length) {
      const list = makeElement(doc, "ul", "agent-chip-list");
      for (const [dimension, value] of preferences) {
        const chip = makeElement(doc, "li", "agent-chip");
        chip.append(
          makeElement(doc, "span", "agent-chip-label", DIMENSION_LABELS[dimension] || dimension),
          makeElement(doc, "strong", "agent-chip-value", `${Math.round(value * 100)}%`),
        );
        list.append(chip);
      }
      preferenceSection.append(list);
    } else {
      preferenceSection.append(makeElement(doc, "p", "agent-muted", "没有额外软偏好。"));
    }

    const clarifications = Array.isArray(intent?.clarifications) ? intent.clarifications : [];
    if (clarifications.length) {
      const clarifySection = addSection(doc, host, "待确认", "agent-clarification-section");
      const list = makeElement(doc, "ul", "agent-statement-list");
      for (const item of clarifications) {
        list.append(makeElement(doc, "li", "", item.question || "请补充这项需求。"));
      }
      clarifySection.append(list);
    }
  }

  function formatFilterValue(filter) {
    const value = filter.value;
    if (filter.field === "budget.max_cents" && Number.isSafeInteger(value)) {
      return formatMoney(value);
    }
    if (filter.unit === "CNY_cent" && Number.isSafeInteger(value)) return formatMoney(value);
    return displayValue(value);
  }

  function renderFilters(doc, host, filters) {
    const section = addSection(doc, host, "实际筛选条件", "agent-filter-section");
    if (!Array.isArray(filters) || filters.length === 0) {
      section.append(makeElement(doc, "p", "agent-muted", "没有商品进入硬约束筛选。"));
      return;
    }

    const list = makeElement(doc, "ul", "agent-filter-list");
    for (const filter of filters) {
      const item = makeElement(doc, "li", "agent-filter-row");
      const field = FILTER_LABELS[filter.field] || filter.field || "筛选条件";
      const operator = OPERATOR_LABELS[filter.operator] || filter.operator || "";
      const scope = { product: "商品", offer: "报价", connection_plan: "连接方案" }[filter.scope] || "未知";
      item.append(
        makeElement(doc, "strong", "agent-filter-name", field),
        makeElement(doc, "span", "agent-filter-value", `${operator} ${formatFilterValue(filter)}`.trim()),
        makeElement(doc, "span", "agent-filter-count", `作用范围：${scope} · 排除 ${filter.excluded_count ?? 0} 个商品`),
      );
      if (filter.unknown_excluded_count > 0) {
        item.append(makeElement(doc, "span", "agent-filter-unknown", `其中 ${filter.unknown_excluded_count} 个商品因数据未知而排除`));
      }
      list.append(item);
    }
    section.append(list);
  }

  function renderWeights(doc, host, weights) {
    const section = addSection(doc, host, "实际排序权重", "agent-weight-section");
    const grid = makeElement(doc, "div", "agent-weight-grid");
    for (const [key, title] of [["product", "商品权重"], ["offer", "报价权重"]]) {
      const group = makeElement(doc, "section", "agent-weight-group");
      group.append(makeElement(doc, "h4", "", title));
      const entries = activeWeights(weights?.[key]);
      if (entries.length === 0) {
        group.append(makeElement(doc, "p", "agent-muted", "本次未排序。"));
      } else {
        group.append(makeElement(doc, "p", "agent-weight-note", "仅列出非零权重"));
        const list = makeElement(doc, "ul", "agent-weight-list");
        for (const [dimension, weight] of entries) {
          const item = makeElement(doc, "li", "agent-weight-row");
          item.append(
            makeElement(doc, "span", "", DIMENSION_LABELS[dimension] || dimension),
            makeElement(doc, "strong", "", `${(weight * 100).toFixed(1)}%`),
          );
          list.append(item);
        }
        group.append(list);
      }
      grid.append(group);
    }
    section.append(grid);
  }

  function renderWarnings(doc, host, warnings) {
    if (!Array.isArray(warnings) || warnings.length === 0) return;
    const list = makeElement(doc, "ul", "agent-warning-list");
    for (const warning of warnings) {
      if (typeof warning?.message === "string" && warning.message) {
        list.append(makeElement(doc, "li", "", warning.message));
      }
    }
    if (list.childElementCount) host.append(list);
  }

  function deliveryText(window) {
    if (!window) return "配送时间未知";
    const days = [window.earliest, window.latest].filter(Boolean).join(" 至 ");
    const observation = window.observed_at ? `观测于 ${window.observed_at}` : "观测时点未知";
    const historyNote = `${observation}；不代表当前下单承诺`;
    if (!days) return `配送时间未知（${historyNote}）`;
    const certainty = window.commitment_type === "guaranteed"
      ? "承诺"
      : window.commitment_type === "estimated" ? "预计" : "配送";
    return `历史${certainty} ${days}（${historyNote}）`;
  }

  function renderOffer(doc, host, offer) {
    const block = makeElement(doc, "div", "agent-offer");
    const price = makeElement(doc, "div", "agent-offer-price-block");
    price.append(
      makeElement(doc, "span", "agent-overline", "推荐商家到手价"),
      makeElement(doc, "strong", "agent-offer-price", formatMoney(offer?.landed_price_cents)),
    );
    block.append(price);

    const facts = makeElement(doc, "dl", "agent-offer-facts");
    const returnPolicy = offer?.effective_return_policy || {};
    const warranty = offer?.effective_warranty || {};
    const values = [
      ["商家", offer?.merchant_name || "未知"],
      ["商品价", formatMoney(offer?.price_cents)],
      ["配送费", formatMoney(offer?.shipping_fee_cents)],
      ["配送", deliveryText(offer?.delivery_window)],
      ["库存", Number.isFinite(offer?.stock) ? `${offer.stock} 件` : "未知"],
      ["退货", Number.isFinite(returnPolicy.days) ? `${returnPolicy.days} 天` : "条件未知"],
      ["开封退货", typeof returnPolicy.opened_return_allowed === "boolean" ? (returnPolicy.opened_return_allowed ? "允许" : "不允许") : "未知"],
      ["保修", Number.isFinite(warranty.months) ? `${warranty.months} 个月` : "条件未知"],
    ];
    for (const [label, value] of values) {
      const row = makeElement(doc, "div", "agent-offer-fact");
      row.append(makeElement(doc, "dt", "", label), makeElement(doc, "dd", "", value));
      facts.append(row);
    }
    block.append(facts);

    const policyNotes = [
      ...(Array.isArray(returnPolicy.exceptions) ? returnPolicy.exceptions : []),
      ...(typeof returnPolicy.conditions === "string" ? [returnPolicy.conditions] : []),
      ...(typeof warranty.conditions === "string" ? [warranty.conditions] : []),
    ].filter(Boolean);
    if (policyNotes.length) {
      const notes = makeElement(doc, "ul", "agent-offer-notes");
      for (const note of policyNotes) notes.append(makeElement(doc, "li", "", note));
      block.append(notes);
    }

    const link = makeElement(doc, "a", "agent-detail-link", "查看商品详情与其他报价");
    const detailParams = new URLSearchParams({ id: offer?.product_id || "" });
    if (offer?.mode_id) detailParams.set("mode_id", offer.mode_id);
    link.href = `/product.html?${detailParams.toString()}`;
    block.append(link);
    host.append(block);
  }

  function renderStatements(doc, host, title, statements) {
    const group = makeElement(doc, "section", "agent-statement-group");
    group.append(makeElement(doc, "h4", "", title));
    if (!Array.isArray(statements) || statements.length === 0) {
      group.append(makeElement(doc, "p", "agent-muted", "暂无"));
      host.append(group);
      return;
    }
    const list = makeElement(doc, "ul", "agent-statement-list");
    for (const statement of statements) {
      const item = makeElement(doc, "li", "");
      item.append(makeElement(doc, "p", "", statement?.text || ""));
      const refs = Array.isArray(statement?.fact_refs) ? statement.fact_refs : [];
      if (refs.length) {
        const details = makeElement(doc, "details", "agent-fact-refs");
        details.append(makeElement(doc, "summary", "", "事实引用"));
        details.append(makeElement(doc, "code", "", refs.join(" · ")));
        item.append(details);
      }
      list.append(item);
    }
    group.append(list);
    host.append(group);
  }

  function renderScoreBreakdown(doc, host, breakdown) {
    const details = makeElement(doc, "details", "agent-score-details");
    details.append(makeElement(doc, "summary", "", "查看商品与报价评分贡献"));
    const grid = makeElement(doc, "div", "agent-score-grid");
    for (const [key, title] of [["product", "商品评分"], ["offer", "报价评分"]]) {
      const group = makeElement(doc, "section", "");
      group.append(makeElement(doc, "h4", "", title));
      const components = Array.isArray(breakdown?.[key]) ? breakdown[key] : [];
      if (!components.length) {
        group.append(makeElement(doc, "p", "agent-muted", "无评分分项"));
      } else {
        const list = makeElement(doc, "ul", "agent-score-list");
        for (const component of components) {
          const item = makeElement(doc, "li", "");
          const dimension = DIMENSION_LABELS[component.dimension] || component.dimension || "评分项";
          const contribution = Number.isFinite(component.contribution)
            ? `${component.contribution.toFixed(2)} 分贡献`
            : "贡献未知";
          const weight = Number.isFinite(component.weight) ? ` · 权重 ${(component.weight * 100).toFixed(1)}%` : "";
          const value = component.missing ? "数据缺失，使用基线处理中性值" : `标准化 ${(component.normalized_score * 100).toFixed(1)} 分`;
          item.append(makeElement(doc, "strong", "", dimension), makeElement(doc, "span", "", `${contribution}${weight} · ${value}`));
          if (Array.isArray(component.source_refs) && component.source_refs.length) {
            item.append(makeElement(doc, "code", "", component.source_refs.join(" · ")));
          }
          list.append(item);
        }
        group.append(list);
      }
      grid.append(group);
    }
    details.append(grid);
    host.append(details);
  }

  function renderRecommendation(doc, host, recommendation) {
    const item = makeElement(doc, "article", "agent-recommendation");
    const product = recommendation?.product || {};
    const offer = recommendation?.offer || {};
    const heading = makeElement(doc, "div", "agent-recommendation-head");
    const rank = makeElement(doc, "span", "agent-rank", `#${recommendation?.rank ?? "?"}`);
    const title = makeElement(doc, "div", "agent-recommendation-title");
    title.append(
      makeElement(doc, "p", "agent-overline", product.brand || "耳机"),
      makeElement(doc, "h4", "", product.product_name || product.product_id || "商品信息未知"),
    );
    const score = makeElement(doc, "strong", "agent-score", Number.isFinite(recommendation?.score)
      ? `${recommendation.score.toFixed(1)} 分`
      : "评分未知");
    heading.append(rank, title, score);
    item.append(heading);

    const productFacts = makeElement(doc, "p", "agent-product-meta");
    const meta = [];
    if (Number.isFinite(product.rating)) meta.push(`评分 ${product.rating.toFixed(1)}`);
    if (Number.isFinite(product.review_count)) meta.push(`${product.review_count} 条评价`);
    if (product.form_factor) meta.push(VALUE_LABELS[product.form_factor] || product.form_factor);
    if (offer.mode_id) meta.push(`方案 ${offer.mode_id}`);
    productFacts.textContent = meta.join(" · ");
    item.append(productFacts);

    const columns = makeElement(doc, "div", "agent-recommendation-grid");
    renderOffer(doc, columns, offer);
    const explanations = makeElement(doc, "div", "agent-explanations");
    renderStatements(doc, explanations, "推荐理由", recommendation?.reason);
    renderStatements(doc, explanations, "优点", recommendation?.strengths);
    renderStatements(doc, explanations, "需要权衡", recommendation?.weaknesses);
    renderStatements(doc, explanations, "取舍", recommendation?.trade_offs);
    renderScoreBreakdown(doc, explanations, recommendation?.score_breakdown);
    columns.append(explanations);
    item.append(columns);
    host.append(item);
  }

  function setResultMode(modeNode, mode) {
    const modeLabels = {
      llm: "LLM 意图解析",
      rule_based_demo: "规则演示（不调用模型）",
    };
    modeNode.textContent = modeLabels[mode] || "运行模式未知";
    modeNode.className = "agent-mode";
  }

  function renderDecisionSummary(doc, host, summary, status) {
    const block = makeElement(doc, "div", "agent-decision-summary");
    block.append(makeElement(doc, "p", "agent-decision-message", summary?.message || STATUS_LABELS[status] || "请求已完成"));
    const values = [
      ["合格商品", summary?.candidate_count],
      ["合格报价", summary?.eligible_offer_count],
      ["实际返回", summary?.returned_count],
    ];
    const stats = makeElement(doc, "dl", "agent-summary-stats");
    for (const [label, value] of values) {
      if (!Number.isFinite(value)) continue;
      const cell = makeElement(doc, "div", "agent-summary-stat");
      cell.append(makeElement(doc, "dt", "", label), makeElement(doc, "dd", "", String(value)));
      stats.append(cell);
    }
    block.append(stats);
    host.append(block);
  }

  function renderResponse(doc, elements, payload) {
    const status = STATUS_LABELS[payload?.status] ? payload.status : "no_results";
    elements.resultTitle.textContent = STATUS_LABELS[status];
    setResultMode(elements.mode, payload?.mode);
    elements.result.replaceChildren();

    renderDecisionSummary(doc, elements.result, payload?.decision_summary, status);

    const meta = payload?.meta || {};
    const metaLine = [
      meta.data_version ? `数据批次 ${meta.data_version}` : "",
      payload?.algorithm_version ? `算法 ${payload.algorithm_version}` : "",
    ].filter(Boolean).join(" · ");
    if (metaLine) elements.result.append(makeElement(doc, "p", "agent-meta-line", metaLine));

    renderWarnings(doc, elements.result, payload?.warnings);
    const decisionSections = makeElement(doc, "div", "agent-decision-grid");
    renderIntent(doc, decisionSections, payload?.intent || {});
    renderFilters(doc, decisionSections, payload?.filters_applied);
    renderWeights(doc, decisionSections, payload?.ranking_weights);
    elements.result.append(decisionSections);

    const recommendations = Array.isArray(payload?.recommendations) ? payload.recommendations : [];
    const listSection = addSection(doc, elements.result, "商品与推荐商家", "agent-recommendations-section");
    if (recommendations.length === 0) {
      const emptyText = status === "needs_clarification"
        ? "确认上方问题后，可重新提交完整需求。"
        : status === "no_results"
          ? "当前数据中没有同时满足这些条件的可购方案。可以调整需求后重新查询。"
          : "本次没有返回推荐商品。";
      listSection.append(makeElement(doc, "p", "agent-empty-result", emptyText));
    } else {
      if (status === "partial") {
        listSection.append(makeElement(doc, "p", "agent-muted", `仅返回 ${recommendations.length} 条实际合格结果，没有补足到三条。`));
      }
      const list = makeElement(doc, "div", "agent-recommendations");
      for (const recommendation of recommendations) renderRecommendation(doc, list, recommendation);
      listSection.append(list);
    }

    if (meta.simulated === true) {
      elements.result.append(makeElement(doc, "p", "agent-simulated-note", "模拟数据；价格、库存、配送与售后条件以本次本地演示数据为准。"));
    }
  }

  function activeWeights(weights) {
    return Object.entries(weights || {}).filter(([, value]) => Number.isFinite(value) && value > 0);
  }

  function showStatus(doc, node, message, kind) {
    node.hidden = false;
    node.className = `agent-status agent-status-${kind}`;
    node.replaceChildren(makeElement(doc, "span", "agent-status-message", message));
  }

  function renderError(doc, result, resultTitle, mode, error) {
    resultTitle.textContent = "购物服务暂不可用";
    mode.textContent = "本次请求未完成";
    mode.className = "agent-mode agent-mode-error";
    result.replaceChildren();
    const state = makeElement(doc, "div", "agent-error-state", error.message);
    if (error.code) state.append(makeElement(doc, "code", "agent-error-code", error.code));
    if (error.retryable) state.append(makeElement(doc, "p", "", "可以检查服务状态后重新提交。"));
    result.append(state);
  }

  function initAgentPage(doc, fetchImpl) {
    const form = doc.getElementById("agent-form");
    const query = doc.getElementById("agent-query");
    const status = doc.getElementById("agent-status");
    const examples = doc.getElementById("example-list");
    const result = doc.getElementById("agent-result-content");
    const resultTitle = doc.getElementById("agent-result-title");
    const mode = doc.getElementById("agent-result-mode");
    if (!form || !query || !status || !examples || !result || !resultTitle || !mode) return;

    for (const example of EXAMPLES) {
      const item = doc.createElement("li");
      const button = makeElement(doc, "button", "", example);
      button.type = "button";
      button.addEventListener("click", () => {
        query.value = example;
        query.focus();
      });
      item.append(button);
      examples.append(item);
    }

    const submit = createSubmissionHandler({
      fetchImpl,
      onLoading: (loading, submittedQuery) => {
        if (!loading) return;
        result.setAttribute("aria-busy", "true");
        status.hidden = true;
        status.replaceChildren();
        resultTitle.textContent = "正在分析需求";
        mode.textContent = "请求处理中";
        mode.className = "agent-mode";
        result.replaceChildren(makeElement(doc, "p", "agent-loading-state", `正在筛选并比较可购方案：${submittedQuery}`));
      },
      onSuccess: (payload) => {
        result.setAttribute("aria-busy", "false");
        status.hidden = true;
        status.replaceChildren();
        renderResponse(doc, { result, resultTitle, mode }, payload);
      },
      onError: (error) => {
        result.setAttribute("aria-busy", "false");
        showStatus(doc, status, error.message, "error");
        renderError(doc, result, resultTitle, mode, error);
      },
    });

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const submittedQuery = query.value.trim();
      if (!submittedQuery) {
        showStatus(doc, status, "请先描述想找的耳机。", "error");
        query.focus();
        return;
      }
      void submit(submittedQuery);
    });
  }

  const api = { createSubmissionHandler, normalizeApiError, formatMoney, activeWeights };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.EarHubAgent = api;

  if (typeof document !== "undefined") {
    const initialize = () => initAgentPage(document, window.fetch.bind(window));
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize);
    else initialize();
  }
})();
