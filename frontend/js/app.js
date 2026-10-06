/* =============================================================================
 * EarHub 页面逻辑（S1.3：数据来自同源 /api/*）
 * -----------------------------------------------------------------------------
 * 商品列表、详情与多商家报价均从后端接口读取：
 *   - products.js -> GET /api/products                 （api.v0.1 §3）
 *   - loadProduct(id)      -> GET /api/products/{id}    （§4）
 *                          + GET /api/products/{id}/offers（§5）
 *
 * 前端只负责呈现：过滤、排序、到手价与有效售后全部由后端确定性逻辑给出。
 * 本文件不做业务判定，也不做"隐藏的推荐计算"——"到手价最低"仅是
 * 后端排序结果的事实性标注。
 *
 * 安全（FIX-S1.3-AUDIT P1）：来自 URL 与 API/数据库的任何文本、属性值
 * 进入 HTML 模板前必须经 esc() 转义；搜索结果计数与筛选标签用 DOM API
 * （textContent/createElement）构建，不走 innerHTML。新增渲染时遵守同一规则。
 *
 * 详情页排版为类 Amazon PDP 三栏结构（图库 | 标题/价格/卖点 | 购买框），
 *
 * 演示数据集为本地虚拟数据（meta.simulated=true），页面保留"模拟数据"标记。
 * ========================================================================== */

