(function () {
  const ReactLib = window.React;
  const ReactDOMLib = window.ReactDOM;
  const { fetchJson, requestJson, Navigation, FormField, Feedback } = window.WriteAgentUI;

  if (!ReactLib || !ReactDOMLib) {
    throw new Error("页面运行资源加载失败，请刷新重试");
  }

  const { Fragment, useEffect, useRef, useState } = ReactLib;
  const searchParams = new URLSearchParams(window.location.search);

  function readPrefill(name) {
    const value = searchParams.get(name);
    return value === null || value === "" ? null : value;
  }

  function parseSourceTracePrefill(value) {
    if (!value) {
      return null;
    }
    try {
      const parsed = JSON.parse(value);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return null;
      }
      if (typeof parsed.capture_id !== "string" || !parsed.capture_id.trim()) {
        return null;
      }
      const fields = ["snapshot_ids", "providers", "selected_item_ids"];
      if (!fields.every(function (field) {
        return Array.isArray(parsed[field]) && parsed[field].every(function (item) {
          return typeof item === "string";
        });
      })) {
        return null;
      }
      if (parsed.captured_at !== null && parsed.captured_at !== undefined && typeof parsed.captured_at !== "string") {
        return null;
      }
      return {
        capture_id: parsed.capture_id,
        snapshot_ids: parsed.snapshot_ids.slice(),
        providers: parsed.providers.slice(),
        selected_item_ids: parsed.selected_item_ids.slice(),
        captured_at: parsed.captured_at || null,
      };
    } catch (_error) {
      return null;
    }
  }

  function normalizeOutlineResult(payload, form, sourceTrace) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      return null;
    }

    const existingBookOutline = payload.book_outline;
    const hasBookOutline = existingBookOutline &&
      typeof existingBookOutline === "object" &&
      !Array.isArray(existingBookOutline) &&
      Object.keys(existingBookOutline).length > 0;
    const directionOutline = payload.outline;
    const hasDirectionOutline = directionOutline &&
      typeof directionOutline === "object" &&
      !Array.isArray(directionOutline) &&
      Object.keys(directionOutline).length > 0;
    const chapterPlan = Array.isArray(payload.chapter_plan) ? payload.chapter_plan : [];
    if (!hasBookOutline && !hasDirectionOutline && chapterPlan.length === 0) {
      return null;
    }

    const bookOutline = hasBookOutline
      ? Object.assign({}, existingBookOutline)
      : Object.assign({}, hasDirectionOutline ? directionOutline : {}, {
          chapter_seed_plan: chapterPlan,
        });
    if (sourceTrace) {
      bookOutline.direction_source = sourceTrace;
    }

    const titleCandidates = Array.isArray(payload.title_candidates) ? payload.title_candidates : [];
    return Object.assign({}, payload, {
      title: payload.title || titleCandidates[0] || form.title,
      logline: payload.logline || "",
      style_rules: payload.style_rules && typeof payload.style_rules === "object" ? payload.style_rules : {},
      book_outline: bookOutline,
    });
  }

  function ResultCard(props) {
    return ReactLib.createElement(
      "section",
      { className: props.wide ? "outline-result-card outline-result-card-wide" : "outline-result-card" },
      ReactLib.createElement("h3", null, props.title),
      props.children
    );
  }

  function renderStyleRules(styleRules) {
    if (!styleRules || typeof styleRules !== "object") {
      return ReactLib.createElement("p", null, "暂无风格规则");
    }

    return ReactLib.createElement(
      "ul",
      { className: "outline-bullets" },
      Object.keys(styleRules).map(function (key) {
        return ReactLib.createElement("li", { key: key }, key + ": " + styleRules[key]);
      })
    );
  }

  function renderSeedPlanningAction(result, form, isPlanning, busy, handlePlanChapters) {
    const seeds = (((result || {}).book_outline || {}).chapter_seed_plan) || [];
    if (!form.bookId || seeds.length === 0) {
      return null;
    }

    return ReactLib.createElement(
      "div",
      { className: "outline-button-row" },
      ReactLib.createElement(
        "button",
        {
          type: "button",
          className: "outline-secondary",
          onClick: handlePlanChapters,
          disabled: busy,
        },
        isPlanning ? "批量建章中..." : "按章节种子批量建章"
      )
    );
  }

  function App() {
    const sourceTracePrefill = parseSourceTracePrefill(readPrefill("source_trace"));
    const [books, setBooks] = useState([]);
    const [result, setResult] = useState(null);
    const [error, setError] = useState("");
    const [saveMessage, setSaveMessage] = useState("");
    const [planMessage, setPlanMessage] = useState("");
    const [isPlanning, setIsPlanning] = useState(false);
    const [isGenerating, setIsGenerating] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const operationRef = useRef(false);
    const busy = isGenerating || isSaving || isPlanning;
    const [form, setForm] = useState({
      bookId: "",
      title: "雾城翻盘",
      platform: "fanqie",
      genre: "urban",
      directionName: "都市逆袭成长",
      coreHook: "普通人借势翻盘，打进城市权力缝隙。",
      audience: "男频快节奏读者",
      notes: "强钩子、低门槛、前三章必须有明显反转。",
    });

    useEffect(function () {
      const nextFields = {
        title: readPrefill("title"),
        platform: readPrefill("platform"),
        genre: readPrefill("genre"),
        directionName: readPrefill("direction_name"),
        coreHook: readPrefill("core_hook"),
        audience: readPrefill("audience"),
        notes: readPrefill("notes"),
      };

      if (Object.values(nextFields).some(Boolean)) {
        setForm(function (current) {
          return Object.assign({}, current, {
            title: nextFields.title || current.title,
            platform: nextFields.platform || current.platform,
            genre: nextFields.genre || current.genre,
            directionName: nextFields.directionName || current.directionName,
            coreHook: nextFields.coreHook || current.coreHook,
            audience: nextFields.audience || current.audience,
            notes: nextFields.notes || current.notes,
          });
        });
      }

      fetchJson("/books")
        .then(function (rows) {
          setBooks(rows);
          if (rows.length > 0) {
            setForm(function (current) {
              return Object.assign({}, current, { bookId: rows[0].id });
            });
          }
        })
        .catch(function (fetchError) {
          setError(fetchError.message || "书籍列表加载失败");
        });
    }, []);

    function updateField(name, value) {
      setForm(function (current) {
        return Object.assign({}, current, { [name]: value });
      });
    }

    function handleGenerate() {
      if (operationRef.current) { return; }
      operationRef.current = true;
      setIsGenerating(true);
      setError("");
      setSaveMessage("");
      setPlanMessage("");

      requestJson("/outline/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: form.title,
          platform: form.platform,
          genre: form.genre,
          direction_name: form.directionName,
          core_hook: form.coreHook,
          audience: form.audience,
          notes: form.notes,
        }),
      })
        .then(function (payload) {
          const normalized = normalizeOutlineResult(payload, form, sourceTracePrefill);
          if (!normalized) {
            throw new Error("大纲生成结果缺少有效大纲数据");
          }
          setResult(normalized);
        })
        .catch(function (requestError) {
          setError(requestError.message || "大纲生成失败");
        })
        .finally(function () {
          operationRef.current = false;
          setIsGenerating(false);
        });
    }

    function handleSave() {
      if (operationRef.current || !result || !form.bookId) { return; }
      operationRef.current = true;
      setIsSaving(true);
      setError("");
      setSaveMessage("");
      setPlanMessage("");

      requestJson("/books/" + form.bookId + "/outline", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          book_outline: result ? result.book_outline : null,
          logline: result ? result.logline : "",
          style_rules: result ? result.style_rules : {},
        }),
      })
        .then(function (payload) {
          setSaveMessage("已保存到书籍：" + payload.title);
          setResult(function (current) {
            if (!current) {
              return current;
            }

            return Object.assign({}, current, {
              book_outline: payload.book_outline,
              logline: payload.logline,
              style_rules: payload.style_rules,
            });
          });
        })
        .catch(function (requestError) {
          setError(requestError.message || "大纲保存失败");
        })
        .finally(function () {
          operationRef.current = false;
          setIsSaving(false);
        });
    }

    function handlePlanChapters() {
      const seeds = (((result || {}).book_outline || {}).chapter_seed_plan) || [];
      if (!form.bookId || seeds.length === 0 || operationRef.current) {
        return;
      }

      setError("");
      setSaveMessage("");
      setPlanMessage("");
      setIsPlanning(true);
      operationRef.current = true;

      requestJson("/books/" + form.bookId + "/chapters/from-outline", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      })
        .then(function (payload) {
          setPlanMessage("已根据章节种子批量创建 " + (payload.created_count || 0) + " 个计划章节。");
        })
        .catch(function (requestError) {
          setError(requestError.message || "批量建章失败");
        })
        .finally(function () {
          operationRef.current = false;
          setIsPlanning(false);
        });
    }

    return ReactLib.createElement(
      "main",
      { className: "outline-shell" },
      ReactLib.createElement(
        "section",
        { className: "outline-hero" },
        ReactLib.createElement("p", { className: "outline-eyebrow" }, "Write Agent · 大纲工作区"),
        ReactLib.createElement("h1", null, "大纲生成"),
        ReactLib.createElement(
          "p",
          null,
          "从写作方向、核心钩子和目标平台偏好生成书籍设定，并将结果回写到当前书籍档案。"
        ),
        ReactLib.createElement(Navigation, { current: "/outline" })
      ),
      ReactLib.createElement(Feedback, { className: "outline-error", role: "alert", message: error }),
      ReactLib.createElement(Feedback, { className: "outline-success", message: saveMessage }),
      ReactLib.createElement(Feedback, { className: "outline-success", message: planMessage }),
      ReactLib.createElement(
        "section",
        { className: "outline-grid" },
        ReactLib.createElement(
          "section",
          { className: "outline-panel outline-form-panel" },
          ReactLib.createElement("h2", null, "生成设定"),
          ReactLib.createElement(
            FormField,
            {
              id: "book-id", label: "目标书籍", as: "select",
              disabled: busy || books.length === 0,
              value: form.bookId,
              onValueChange: value => updateField("bookId", value),
            },
            books.length === 0 ? ReactLib.createElement("option", { value: "" }, "暂无书籍，请先在文章管理中新建") : null,
            books.map(function (book) {
              return ReactLib.createElement("option", { key: book.id, value: book.id }, book.title);
            })
          ),
          ReactLib.createElement(FormField, {
            id: "outline-title", label: "书名", disabled: busy, value: form.title,
            onValueChange: value => updateField("title", value),
          }),
          ReactLib.createElement(FormField, {
            id: "outline-platform", label: "平台", disabled: busy, value: form.platform,
            onValueChange: value => updateField("platform", value),
          }),
          ReactLib.createElement(FormField, {
            id: "outline-genre", label: "题材", disabled: busy, value: form.genre,
            onValueChange: value => updateField("genre", value),
          }),
          ReactLib.createElement(FormField, {
            id: "direction-name", label: "方向名称", disabled: busy, value: form.directionName,
            onValueChange: value => updateField("directionName", value),
          }),
          ReactLib.createElement(FormField, {
            id: "core-hook", label: "核心钩子", as: "textarea", disabled: busy, value: form.coreHook,
            onValueChange: value => updateField("coreHook", value),
          }),
          ReactLib.createElement(FormField, {
            id: "audience", label: "目标读者", disabled: busy, value: form.audience,
            onValueChange: value => updateField("audience", value),
          }),
          ReactLib.createElement(FormField, {
            id: "outline-notes", label: "补充说明", as: "textarea", disabled: busy, value: form.notes,
            onValueChange: value => updateField("notes", value),
          }),
          ReactLib.createElement(
            "div",
            { className: "outline-button-row" },
            ReactLib.createElement("button", { type: "button", onClick: handleGenerate, disabled: busy }, isGenerating ? "生成中..." : "生成设定"),
            ReactLib.createElement(
              "button",
              {
                type: "button",
                className: "outline-secondary",
                onClick: handleSave,
                disabled: busy || !result || !form.bookId,
              },
              isSaving ? "保存中..." : "保存到书籍"
            )
          )
        ),
        ReactLib.createElement(
          "section",
          { className: "outline-panel" },
          ReactLib.createElement("h2", null, "结果预览"),
          !result
            ? ReactLib.createElement("div", { className: "outline-empty" }, "先生成一版设定，这里会展示故事梗概、三幕大纲和章节种子。")
            : ReactLib.createElement(
                "div",
                { className: "outline-result-grid" },
                ReactLib.createElement(
                  ResultCard,
                  { title: result.title || form.title },
                  ReactLib.createElement("p", null, result.logline || "暂无故事梗概")
                ),
                ReactLib.createElement(
                  ResultCard,
                  { title: "目标方向" },
                  ReactLib.createElement("p", null, form.directionName)
                ),
                ReactLib.createElement(
                  ResultCard,
                  { title: "三幕大纲" },
                  ReactLib.createElement(
                    "ul",
                    { className: "outline-bullets" },
                    (((result.book_outline || {}).acts) || []).map(function (act, index) {
                      return ReactLib.createElement("li", { key: index }, act);
                    })
                  )
                ),
                ReactLib.createElement(
                  ResultCard,
                  { title: "角色弧线" },
                  ReactLib.createElement(
                    "ul",
                    { className: "outline-bullets" },
                    (((result.book_outline || {}).character_arcs) || []).map(function (arc, index) {
                      return ReactLib.createElement("li", { key: index }, arc);
                    })
                  )
                ),
                ReactLib.createElement(
                  ResultCard,
                  { title: "章节种子", wide: true },
                  ReactLib.createElement(
                    Fragment,
                    null,
                    renderSeedPlanningAction(result, form, isPlanning, busy, handlePlanChapters),
                    ReactLib.createElement(
                      "div",
                      { className: "outline-seed-list" },
                      ((((result.book_outline || {}).chapter_seed_plan) || [])).map(function (seed, index) {
                        return ReactLib.createElement(
                          "article",
                          { className: "outline-seed-card", key: index },
                          ReactLib.createElement("strong", null, "第 " + seed.chapter + " 章"),
                          ReactLib.createElement("p", null, seed.goal || "")
                        );
                      })
                    )
                  )
                ),
                ReactLib.createElement(
                  ResultCard,
                  { title: "风格规则", wide: true },
                  renderStyleRules(result.style_rules)
                )
              )
        )
      )
    );
  }

  const rootElement = document.getElementById("outline-root");
  ReactDOMLib.createRoot(rootElement).render(ReactLib.createElement(App));
})();
