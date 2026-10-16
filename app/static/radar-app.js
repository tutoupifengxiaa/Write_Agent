(function () {
  const ReactLib = window.React;
  const ReactDOMLib = window.ReactDOM;
  const { requestJson, Navigation, statusLabel } = window.WriteAgentUI;

  if (!ReactLib || !ReactDOMLib) {
    throw new Error("页面运行资源加载失败，请刷新重试");
  }

  const { useEffect, useState } = ReactLib;

  const PROVIDER_LABELS = {
    fanqie: "番茄小说",
    qimao: "七猫小说",
    qidian: "起点中文网",
  };

  const COLLECTION_LABELS = { real: "采集成功", degraded: "暂不可用", failed: "采集失败" };

  function captureTime(value) {
    if (!value) return "未记录采集时间";
    const date = new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : value + "Z");
    return Number.isNaN(date.getTime()) ? "未记录采集时间" : date.toLocaleString("zh-CN", { hour12: false });
  }

  function ResultBlock(props) {
    return ReactLib.createElement(
      "section",
      {
        className: props.wide ? "radar-result-block radar-result-block-wide" : "radar-result-block",
      },
      ReactLib.createElement("h4", null, props.title),
      props.children
    );
  }

  function validatedSourceTrace(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return null;
    }
    if (typeof value.capture_id !== "string" || !value.capture_id.trim()) {
      return null;
    }
    const arrayFields = ["snapshot_ids", "providers", "selected_item_ids"];
    const validArrays = arrayFields.every(function (field) {
      return Array.isArray(value[field]) && value[field].every(function (item) {
        return typeof item === "string";
      });
    });
    if (!validArrays || (value.captured_at !== null && value.captured_at !== undefined && typeof value.captured_at !== "string")) {
      return null;
    }
    return {
      capture_id: value.capture_id,
      snapshot_ids: value.snapshot_ids.slice(),
      providers: value.providers.slice(),
      selected_item_ids: value.selected_item_ids.slice(),
      captured_at: value.captured_at || null,
    };
  }

  function buildOutlinePrefillLink(result, form) {
    const params = new URLSearchParams();
    params.set("direction_name", result.direction_name || form.directionName || "");
    params.set("title", ((result.title_candidates || [])[0]) || "");
    params.set("platform", ((result.target_platforms || [])[0]) || "");
    params.set("genre", ((form.keywords || "").split(",")[0] || "").trim());
    params.set("core_hook", result.core_hook || "");
    params.set("audience", result.audience || "");
    params.set("notes", form.notes || "");
    const sourceTrace = validatedSourceTrace(result.source_trace);
    if (sourceTrace) {
      params.set("source_trace", JSON.stringify(sourceTrace));
    }
    return "/outline?" + params.toString();
  }

  function App() {
    const [hotlists, setHotlists] = useState([]);
    const [activeProvider, setActiveProvider] = useState("fanqie");
    const [insights, setInsights] = useState(null);
    const [captureId, setCaptureId] = useState("");
    const [selectedItemIds, setSelectedItemIds] = useState([]);
    const [isCapturing, setIsCapturing] = useState(false);
    const [result, setResult] = useState(null);
    const [error, setError] = useState("");
    const [form, setForm] = useState({
      directionName: "都市逆袭成长",
      selectedPlatforms: "番茄, 七猫",
      keywords: "都市, 逆袭, 成长",
      notes: "偏快节奏、强钩子、低门槛阅读",
    });

    function loadCapture() {
      setError("");
      setResult(null);
      setCaptureId("");
      setSelectedItemIds([]);
      setHotlists([]);
      setInsights(null);
      setIsCapturing(true);
      return requestJson("/trends/captures", { method: "POST" })
        .then(function (payload) {
          const snapshots = Array.isArray(payload.snapshots) ? payload.snapshots : [];
          setHotlists(snapshots.map(function (snapshot) {
            return Object.assign({}, snapshot, {
              provider_label: PROVIDER_LABELS[snapshot.provider] || snapshot.provider || "未知平台",
              items: Array.isArray(snapshot.items) ? snapshot.items : [],
            });
          }));
          setInsights(payload.insights || null);
          setCaptureId(payload.capture_id || "");
          setSelectedItemIds([]);
          setActiveProvider(function (current) {
            return snapshots.some(function (snapshot) { return snapshot.provider === current; })
              ? current : ((snapshots[0] || {}).provider || "fanqie");
          });
        })
        .catch(function (fetchError) {
          setError(fetchError.message || "选题雷达加载失败");
        })
        .finally(function () {
          setIsCapturing(false);
        });
    }

    useEffect(function () {
      loadCapture();
    }, []);

    function updateField(name, value) {
      setForm(function (current) {
        return Object.assign({}, current, { [name]: value });
      });
    }

    function toggleTrendItem(itemId) {
      if (!itemId) {
        return;
      }
      setSelectedItemIds(function (current) {
        return current.indexOf(itemId) >= 0
          ? current.filter(function (value) {
              return value !== itemId;
            })
          : current.concat(itemId);
      });
    }

    function handleGenerate() {
      setError("");
      if (!captureId || isCapturing) {
        setError("请先完成一次趋势采集后再生成方向包。");
        return;
      }
      requestJson("/trends/generate-direction", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          direction_name: form.directionName,
          selected_platforms: form.selectedPlatforms
            .split(",")
            .map(function (item) {
              return item.trim();
            })
            .filter(Boolean),
          keywords: form.keywords
            .split(",")
            .map(function (item) {
              return item.trim();
            })
            .filter(Boolean),
          notes: form.notes,
          capture_id: captureId,
          selected_item_ids: selectedItemIds,
        }),
      })
        .then(setResult)
        .catch(function (requestError) {
          setError(requestError.message || "方向生成失败");
        });
    }

    return ReactLib.createElement(
      "main",
      { className: "radar-shell" },
      ReactLib.createElement(
        "section",
        { className: "radar-hero" },
        ReactLib.createElement("p", { className: "radar-eyebrow" }, "Write Agent · 选题雷达"),
        ReactLib.createElement("h1", null, "选题雷达"),
        ReactLib.createElement(
          "p",
          null,
          "浏览平台热榜，挑选灵感依据，将写作方向带入大纲。"
        ),
        ReactLib.createElement(Navigation, { current: "/radar" })
      ),
      error
        ? ReactLib.createElement(
            "section",
            { className: "radar-panel", style: { marginTop: "18px" } },
            ReactLib.createElement("div", { className: "radar-empty", role: "alert" }, error)
          )
        : null,
      ReactLib.createElement(
        "section",
        { className: "radar-grid" },
        ReactLib.createElement(
          "div",
          { className: "radar-panel radar-ranking-panel" },
          ReactLib.createElement(
            "div",
            { className: "radar-toolbar" },
            ReactLib.createElement("h2", null, "热门榜单"),
            ReactLib.createElement(
              "button",
              { className: "radar-button radar-refresh", type: "button", onClick: loadCapture, disabled: isCapturing },
              isCapturing ? "采集中..." : "刷新采集"
            )
          ),
          ReactLib.createElement(
            "div",
            { className: "radar-platforms", role: "group", "aria-label": "选择榜单平台" },
            hotlists.map(function (provider) {
              const selectedCount = provider.items.filter(function (item) {
                return selectedItemIds.indexOf(item.id) >= 0;
              }).length;
              return ReactLib.createElement(
                "button",
                {
                  key: provider.provider,
                  className: "radar-platform",
                  type: "button",
                  "aria-pressed": activeProvider === provider.provider,
                  onClick: function () { setActiveProvider(provider.provider); },
                },
                provider.provider_label,
                ReactLib.createElement("span", { className: "radar-platform-count" }, provider.items.length),
                selectedCount ? ReactLib.createElement("small", null, "已选 " + selectedCount) : null
              );
            })
          ),
          ReactLib.createElement(
            "div",
            { className: "radar-ranking" },
            hotlists.length === 0
              ? ReactLib.createElement("div", { className: "radar-empty" }, isCapturing ? "正在采集榜单..." : "本次采集没有可展示的平台数据。")
              : hotlists.filter(function (provider) { return provider.provider === activeProvider; }).map(function (provider) {
              return ReactLib.createElement(
                "section",
                { key: provider.provider },
                ReactLib.createElement(
                  "div",
                  { className: "radar-capture-meta", title: captureId ? "采集批次：" + captureId : undefined },
                  ReactLib.createElement("span", { className: "capture-status", "data-status": provider.status }, COLLECTION_LABELS[provider.status] || statusLabel(provider.status)),
                  ReactLib.createElement("span", null, "更新于 " + captureTime(provider.captured_at))
                ),
                ReactLib.createElement(
                  "div",
                  { className: "radar-list radar-ranking-list", role: "region", "aria-label": provider.provider_label + "榜单", tabIndex: 0 },
                  provider.items.length === 0
                    ? ReactLib.createElement("div", { className: "radar-empty" }, provider.error_message || "该平台本次没有榜单条目。")
                    : provider.items.map(function (item) {
                    const itemId = item.id || "";
                    return ReactLib.createElement(
                      "article",
                      { className: "radar-book" + (selectedItemIds.indexOf(itemId) >= 0 ? " is-selected" : ""), key: itemId || provider.provider + "-" + item.rank },
                      ReactLib.createElement(
                            "label",
                            { className: "radar-book-choice" },
                            ReactLib.createElement("input", {
                              type: "checkbox",
                              disabled: !itemId,
                              "aria-label": "选择《" + item.title + "》作为方向依据",
                              checked: selectedItemIds.indexOf(itemId) >= 0,
                              onChange: function () {
                                toggleTrendItem(itemId);
                              },
                            }),
                            ReactLib.createElement("span", { className: "radar-rank", "data-top": item.rank <= 3 }, String(item.rank).padStart(2, "0")),
                            ReactLib.createElement(
                              "span",
                              { className: "radar-book-info" },
                              ReactLib.createElement("strong", { className: "radar-book-title" }, item.title),
                              ReactLib.createElement(
                                "span", { className: "radar-book-meta" },
                                ReactLib.createElement("span", null, item.author || "未知作者"),
                                ReactLib.createElement("span", null, item.category || "未分类")
                              ),
                              item.summary ? ReactLib.createElement("span", { className: "radar-book-summary" }, item.summary) : null
                            )
                      )
                    );
                  })
                )
              );
            })
          ),
          ReactLib.createElement(
            "div", { className: "radar-selection", role: "status" },
            ReactLib.createElement("strong", null, "已选 " + selectedItemIds.length + " 本"),
            ReactLib.createElement("span", null, "可跨平台勾选，作为写作方向的参考")
          )
        ),
        ReactLib.createElement(
          "div",
          { className: "radar-panel radar-direction-panel" },
          ReactLib.createElement("h2", null, "方向生成器"),
          insights
            ? ReactLib.createElement(
                "div",
                { className: "radar-insights", style: { marginBottom: "16px" } },
                ReactLib.createElement(
                  "article",
                  { className: "radar-card" },
                  ReactLib.createElement("h3", null, "热点摘要"),
                  ReactLib.createElement(
                    "div",
                    { className: "radar-meta" },
                    (insights.top_keywords || []).map(function (item) {
                      return ReactLib.createElement("span", { className: "radar-chip", key: item.keyword }, item.keyword + " x " + item.count);
                    })
                  ),
                  ReactLib.createElement(
                    "div",
                    { className: "radar-meta" },
                    (insights.top_categories || []).map(function (item) {
                      return ReactLib.createElement("span", { className: "radar-chip", key: item.category }, item.category + " x " + item.count);
                    })
                  ),
                  ReactLib.createElement(
                    "div",
                    { className: "radar-meta" },
                    (insights.genre_buckets || []).map(function (item) {
                      return ReactLib.createElement("span", { className: "radar-chip", key: item.bucket }, item.bucket + " x " + item.count);
                    })
                  )
                )
              )
            : ReactLib.createElement("div", { className: "radar-empty" }, "暂无趋势分析，请先采集趋势。"),
          ReactLib.createElement(
            "div",
            { className: "radar-form" },
            ReactLib.createElement("label", { htmlFor: "direction-name" }, "写作方向"),
            ReactLib.createElement("input", {
              id: "direction-name",
              value: form.directionName,
              onChange: function (event) {
                updateField("directionName", event.target.value);
              },
            }),
            ReactLib.createElement("label", { htmlFor: "selected-platforms" }, "目标平台"),
            ReactLib.createElement("input", {
              id: "selected-platforms",
              value: form.selectedPlatforms,
              onChange: function (event) {
                updateField("selectedPlatforms", event.target.value);
              },
            }),
            ReactLib.createElement("label", { htmlFor: "keywords" }, "关键词"),
            ReactLib.createElement("input", {
              id: "keywords",
              value: form.keywords,
              onChange: function (event) {
                updateField("keywords", event.target.value);
              },
            }),
            ReactLib.createElement("label", { htmlFor: "notes" }, "补充说明"),
            ReactLib.createElement("textarea", {
              id: "notes",
              value: form.notes,
              onChange: function (event) {
                updateField("notes", event.target.value);
              },
            }),
            ReactLib.createElement(
              "button",
              { className: "radar-button", type: "button", onClick: handleGenerate, disabled: !captureId || isCapturing },
              "生成方向包"
            )
          ),
          result
            ? ReactLib.createElement(
                "div",
                { className: "radar-output" },
                ReactLib.createElement("h3", null, "Direction Package"),
                ReactLib.createElement(
                  "div",
                  { className: "radar-result-grid" },
                  ReactLib.createElement(
                    ResultBlock,
                    { title: result.direction_name || "方向名" },
                    ReactLib.createElement("p", null, result.core_hook || ""),
                    ReactLib.createElement(
                      "div",
                      { className: "radar-meta" },
                      (result.target_platforms || []).map(function (platform) {
                        return ReactLib.createElement("span", { className: "radar-chip", key: platform }, platform);
                      })
                    ),
                    ReactLib.createElement(
                      "div",
                      { className: "radar-meta" },
                      ReactLib.createElement("span", { className: "radar-chip" }, result.audience || "未指定受众")
                    )
                  ),
                  ReactLib.createElement(
                    ResultBlock,
                    { title: "标题候选" },
                    ReactLib.createElement(
                      "ul",
                      { className: "radar-bullets" },
                      (result.title_candidates || []).map(function (title) {
                        return ReactLib.createElement("li", { key: title }, title);
                      })
                    )
                  ),
                  ReactLib.createElement(ResultBlock, { title: "Logline" }, ReactLib.createElement("p", null, result.logline || "")),
                  ReactLib.createElement(
                    ResultBlock,
                    { title: "卖点提炼" },
                    ReactLib.createElement(
                      "ul",
                      { className: "radar-bullets" },
                      (result.selling_points || []).map(function (item) {
                        return ReactLib.createElement("li", { key: item }, item);
                      })
                    )
                  ),
                  ReactLib.createElement(
                    ResultBlock,
                    { title: "风险提示" },
                    ReactLib.createElement(
                      "ul",
                      { className: "radar-bullets" },
                      (result.risk_flags || []).map(function (item) {
                        return ReactLib.createElement("li", { key: item }, item);
                      })
                    )
                  ),
                  ReactLib.createElement(
                    ResultBlock,
                    { title: "三幕大纲" },
                    ReactLib.createElement(
                      "ul",
                      { className: "radar-bullets" },
                      ((result.outline || {}).acts || []).map(function (act, index) {
                        return ReactLib.createElement("li", { key: index }, act);
                      })
                    )
                  ),
                  ReactLib.createElement(
                    ResultBlock,
                    { title: "开篇场景" },
                    ReactLib.createElement(
                      "ul",
                      { className: "radar-bullets" },
                      ReactLib.createElement("li", null, "地点：" + (((result.opening_scene || {}).location) || "")),
                      ReactLib.createElement("li", null, "冲突：" + (((result.opening_scene || {}).conflict) || "")),
                      ReactLib.createElement("li", null, "钩子：" + (((result.opening_scene || {}).hook) || ""))
                    )
                  ),
                  ReactLib.createElement(
                    ResultBlock,
                    { title: "章节规划", wide: true },
                    ReactLib.createElement(
                      "div",
                      { className: "radar-list" },
                      (result.chapter_plan || []).map(function (item, index) {
                        return ReactLib.createElement(
                          "article",
                          { className: "radar-card", key: index },
                          ReactLib.createElement("h3", null, "Chapter " + item.chapter),
                          ReactLib.createElement("p", null, item.goal || "")
                        );
                      })
                    ),
                    ReactLib.createElement(
                      "a",
                      {
                        href: buildOutlinePrefillLink(result, form),
                        className: "radar-button",
                        style: { display: "inline-block", marginTop: "14px", textDecoration: "none" },
                      },
                      "带入大纲页"
                    )
                  )
                )
              )
            : null
        )
      )
    );
  }

  const rootElement = document.getElementById("radar-root");
  ReactDOMLib.createRoot(rootElement).render(ReactLib.createElement(App));
})();