(function () {
  "use strict";

  /* ------------------------------- 展示标签 ------------------------------- */

  const SUB_CATEGORY_LABELS = {
    over_ear_headphones: "头戴式耳机",
    on_ear_headphones: "贴耳式耳机",
    true_wireless_earbuds: "真无线耳塞",
    wireless_earbuds: "无线耳塞",
    gaming_headsets: "游戏耳机",
    wired_headphones: "有线耳机",
  };

  const FORM_FACTOR_LABELS = {
    over_ear: "头戴式",
    on_ear: "贴耳式",
    in_ear: "入耳式",
    open_ear: "开放式",
  };

  const WEIGHT_BASIS_LABELS = {
    single_earbud: "单只",
    pair: "一对",
    headset: "整副",
    with_case: "含充电盒",
  };

  const MERCHANT_TYPE_LABELS = {
    authorized: "授权经销",
    independent: "独立商家",
    marketplace: "集市商家",
  };

  const TRANSPORT_LABELS = {
    wired_3_5mm: "3.5mm 有线",
    usb_audio: "USB 音频",
    bluetooth: "蓝牙",
    wireless_dongle: "无线发射器",
    optical_transmitter: "光纤发射",
  };

  const REASON_LABELS = {
    out_of_stock: "缺货",
    stock_unknown: "库存未知",
    price_unknown: "价格未知",
    expired: "报价已过期",
    region_mismatch: "不配送当前地区",
    mode_unavailable: "无可用连接方案",
    plan_incomplete: "必要配件信息不完整",
    accessory_out_of_stock: "必要配件缺货",
  };

  const ART_BY_SUB = {
    over_ear_headphones: "🎧",
    on_ear_headphones: "🎧",
    wired_headphones: "🎧",
    gaming_headsets: "🎮",
    true_wireless_earbuds: "🎼",
    wireless_earbuds: "🎼",
  };

  const TINTS = ["#e8eef8", "#eef4ec", "#f6eee8", "#efeaf6", "#e9f2f2", "#f6f0e8"];

  /* ------------------------------- 安全转义 ------------------------------- */

  /**
   * HTML 转义：所有 URL/API 来源的文本与属性值进入模板前必经此函数。
   * 覆盖 & < > " '，文本节点与双引号属性上下文均安全。
   */
  function esc(value) {
    if (value === null || value === undefined) return "";
    return String(value).replace(/[&<>"']/g, (ch) => {
      switch (ch) {
        case "&": return "&amp;";
        case "<": return "&lt;";
        case ">": return "&gt;";
        case '"': return "&quot;";
        default: return "&#39;";
      }
    });
  }

  /* ------------------------------- 格式化 ------------------------------- */

  function fmtYuan(cents) {
    if (cents === null || cents === undefined) return "—";
    return (cents / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function priceHTML(cents) {
    return `<span class="price-now"><span class="cur">¥</span>${fmtYuan(cents)}</span>`;
  }

  /** 亚马逊式大价格：¥ 上标 + 整数大字 + 小数角标（仅数值，无外部文本）。 */
  function bigPriceHTML(cents, cls) {
    if (cents === null || cents === undefined) return `<span class="${esc(cls)}">暂无报价</span>`;
    const [int, dec] = fmtYuan(cents).split(".");
    return `<span class="${esc(cls)}"><span class="pdp-cur">¥</span>${int}<span class="pdp-dec">.${dec}</span></span>`;
  }

  function fmtDate(iso) {
    return iso ? String(iso).slice(5).replace("-", "/") : "—";
  }

  function fmtDateFull(iso) {
    return iso ? String(iso).slice(0, 10) : "—";
  }

  function starsHTML(rating) {
    if (rating === null || rating === undefined) return `<span class="stars" aria-hidden="true">—</span>`;
    const full = Math.floor(rating);
    const half = rating - full >= 0.5;
    let out = "";
    for (let i = 0; i < 5; i += 1) {
      if (i < full) out += "★";
      else if (i === full && half) out += "★";
      else out += `<span class="off">★</span>`;
    }
    return `<span class="stars" aria-hidden="true">${out}</span>`;
  }

  function boolLabel(value) {
    if (value === true) return "支持";
    if (value === false) return "不支持";
    return "未知";
  }

  /* --------------------------- 商品视图模型（呈现） --------------------------- */

  function imageOf(summary) {
    // image_path 为空时用本地演示占位图（p001–p015）；P013–P015 为生成的 PNG，
    // 其余为 JPG。仍加载失败则露出 emoji 回退层。
    const id = (summary.product_id || "").toLowerCase();
    const ext = ["p013", "p014", "p015"].includes(id) ? "png" : "jpg";
    return summary.image_path || `/static/img/${id}.${ext}`;
  }

  function artOf(summary) {
    return ART_BY_SUB[summary.sub_category] || "🎧";
  }

  function tintOf(summary) {
    let hash = 0;
    for (const ch of String(summary.product_id || "")) hash = (hash * 31 + ch.charCodeAt(0)) % TINTS.length;
    return TINTS[hash];
  }

  function mediaHTML(view, variant) {
    const fallback = `<span class="media-fallback" aria-hidden="true">${artSafe(view.art)}</span>`;
    if (!view.image) return fallback;
    const cls = variant === "thumb" ? "media media-thumb" : "media";
    // 回退层常驻在图片下层：图片没加载出来（或加载失败被移除）时自然露出来。
    return `${fallback}<img class="${cls}" src="${esc(view.image)}"
              alt="${esc(view.alt)}"
              loading="lazy" decoding="async"
              onerror="this.remove()">`;
  }

  function artSafe(art) {
    // emoji 来自内部映射；仍统一转义，防止未来替换数据源时引入文本。
    return esc(art);
  }

  function featureList(summary) {
    const out = [];
    if (summary.wireless === true) out.push("无线连接");
    else if (summary.wireless === false) out.push("有线连接");
    if (summary.active_noise_cancelling === true) out.push("主动降噪");
    const bat = (summary.battery_specs || []).find((b) => b.hours != null && b.basis === "single_charge")
      || (summary.battery_specs || []).find((b) => b.hours != null);
    if (bat) {
      const basis = bat.basis === "with_case" ? "含盒" : "单次";
      const anc = bat.anc_state === "on" ? "·ANC开" : bat.anc_state === "off" ? "·ANC关" : "";
      out.push(`续航 ${bat.hours} 小时（${basis}${anc}）`);
    }
    const wt = (summary.weight_specs || []).find((w) => w.grams != null);
    if (wt) out.push(`${wt.grams} 克（${WEIGHT_BASIS_LABELS[wt.basis] || wt.basis || "口径未知"}）`);
    return out.slice(0, 3);
  }

  function shipText(summary) {
    if (summary.availability === "out_of_stock") return `<span class="ship-plain">暂时缺货</span>`;
    const offer = summary.display_offer;
    if (!offer) {
      return `<span class="ship-plain">${summary.availability === "unknown" ? "库存未知" : "暂无有货报价"}</span>`;
    }
    const w = offer.delivery_window;
    if (!w.observed_at) {
      return `<span class="ship-plain">配送观测日期未知，时效不可确认</span>`;
    }
    const kind = w.commitment_type === "guaranteed"
      ? "历史承诺窗口"
      : w.commitment_type === "estimated" ? "历史预计窗口" : "历史配送窗口";
    const span = w.earliest && w.latest
      ? `${esc(fmtDate(w.earliest))}–${esc(fmtDate(w.latest))}`
      : "时间未知";
    return `<span class="ship-plain">${kind}：${span}</span>
      <span class="ship-plain">观测于 ${esc(fmtDateFull(w.observed_at))}；不代表今天下单时效</span>`;
  }

  /* --------------------------- 数据加载（后端接口） --------------------------- */

  async function fetchJSON(url) {
    const resp = await fetch(url, { headers: { Accept: "application/json" } });
    if (!resp.ok) {
      let message = `请求失败（HTTP ${resp.status}）`;
      try {
        const body = await resp.json();
        if (body && body.error && body.error.message) message = body.error.message;
      } catch (e) { /* 非 JSON 错误体保持默认文案 */ }
      const err = new Error(message);
      err.status = resp.status;
      throw err;
    }
    return resp.json();
  }

  async function loadProduct(id) {
    const encoded = encodeURIComponent(id);
    const params = new URLSearchParams(window.location.search);
    const offerParams = new URLSearchParams({ destination_region: "CN-SH" });
    if (params.get("mode_id")) offerParams.set("mode_id", params.get("mode_id"));
    const [detail, offers] = await Promise.all([
      fetchJSON(`/api/products/${encoded}`),
      fetchJSON(`/api/products/${encoded}/offers?${offerParams.toString()}`),
    ]);
    return { detail, offers };
  }

  /* ------------------------------- 组件渲染 ------------------------------- */

  function cardHTML(summary) {
    const view = {
      art: artOf(summary),
      image: imageOf(summary),
      alt: `${summary.brand} ${summary.product_name}`,
    };
    const offer = summary.display_offer;
    const soldOut = summary.availability === "out_of_stock";
    const priceBlock = offer
      ? `<div class="card-price">${priceHTML(offer.landed_price_cents)}</div>`
      : `<div class="card-price"><span class="ship-plain">${soldOut ? "暂时缺货" : "价格未知"}</span></div>`;

    return `
      <li class="card">
        <div class="card-media" style="background:${tintOf(summary)}">
          ${mediaHTML(view, "card")}
          <button class="card-fav" type="button" aria-label="收藏（演示按钮）">♡</button>
        </div>
        <div class="card-body">
          <span class="card-brand">${esc(summary.brand)}</span>
          <h3 class="card-title"><a href="/product.html?id=${encodeURIComponent(summary.product_id)}">${esc(summary.product_name)}</a></h3>
          <div class="rating">
            ${starsHTML(summary.rating)}
            <span>${summary.rating !== null ? summary.rating.toFixed(1) : "—"}</span>
            <a class="rating-count" href="/product.html?id=${encodeURIComponent(summary.product_id)}">${
              summary.review_count !== null ? `${summary.review_count.toLocaleString("zh-CN")} 条评价` : "暂无评价"
            }</a>
          </div>
          ${priceBlock}
          <ul class="card-features">
            ${featureList(summary).map((f) => `<li>${esc(f)}</li>`).join("")}
          </ul>
          <div class="card-ship">${shipText(summary)}</div>
          <div class="card-actions">
            <a class="btn btn-primary btn-block btn-sm" href="/product.html?id=${encodeURIComponent(summary.product_id)}">
              ${offer ? "查看报价" : "查看详情"}
            </a>
            <span class="offer-note">${
              offer
                ? `报价 ${esc(offer.offer_id)} · ${esc(offer.merchant_name)}（${esc(offer.merchant_id)}）· 模式 ${esc(offer.mode_id)}`
                : "暂无有货报价"
            }</span>
          </div>
        </div>
      </li>`;
  }

  function pagerHTML(res) {
    const pages = Math.ceil(res.total / res.page_size);
    if (res.total === 0) return "";
    const mk = (page, label, cls) => {
      const sp = new URLSearchParams(window.location.search);
      sp.set("page", String(page));
      return `<a class="${esc(cls || "")}" href="?${esc(sp.toString())}">${esc(label)}</a>`;
    };
    if (res.page > pages) {
      return `<nav class="pager pager-out-of-range" aria-label="分页">
        <span>请求的第 ${res.page} 页超出范围，共 ${pages} 页</span>
        ${mk(pages, `返回第 ${pages} 页`, "pager-return")}
      </nav>`;
    }
    let out = "";
    out += res.page > 1 ? mk(res.page - 1, "上一页") : `<span class="disabled">上一页</span>`;
    for (let i = 1; i <= pages; i += 1) {
      out += i === res.page ? `<span class="current">${i}</span>` : mk(i, String(i));
    }
    out += res.page < pages ? mk(res.page + 1, "下一页") : `<span class="disabled">下一页</span>`;
    return `<nav class="pager" aria-label="分页">${out}</nav>`;
  }

  function filtersHTML(params) {
    const priceRanges = [
      { label: "500 元以内", min: null, max: 50000 },
      { label: "500 – 1000 元", min: 50000, max: 100000 },
      { label: "1000 – 2000 元", min: 100000, max: 200000 },
      { label: "2000 元以上", min: 200000, max: null },
    ];
    const link = (over) => {
      const sp = new URLSearchParams(window.location.search);
      Object.keys(over).forEach((k) => {
        if (over[k] === null) sp.delete(k);
        else sp.set(k, String(over[k]));
      });
      sp.delete("page");
      const qs = sp.toString();
      return qs ? `?${qs}` : "?";
    };

    return `
      <aside class="filters" aria-label="筛选">
        <h2>筛选</h2>

        <div class="filter-group">
          <h3>品类</h3>
          <ul class="filter-list">
            ${Object.keys(SUB_CATEGORY_LABELS).map((key) => {
              const on = params.sub_category === key;
              return `<li><a href="${link({ sub_category: on ? null : key })}"${on ? ' style="font-weight:700;color:var(--brand)"' : ""}>
                ${on ? "✓ " : ""}${esc(SUB_CATEGORY_LABELS[key])}</a></li>`;
            }).join("")}
          </ul>
        </div>

        <div class="filter-group">
          <h3>品牌</h3>
          <form class="brand-filter" id="brand-filter-form" action="/" method="get">
            <label class="sr-only" for="brand-filter-input">输入完整品牌名</label>
            <div class="brand-filter-row">
              <input id="brand-filter-input" name="brand" type="search" maxlength="80"
                value="${esc(params.brand)}" placeholder="输入品牌名" autocomplete="off">
              <button class="filter-apply" type="submit">应用</button>
            </div>
          </form>
        </div>

        <div class="filter-group">
          <h3>价格（到手价）</h3>
          <ul class="filter-list">
            ${priceRanges.map((r) => {
              const on = params.min_price_cents === r.min && params.max_price_cents === r.max;
              return `<li><a href="${link({ min_price_cents: r.min, max_price_cents: r.max })}"${on ? ' style="font-weight:700;color:var(--brand)"' : ""}>
                ${on ? "✓ " : ""}${r.label}</a></li>`;
            }).join("")}
          </ul>
        </div>

        <div class="filter-group">
          <h3>顾客评分</h3>
          <ul class="filter-list">
            ${[4.5, 4.0, 3.0].map((r) => {
              const on = params.min_rating === r;
              return `<li><a href="${link({ min_rating: on ? null : r })}"${on ? ' style="font-weight:700;color:var(--brand)"' : ""}>
                ${on ? "✓ " : ""}${r.toFixed(1)} 分及以上</a></li>`;
            }).join("")}
          </ul>
        </div>
      </aside>`;
  }

  /* --------------------------- 顶部搜索框品类选择 --------------------------- */

  function initSearchScope() {
    const scope = document.querySelector(".search-scope");
    const form = document.querySelector("form.search");
    if (!scope || !form) return;

    const current = new URLSearchParams(window.location.search).get("sub_category") || "";
    if (current) scope.value = current;

    scope.addEventListener("change", () => {
      // 切换品类时保留已有搜索词，其余筛选条件按新品类重新开始。
      form.submit();
    });
  }

  /* ------------------------------- 首页 ------------------------------- */

  function setRequestStatus(message, { kind = "loading", retry = null } = {}) {
    const status = document.getElementById("request-status");
    if (!status) return;

    status.replaceChildren();
    status.hidden = !message;
    status.dataset.kind = kind;
    status.setAttribute("role", kind === "error" ? "alert" : "status");
    status.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
    if (!message) return;

    const copy = document.createElement("span");
    copy.textContent = message;
    status.appendChild(copy);
    if (retry) {
      const button = document.createElement("button");
      button.className = "btn btn-outline btn-sm";
      button.type = "button";
      button.textContent = "重试";
      button.addEventListener("click", retry);
      status.appendChild(button);
    }
  }

  async function initHome() {
    const params = window.EarHubProducts.readParams(window.location.search);

    // 搜索框回填（input.value 由浏览器按纯文本处理，不解析 HTML）
    const input = document.querySelector(".search input[type='search']");
    if (input && params.q) input.value = params.q;

    const grid = document.getElementById("product-grid");
    const count = document.getElementById("results-count");
    const results = document.getElementById("results-region");
    const pager = document.getElementById("pager-mount");
    const filters = document.getElementById("filters-mount");
    const chips = document.getElementById("active-filters");

    const sort = document.getElementById("sort-select");
    if (sort) {
      sort.value = params.sort;
      sort.addEventListener("change", () => {
        const sp = new URLSearchParams(window.location.search);
        sp.set("sort", sort.value);
        sp.delete("page");
        window.location.search = sp.toString();
      });
    }

    if (filters) {
      filters.addEventListener("submit", (event) => {
        if (event.target.id !== "brand-filter-form") return;
        event.preventDefault();
        const brand = document.getElementById("brand-filter-input").value.trim();
        const sp = new URLSearchParams(window.location.search);
        if (brand) sp.set("brand", brand);
        else sp.delete("brand");
        sp.delete("page");
        window.location.search = sp.toString();
      });
    }

    const load = async () => {
      if (results) results.setAttribute("aria-busy", "true");
      if (grid) grid.replaceChildren();
      if (pager) pager.replaceChildren();
      setRequestStatus("正在加载商品…");

      try {
        const res = await window.EarHubProducts.loadProducts(params, fetchJSON);
        if (!res || !Array.isArray(res.items)) throw new Error("商品数据格式不正确");
        if (results) results.setAttribute("aria-busy", "false");
        setRequestStatus("");

        // 结果计数使用 DOM API，搜索词始终作为纯文本处理。
        if (count) {
          count.replaceChildren();
          if (params.q) {
            count.append("“");
            const qStrong = document.createElement("b");
            qStrong.textContent = params.q;
            count.append(qStrong, "” 的搜索结果：");
          } else {
            count.append("耳机商品：");
          }
          const totalStrong = document.createElement("b");
          totalStrong.textContent = String(res.total);
          count.append(totalStrong, params.q ? " 个商品" : " 个");
        }

        if (grid) {
          if (res.items.length) {
            grid.innerHTML = res.items.map(cardHTML).join("");
          } else {
            const empty = document.createElement("li");
            empty.className = "panel empty-state";
            if (res.total > 0 && res.page > Math.ceil(res.total / res.page_size)) {
              empty.textContent = `请求的第 ${res.page} 页没有商品；当前筛选共 ${res.total} 件商品。`;
            } else {
              empty.textContent = "没有符合条件的商品。可以放宽价格或减少筛选条件后重试。";
            }
            grid.appendChild(empty);
          }
        }
        if (filters) filters.innerHTML = filtersHTML(params);
        if (pager) pager.innerHTML = pagerHTML(res);

        // 筛选标签以 DOM API 构建，不将 URL 文本解析为 HTML。
        if (chips) {
          const items = [];
          if (params.q) items.push(["q", `关键词：${params.q}`]);
          if (params.brand) items.push(["brand", `品牌：${params.brand}`]);
          if (params.sub_category) items.push(["sub_category", `品类：${SUB_CATEGORY_LABELS[params.sub_category] || params.sub_category}`]);
          if (params.min_price_cents !== null) items.push(["min_price_cents", `价格下限：¥${fmtYuan(params.min_price_cents)}`]);
          if (params.max_price_cents !== null) items.push(["max_price_cents", `价格上限：¥${fmtYuan(params.max_price_cents)}`]);
          if (params.min_rating !== null) items.push(["min_rating", `评分：${params.min_rating} 分以上`]);

          chips.replaceChildren();
          items.forEach(([key, label]) => {
            const chip = document.createElement("span");
            chip.className = "chip";
            chip.textContent = label;
            const btn = document.createElement("button");
            btn.type = "button";
            btn.dataset.remove = key;
            btn.title = "移除该筛选";
            btn.setAttribute("aria-label", `移除${label}`);
            btn.textContent = "×";
            btn.addEventListener("click", () => {
              const sp = new URLSearchParams(window.location.search);
              sp.delete(key);
              sp.delete("page");
              window.location.search = sp.toString();
            });
            chip.appendChild(btn);
            chips.appendChild(chip);
          });
          if (items.length) {
            const clear = document.createElement("a");
            clear.className = "chip-clear";
            clear.href = "./";
            clear.textContent = "清除全部";
            chips.appendChild(clear);
          }
        }
      } catch (err) {
        if (results) results.setAttribute("aria-busy", "false");
        if (count) count.textContent = "商品加载失败";
        setRequestStatus(`商品数据加载失败：${err.message}。请检查本地服务或数据库后重试。`, {
          kind: "error",
          retry: () => { void load(); },
        });
      }
    };

    await load();
  }

  /* --------------------------- 详情页（类 Amazon PDP） --------------------------- */

  function batterySpecText(product, spec) {
    const duration = spec.hours == null ? "续航时长未知" : `${spec.hours} 小时`;
    const basis = spec.basis === "with_case" ? "含盒累计" : "单次充电";
    const anc = spec.anc_state === "on" ? "·ANC 开"
      : spec.anc_state === "off" ? "·ANC 关"
        : spec.anc_state === "not_applicable" ? "·ANC 不适用"
          : spec.anc_state === "unknown" ? "·ANC 状态未知" : "";
    const mode = (product.connection_modes || []).find((item) => item.mode_id === spec.mode_id);
    const modeName = mode ? TRANSPORT_LABELS[mode.transport] || mode.transport : "模式未知";
    const modeText = spec.mode_id == null
      ? "适用模式未知"
      : `适用模式：${modeName}（${spec.mode_id}）`;
    const conditions = spec.test_conditions == null || !String(spec.test_conditions).trim()
      ? "测试条件未知"
      : `测试条件：${spec.test_conditions}`;
    return `${duration}（${basis}${anc}）；${modeText}；${conditions}`;
  }

  function batteryText(product) {
    const specs = product.battery_specs;
    if (specs === null) return "未知";
    if (!specs.length) return "无记录";
    return specs.map((spec) => batterySpecText(product, spec)).join(" / ");
  }

  function metricsText(metrics) {
    const rating = metrics.rating == null ? "评分未知" : `${metrics.rating.toFixed(1)} 分`;
    const count = metrics.review_count == null
      ? "评价数量未知"
      : `${metrics.review_count.toLocaleString("zh-CN")} 条评价`;
    return `${rating}（${count}）`;
  }

  function weightText(product) {
    const specs = product.weight_specs;
    if (specs === null) return "未知";
    if (!specs.length) return "无记录";
    return specs.map((w) =>
      w.grams != null ? `${w.grams} 克（${WEIGHT_BASIS_LABELS[w.basis] || w.basis || "口径未知"}）` : "未知"
    ).join(" / ");
  }

  /** "关于此商品"要点：全部来自商品事实，缺数据的条目直接不出现。 */
  function bulletList(product) {
    const out = [];
    if (product.bluetooth_version) out.push(`蓝牙 ${product.bluetooth_version}`);
    const transports = (product.connection_modes || [])
      .map((m) => TRANSPORT_LABELS[m.transport] || m.transport);
    if (transports.length) out.push(`连接方式：${transports.join(" / ")}`);
    if (product.active_noise_cancelling === true) out.push("支持主动降噪（ANC）");
    if (product.microphone === true) out.push("内置麦克风");
    if (product.multipoint === true) out.push("支持多点连接");
    const bat = product.battery_specs;
    if (bat) for (const b of bat) out.push(`续航：${batterySpecText(product, b)}`);
    const wt = product.weight_specs;
    if (wt) for (const w of wt) {
      if (w.grams == null) continue;
      out.push(`重量 ${w.grams} 克（${WEIGHT_BASIS_LABELS[w.basis] || w.basis || "口径未知"}）`);
    }
    if (product.foldable === true) out.push("可折叠收纳");
    if ((product.charging_methods || []).length) out.push(`充电方式：${product.charging_methods.join(" / ")}`);
    if ((product.codec_support || []).length) out.push(`支持编码：${product.codec_support.join(" / ")}`);
    return out.slice(0, 9);
  }

  /** 到手价拆解行：商品价 ＋ 运费 ＋ 必要配件 － 即时优惠（仅数值）。 */
  function priceBreakdown(view) {
    const o = view.offer;
    const parts = [o.price_cents == null ? "商品价未知" : `商品价 ¥${fmtYuan(o.price_cents)}`];
    parts.push(shippingLabel(o.shipping_fee_cents));
    parts.push(view.accessory_total_cents == null ? "必要配件价格未知" : `必要配件 ¥${fmtYuan(view.accessory_total_cents)}`);
    parts.push(o.instant_discount_cents == null ? "即时优惠未知" : `优惠 −¥${fmtYuan(o.instant_discount_cents)}`);
    return parts.join("　·　");
  }

  function shippingLabel(cents) {
    return cents == null ? "运费未知" : cents === 0 ? "免运费" : `运费 ¥${fmtYuan(cents)}`;
  }

  function deliveryLabel(window) {
    if (!window) return "配送窗口未知";
    if (!window.observed_at) return "配送观测日期未知，时效不可确认";
    const span = window.earliest && window.latest
      ? `${esc(fmtDate(window.earliest))}–${esc(fmtDate(window.latest))}`
      : "时间未知";
    const kind = window.commitment_type === "guaranteed"
      ? "历史承诺窗口"
      : window.commitment_type === "estimated" ? "历史预计窗口" : "历史配送窗口";
    return `${kind}：${span}（观测于 ${esc(fmtDateFull(window.observed_at))}，不代表今天下单时效）`;
  }

  function sourcesHTML(title, sourceMap, derivations) {
    const entries = Object.entries(sourceMap || {});
    return `<details class="pdp-sources"><summary>${esc(title)}</summary>
      ${entries.length ? `<dl class="pdp-source-list">${entries.map(([field, source]) => {
        const s = source || {};
        return `<div><dt>${esc(field)}</dt><dd>类型：${esc(s.source_type || "未知")} · 来源：${esc(s.source_ref || "未知")}<br>
          观测：${esc(s.observed_at || "未知")} · 方法：${esc(s.method || "未知")}
          ${s.missing_reason ? `<br>缺失原因：${esc(s.missing_reason)}` : ""}</dd></div>`;
      }).join("")}</dl>` : "<p>来源信息未知</p>"}
      ${Object.entries(derivations || {}).map(([field, d]) => `<p>${esc(field)}：${esc(d.rule)}；输入 ${esc((d.input_refs || []).join("、"))}</p>`).join("")}
    </details>`;
  }

  function policyHTML(policy, warranty, policyOrigin, warrantyOrigin) {
    const p = policy || {}, w = warranty || {};
    const origin = value => value === "offer_override" ? "报价覆盖" : value === "merchant_default" ? "商家默认" : "来源未知";
    const returnTerm = p.days == null ? "退货期限未知" : p.days === 0 ? "不接受退货" : `${p.days} 天退货`;
    const warrantyTerm = w.months == null ? "保修期限未知" : w.months === 0 ? "不提供保修" : `${w.months} 个月`;
    return `<div>${returnTerm} · ${
      p.opened_return_allowed === true ? "可开封试戴退货" : p.opened_return_allowed === false ? "不接受开封退货" : "开封退货条件未知"
    }</div><div>退货运费：${p.return_shipping_fee_cents == null ? "未知" : `¥${fmtYuan(p.return_shipping_fee_cents)}`}</div>
      <div>退货条件：${esc(p.conditions == null ? "未知" : p.conditions)}</div>
      <div>例外：${p.exceptions == null ? "未知" : p.exceptions.length ? p.exceptions.map(esc).join("；") : "无额外例外"}（${origin(policyOrigin)}）</div>
      <div>保修：${warrantyTerm} · ${esc(w.provider || "提供方未知")}</div>
      <div>保修条件：${esc(w.conditions == null ? "未知" : w.conditions)}（${origin(warrantyOrigin)}）</div>`;
  }

  function pdpOfferRowHTML(view, isBest) {
    const o = view.offer;
    const m = view.merchant;
    const w = view.delivery_window;
    const policy = view.effective_return_policy;
    const warranty = view.effective_warranty;
    const reasons = (view.eligibility_reasons || [])
      .map((r) => REASON_LABELS[r] || esc(r))
      .join("、");

    return `
      <li class="pdp-offer-row${isBest ? " best" : ""}${view.eligible ? "" : " blocked"}">
        <div class="pdp-offer-seller">
          <div class="pdp-offer-merchant">${esc(m.merchant_name)}${isBest ? '<span class="tag">到手价最低</span>' : ""}</div>
          <div class="pdp-offer-sub">报价 ${esc(o.offer_id)} · 商家 ${esc(m.merchant_id)} · 模式 ${esc(view.mode_id || "未知")} · ${esc(MERCHANT_TYPE_LABELS[m.merchant_type] || m.merchant_type || "类型未知")} · 商家评分 ${
            m.merchant_rating !== null ? m.merchant_rating.toFixed(1) : "—"
          } · 库存 ${o.stock !== null ? `${o.stock} 件` : "未知"}</div>
          ${view.eligible ? "" : `<div class="pdp-offer-blocked">暂不可购：${reasons || "条件未知"}</div>`}
        </div>
        <div class="pdp-offer-cell">
          <span class="k">到手价</span>
          <span class="pdp-offer-price">${view.landed_price_cents !== null ? `¥${fmtYuan(view.landed_price_cents)}` : "无法定价"}</span>
          <span class="pdp-offer-sub">${priceBreakdown(view)}</span>
          ${o.original_price_cents ? `<span class="pdp-offer-was">参考价 ¥${fmtYuan(o.original_price_cents)}</span>` : ""}
        </div>
        <div class="pdp-offer-cell">
          <span class="k">配送 / 运费</span>
          ${deliveryLabel(w)}
          · ${shippingLabel(o.shipping_fee_cents)}
          <span class="pdp-offer-sub">配送至 ${esc(w ? w.destination_region || "未知" : "未知")}；条件：${esc(o.delivery ? o.delivery.conditions || "未知" : "未知")}</span>
        </div>
        <div class="pdp-offer-cell">
          <span class="k">售后</span>
          ${policyHTML(policy, warranty, view.policy_origin, view.warranty_origin)}
        </div>
        <div class="pdp-offer-evidence">
          ${sourcesHTML("报价原始字段来源", o.field_sources)}
          ${sourcesHTML("商家字段来源", m.field_sources)}
          ${sourcesHTML("方案与有效售后来源 / 计算依据", view.field_sources, view.derivation)}
        </div>
      </li>`;
  }

  async function initProduct() {
    const id = new URLSearchParams(window.location.search).get("id");
    const mount = document.getElementById("detail-mount");
    if (!mount) return;
    if (!id) {
      mount.innerHTML = `<div class="panel" role="alert"><h2>缺少商品编号</h2><p>请从商品列表选择商品。</p><a class="btn btn-primary" href="/">返回商品列表</a></div>`;
      return;
    }

    let loaded;
    try {
      loaded = await loadProduct(id);
    } catch (err) {
      mount.innerHTML = `<div class="panel" style="text-align:center">
        <h2 style="margin:0 0 8px">${err.status === 404 ? "未找到该商品" : "商品数据加载失败"}</h2>
        <p style="color:var(--muted);margin:0 0 16px">${err.status === 404 ? `商品编号 ${esc(id)} 不存在。` : esc(err.message)}</p>
        <a class="btn btn-primary" href="/">返回商品列表</a>
      </div>`;
      return;
    }

    const product = loaded.detail.product;
    const metrics = loaded.detail.metrics;
    const views = loaded.offers.items;
    const best = views.find((v) => v.eligible) || null;
    const eligibleCount = views.filter((v) => v.eligible).length;

    document.title = `${product.product_name}｜EarHub`;

    const imgView = {
      art: ART_BY_SUB[product.sub_category] || "🎧",
      image: imageOf(product),
      alt: product.product_name,
    };
    const w = best ? best.delivery_window : null;
    const seller = best ? best.merchant : null;
    const policy = best ? best.effective_return_policy : null;
    const warranty = best ? best.effective_warranty : null;

    const specRows = [
      ["品牌", product.brand],
      ["型号", product.model],
      ["品类", SUB_CATEGORY_LABELS[product.sub_category] || product.sub_category],
      ["形态", FORM_FACTOR_LABELS[product.form_factor] || product.form_factor || "未知"],
      ["连接方式", (product.connection_modes || []).map((m) => TRANSPORT_LABELS[m.transport] || m.transport).join(" / ") || "未知"],
      ["蓝牙版本", product.bluetooth_version || "未知"],
      ["主动降噪", boolLabel(product.active_noise_cancelling)],
      ["麦克风", boolLabel(product.microphone)],
      ["多点连接", boolLabel(product.multipoint)],
      ["续航", batteryText(product)],
      ["重量", weightText(product)],
      ["充电方式", (product.charging_methods || []).join(" / ") || "未知"],
      ["支持编码", (product.codec_support || []).join(" / ") || "未知"],
      ["评分", metricsText(metrics)],
    ];

    mount.innerHTML = `
      <div class="pdp">

        <!-- ======== 顶部：图库 | 标题/价格/卖点 | 购买框 ======== -->
        <div class="pdp-top">
          <div class="pdp-gallery">
            <div class="pdp-thumbs">
              <div class="pdp-thumb active">${mediaHTML(imgView, "thumb")}</div>
            </div>
            <div class="pdp-hero" style="background:${tintOf(product)}">
              ${mediaHTML(imgView, "main")}
            </div>
          </div>

          <div class="pdp-info">
            <a class="pdp-brand" href="/?brand=${encodeURIComponent(product.brand)}">${esc(product.brand)}</a>
            <h1 class="pdp-title">${esc(product.product_name)}</h1>
            <div class="pdp-rating">
              <b>${metrics.rating !== null ? metrics.rating.toFixed(1) : "—"}</b>
              ${starsHTML(metrics.rating)}
              <a href="#pdp-reviews">${metrics.review_count !== null ? `${metrics.review_count.toLocaleString("zh-CN")} 条评价` : "评价数量未知"}</a>
              <span class="pdp-dot">|</span>
              <a href="#pdp-offers">${views.length} 个商家报价</a>
            </div>

            <div class="pdp-rule"></div>

            ${best ? `
            <div class="pdp-price-block">
              <div class="pdp-price-row">
                <span class="pdp-price-tag">到手价</span>
                ${bigPriceHTML(best.landed_price_cents, "pdp-price")}
                ${best.offer.original_price_cents ? `<span class="pdp-was">参考价 ¥${fmtYuan(best.offer.original_price_cents)}</span>` : ""}
              </div>
              <div class="pdp-price-detail">${priceBreakdown(best)}</div>
              <div class="pdp-price-detail">报价 ${esc(best.offer.offer_id)} · 商家 ${esc(best.merchant.merchant_id)} · 模式 ${esc(best.mode_id)}</div>
            </div>

            <div class="pdp-rule"></div>

            <div class="pdp-facts">
              <div class="pdp-fact">
                <span class="k">配送窗口</span>
                <b>${deliveryLabel(w)}</b>
              </div>
              <div class="pdp-fact">
                <span class="k">配送至</span>
                <span>${esc(w ? w.destination_region || "未知" : "未知")} · ${
                  best.offer.shipping_fee_cents === 0 ? "免运费" : `运费 ¥${fmtYuan(best.offer.shipping_fee_cents)}`
                }</span>
              </div>
              <div class="pdp-fact">
                <span class="k">库存状态</span>
                <span class="pdp-stock in">有货${best.offer.stock !== null ? `（${best.offer.stock} 件）` : ""}</span>
                <span class="pdp-fact-sub">另有 ${Math.max(0, eligibleCount - 1)} 条可购报价，共 ${views.length} 条</span>
              </div>
            </div>` : `
            <div class="pdp-price-block">
              <div class="pdp-price-row"><span class="pdp-price out">暂无可购报价</span></div>
              <div class="pdp-price-detail">全部 ${views.length} 条报价均不满足可购条件（缺货/过期/价格未知），可展开下方报价明细查看原因。</div>
            </div>`}

            <div class="pdp-rule"></div>

            <h2 class="pdp-h2">关于此商品</h2>
            <ul class="pdp-bullets">
              ${bulletList(product).map((b) => `<li>${esc(b)}</li>`).join("")}
            </ul>
          </div>

          <!-- ======== 购买框 ======== -->
          <aside class="pdp-buybox">
            ${best ? `
              <div class="pdp-buybox-price">${bigPriceHTML(best.landed_price_cents, "pdp-price")}</div>
              <div class="pdp-buybox-seller">报价 ${esc(best.offer.offer_id)} · 模式 ${esc(best.mode_id)} · 商家 ${esc(seller.merchant_id)}</div>
              <div class="pdp-buybox-ship">${
                best.offer.shipping_fee_cents === 0 ? "免运费" : `运费 ¥${fmtYuan(best.offer.shipping_fee_cents)}`
              }</div>
              <div class="pdp-buybox-delivery">
                ${deliveryLabel(w)}
              </div>
              <div class="pdp-buybox-stock">有货${best.offer.stock !== null ? `（${best.offer.stock} 件）` : ""}</div>
              <div class="pdp-buybox-seller">由 <b>${esc(seller.merchant_name)}</b> 销售${
                seller.merchant_rating !== null ? ` · 商家评分 ${seller.merchant_rating.toFixed(1)}` : ""
              }</div>
              <div class="pdp-qty">
                <label for="pdp-qty-select">数量：</label>
                <select id="pdp-qty-select">${[1, 2, 3, 4, 5].map((n) => `<option>${n}</option>`).join("")}</select>
              </div>
              <button class="btn btn-primary btn-block pdp-btn" type="button">加入购物车（演示）</button>
              <button class="btn btn-buy btn-block pdp-btn" type="button">立即购买（演示）</button>
              <ul class="pdp-buybox-meta">
                <li><span class="k">退货</span>${policy.days == null ? "期限未知" : policy.days === 0 ? "不接受退货" : `${policy.days} 天`}（${
                  policy.opened_return_allowed === false ? "不接受开封退货" :
                  policy.opened_return_allowed === true ? "可开封试戴" : "开封退货条件未知"
                }）</li>
                <li><span class="k">保修</span>${warranty.months == null ? "期限未知" : warranty.months === 0 ? "不提供保修" : `${warranty.months} 个月`}${
                  warranty.provider ? ` · ${esc(warranty.provider)}` : ""
                }</li>
                <li><span class="k">交易提示</span>演示站点，不产生真实订单与支付</li>
                <li><span class="k">配送口径</span>${deliveryLabel(w)}</li>
              </ul>` : `
              <div class="pdp-buybox-price"><span class="pdp-price out">暂无可购报价</span></div>
              <div class="pdp-buybox-stock out">当前没有满足可购条件的报价</div>
              <button class="btn btn-outline btn-block pdp-btn" type="button" disabled>暂无可购方案</button>
              <ul class="pdp-buybox-meta">
                <li><span class="k">交易提示</span>演示站点，不产生真实订单与支付</li>
              </ul>`}
          </aside>
        </div>

        <!-- ======== 商品描述 ======== -->
        <section class="pdp-section">
          <h2>商品描述</h2>
          <p class="pdp-desc">${esc(product.description) || "暂无描述。"}</p>
        </section>

        <!-- ======== 商品参数 ======== -->
        <section class="pdp-section">
          <h2>商品参数</h2>
          <table class="spec-table pdp-specs">
            ${specRows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("")}
          </table>
          <p class="pdp-note">
            续航与重量按上表标注的口径记录；不同口径的数值不能直接比较。来源与观测时点见报价明细。
          </p>
          ${sourcesHTML("商品字段来源与观测信息", product.field_sources)}
          ${sourcesHTML("评价指标来源与观测信息", metrics.field_sources)}
        </section>

        <section class="pdp-section" id="pdp-reviews">
          <h2>评价摘要</h2>
          <p>评分：${metrics.rating == null ? "未知" : `${metrics.rating.toFixed(1)} / 5`}；评价数量：${metrics.review_count == null ? "未知" : metrics.review_count}。</p>
          <p class="pdp-note">模拟评价汇总，当前数据不提供逐条评论正文。</p>
        </section>

        <!-- ======== 多商家报价 ======== -->
        <section class="pdp-section" id="pdp-offers">
          <form class="pdp-mode-form" action="/product.html" method="get">
            <input type="hidden" name="id" value="${esc(product.product_id)}">
            <label for="pdp-mode">连接方案：</label>
            <select id="pdp-mode" name="mode_id">
              <option value="">后端默认方案</option>
              ${(product.connection_modes || []).map(m => `<option value="${esc(m.mode_id)}"${new URLSearchParams(window.location.search).get("mode_id") === m.mode_id ? " selected" : ""}>${esc(m.mode_id)} · ${esc(TRANSPORT_LABELS[m.transport] || m.transport)}</option>`).join("")}
            </select>
            <button class="btn btn-outline btn-sm" type="submit">查看方案报价</button>
          </form>
          <p class="pdp-note">配送地区固定为 CN-SH；必要配件、到手价及可购资格由后端按当前连接方案返回。</p>
          <div class="pdp-section-head">
            <h2>商家报价（${views.length}）</h2>
            <span class="pdp-note">可购优先、按到手价升序</span>
          </div>
          ${views.length ? `
            <ul class="pdp-offers">
              <li class="pdp-offer-head" aria-hidden="true">
                <span>商家</span><span>到手价</span><span>配送 / 运费</span><span>售后</span>
              </li>
              ${views.map((v) => pdpOfferRowHTML(v, best && v.offer.offer_id === best.offer.offer_id)).join("")}
            </ul>` : `
            <div class="panel" style="color:var(--muted);font-size:13px">该商品当前没有报价。</div>`}
          <p class="pdp-note">
            排序与到手价由后端 api.v0.1 确定性逻辑给出（到手价＝商品价＋运费＋必要配件－即时优惠）；
            “到手价最低”是当前地区与连接方案下的后端排序结果标注。
          </p>
        </section>
      </div>`;
  }

  /* ------------------------------- 启动与导出 ------------------------------- */

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", () => {
      initSearchScope();

      const page = document.body.dataset.page;
      if (page === "home") initHome();
      else if (page === "product") initProduct();
    });
  }

  // 供无 DOM 回归测试读取转义函数（Node require 本文件；浏览器挂到 window 备查）
  if (typeof window !== "undefined") window.EarHubUI = { esc };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { esc, batteryText, batterySpecText, bulletList, metricsText };
  }
})();
