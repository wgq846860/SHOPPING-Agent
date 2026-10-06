/* Product-list query state shared by the traditional shopping page. */
(function (root, factory) {
  const products = factory();
  if (typeof module === "object" && module.exports) module.exports = products;
  if (root) root.EarHubProducts = products;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const PAGE_SIZE = 24;

  function readParams(search) {
    const params = new URLSearchParams(search === undefined ? window.location.search : search);
    const number = (key) => {
      const value = params.get(key);
      return value ? Number(value) : null;
    };
    const page = Number(params.get("page"));

    return {
      q: (params.get("q") || "").trim(),
      brand: (params.get("brand") || "").trim(),
      sub_category: params.get("sub_category") || "",
      min_price_cents: number("min_price_cents"),
      max_price_cents: number("max_price_cents"),
      min_rating: number("min_rating"),
      sort: params.get("sort") || "relevance",
      page: Number.isSafeInteger(page) && page > 0 ? page : 1,
    };
  }

  function buildProductsUrl(params) {
    const query = new URLSearchParams();
    if (params.q) query.set("q", params.q);
    if (params.brand) query.set("brand", params.brand);
    if (params.sub_category) query.set("sub_category", params.sub_category);
    if (params.min_price_cents !== null) query.set("min_price_cents", String(params.min_price_cents));
    if (params.max_price_cents !== null) query.set("max_price_cents", String(params.max_price_cents));
    if (params.min_rating !== null) query.set("min_rating", String(params.min_rating));
    query.set("sort", params.sort || "relevance");
    query.set("page", String(params.page));
    query.set("page_size", String(PAGE_SIZE));
    return `/api/products?${query.toString()}`;
  }

  function loadProducts(params, fetchJSON) {
    if (typeof fetchJSON !== "function") throw new TypeError("fetchJSON must be a function");
    return fetchJSON(buildProductsUrl(params));
  }

  return Object.freeze({ PAGE_SIZE, readParams, buildProductsUrl, loadProducts });
});
